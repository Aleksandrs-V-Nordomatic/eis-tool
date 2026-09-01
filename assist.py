#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Read the files the deterministic extractor honestly could not — and nothing else.

    python3 assist.py --pack out                       # local OCR: no account, no key
    python3 assist.py --pack out --provider gemini     # a model, if a key is configured
    python3 assist.py --pack out --dry-run             # what would be read, and by what

WHY THE DEFAULT READER IS A LOCAL ONE, AND NOT A MODEL. The first version of this file
required `GEMINI_API_KEY`, which meant the pipeline depended on a personal account that
nothing in this repository owns. That is a worse problem than an unread scan: a credential
with no owner fails silently, at the worst moment, and cannot be handed over.

So the default reader is **Tesseract with the Latvian language pack, installed on the
runner**. It needs no account, no key, no e-mail, no billing relationship and no
migration plan. It is worse than a frontier model on a bad scan and entirely adequate on
a clean one — and the volume argues it is enough: the deterministic extractor reads most
tenders whole, leaving this lane an ordinarily empty queue. Buying a cloud dependency to
serve a queue that is usually empty is the wrong trade.

A hosted model stays available for the day a scan defeats Tesseract; it is one flag and
one secret, and `PROVIDERS` below is the only place that has to know it exists.

WHY THIS DOES NOT BREAK THE "NO MODEL" RULE. The rule was always about the MAIN path: same
bytes in, same text out, and a coverage claim that can be checked. That still holds — this
step never touches a file the extractor could read, and never rewrites `normalized/`.

What it works on is the queue the extractor already produces and names: `unreadable_files`
in `manifest_normalized.json`, each entry carrying a digest and a reason. Today those are
honest holes — a scanned PDF is reported `no text layer` and that is the end of it. A hole
a person still has to open by hand is not more honest than a hole read by a model and
labelled as such; it is only cheaper to defend.

FOUR PROPERTIES MAKE IT SAFE TO KEEP:

**Quarantined.** Output lands in `llm/`, never in `normalized/`. Every entry is marked
`llm-fallback`, so a downstream reader can always tell which characters a machine
transcribed from a picture and which a decoder recovered from a text layer. A consumer
that requires located quotes must treat this text as a reason to look, never as the quote.

**Cached by content.** The key is the file's sha256. A digest already answered is never
sent again, so a re-run is byte-identical and costs nothing — determinism's honest cousin,
and the reason a warm run still reports zero tokens like the deterministic core does.

**Provenance or it did not happen.** Every answer is written beside a record of which
provider, model and prompt produced it, and when. A transcription whose origin is unknown
is worse than a gap, because a gap does not look like evidence.

**Free by default, and bounded.** The provider is Google's free tier, which for EEA users
carries the paid-tier data terms — the submitted documents are not used for training. Free
quota is finite, so the run paces itself and stops cleanly: anything left over is marked
`llm-deferred` and the next run picks it up. Running out of quota is not a failure.

NOT SENT, EVER: drawings (DWG/DXF) — a named non-goal with PDF duplicates in practice;
files the extractor read perfectly well; and anything the caller did not ask for by reason.
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

import net

from console import say, utf8_streams

# The reasons worth spending a request on. A scan is a document somebody wrote and nobody
# can read; a CAD drawing is not prose and a model would invent it. Widen with --reasons
# only with a measured argument.
DEFAULT_REASONS = ("no text layer",)

# A scan does not always arrive as a PDF. Alongside the files reported as "no text layer",
# a day brings jpg/png attachments no decoder even tried — buyers photograph or scan a page
# and attach the image itself. Those are the same document in a different wrapper, so they
# belong in the same queue.
#
# Some of those images will be photographs and logos. That costs nothing worth guarding
# against: the local reader returns no text, the file is recorded as such, and the only price
# is CPU seconds on a runner that is free. Guessing which image is a document would be
# exactly the judgement about usefulness this project refuses to make.
IMAGES = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")

# Vector drawings are a named non-goal, and rasterising one to guess at it is worse than
# leaving the honest gap: a model or an OCR engine asked to read a floor plan invents rooms.
NEVER = (".dwg", ".dxf", ".emf", ".wmf")

# Gemini accepts a PDF inline inside the request; past roughly 20 MB the upload has to go
# through the Files API instead. Base64 inflates by 4/3, so the raw ceiling sits lower.
# Larger files are reported, not silently skipped — the Files API is the next step, and a
# hole that names itself is one somebody can close.
INLINE_LIMIT = 14 * 1024 * 1024

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent")
DEFAULT_MODEL = "gemini-2.5-flash"

PROMPT_VERSION = 1
PROMPT = """Transcribe this document to Markdown, completely and literally.

Rules:
- Transcribe every word you can read, in the document's own language (usually Latvian).
- Do not summarise, translate, correct or reorder anything.
- Keep tables as Markdown tables. Keep headings, numbering and clause numbers exactly.
- Where the scan is unreadable, write [nesalasāms] instead of guessing the words.
- Output only the transcription. No preamble, no commentary about the document.
"""


class Quota(RuntimeError):
    """The provider says we are out of free quota. Not a failure — a tomorrow."""


def prompt_digest():
    return hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()[:16]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------- the queue
def load_pack(pack):
    """(unreadable entries, {sha256: relative path}) from a finished fetch+normalize."""
    with open(os.path.join(pack, "normalized", "manifest_normalized.json"),
              encoding="utf-8") as fh:
        normalized = json.load(fh)
    with open(os.path.join(pack, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)

    by_digest = {}
    for record in manifest.get("documents") or []:
        for f in record.get("files") or []:
            if f.get("sha256"):
                by_digest.setdefault(f["sha256"], f["path"])
    return (normalized.get("unreadable_files") or []), by_digest


def queue(unreadable, by_digest, reasons=DEFAULT_REASONS, limit=None, size_limit=INLINE_LIMIT):
    """What to send, with a reason for everything left behind.

    A file the extractor unpacked out of an archive has no downloaded original to point at
    — `normalize` deletes its scratch directory when it finishes — so it cannot be sent
    from here. That is stated per file rather than left as a silent shortfall.
    """
    send, skip = [], []
    for entry in unreadable:
        note = entry.get("reason") or ""
        name = (entry.get("file") or "").lower()
        ext = os.path.splitext(name)[1]
        if ext in NEVER or any(name.endswith(x) for x in NEVER):
            skip.append(dict(entry, skipped="drawing-not-sent"))
        elif not (any(r in note for r in reasons) or ext in IMAGES):
            skip.append(dict(entry, skipped="reason-not-selected"))
        elif not entry.get("sha256"):
            skip.append(dict(entry, skipped="no-digest-to-cache-by"))
        elif entry.get("path"):
            # The extractor named where the bytes are. That is the only way to reach a file
            # that exists solely inside an archive, which is where most scans in a day live.
            send.append(dict(entry))
        elif entry["sha256"] not in by_digest:
            skip.append(dict(entry, skipped="source-not-retained"))
        elif size_limit is not None and entry.get("bytes", 0) > size_limit:
            # A ceiling that belongs to the reader, not to the file: a hosted model has a
            # request size limit, the local one only has patience.
            skip.append(dict(entry, skipped="too-large-for-inline-upload"))
        else:
            send.append(dict(entry, path=by_digest[entry["sha256"]]))

    if limit is not None and len(send) > limit:
        for entry in send[limit:]:
            skip.append(dict(entry, skipped="over-the-run-limit"))
        send = send[:limit]
    return send, skip


# ---------------------------------------------------------------------------- the provider
def gemini_send(blob, mime, model, api_key, timeout=180):
    """One document to Gemini. Returns (text, usage). Raises Quota when the free tier is out."""
    body = json.dumps({
        "contents": [{"parts": [
            {"text": PROMPT},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(blob).decode()}},
        ]}],
        # Transcription, not composition: keep the model as close to reading as it gets.
        "generationConfig": {"temperature": 0.0},
    }).encode("utf-8")

    request = urllib.request.Request(
        ENDPOINT % model, data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    # The HTTPError arm comes first and keeps its own meaning: 429 and 503 are the provider
    # saying the free tier is spent, which the caller counts and stops on, and retrying them
    # here would spend the loop re-asking a settled question. Everything below it is the
    # transport, which says nothing about quota — and used to escape as a bare OSError past
    # the per-file `except RuntimeError` that exists to keep one unreadable scan from ending
    # the lane.
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (429, 503):
            raise Quota("provider is out of free quota or overloaded (%d): %s"
                        % (exc.code, detail))
        raise RuntimeError("provider refused (%d): %s" % (exc.code, detail))
    except net.TRANSPORT_ERRORS as exc:
        raise RuntimeError("provider unreachable (%s: %s)" % (type(exc).__name__, exc))

    candidates = payload.get("candidates") or []
    if not candidates:
        # A refusal carries no candidate. Saying so beats writing an empty transcription
        # that reads like a document with nothing in it.
        raise RuntimeError("provider returned no candidate: %s" % json.dumps(payload)[:300])
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text") or "" for p in parts).strip()
    return text, payload.get("usageMetadata") or {}


def mime_for(path):
    return {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".tif": "image/tiff",
            ".tiff": "image/tiff"}.get(os.path.splitext(path)[1].lower(), "application/pdf")


# --------------------------------------------------------------------- the local reader
OCR_DPI = 300           # below ~200 Tesseract loses Latvian diacritics on small print
OCR_LANGS = "lav+eng"   # tenders mix Latvian prose with English equipment names
OCR_MAX_PAGES = 200


def tesseract_read(blob, mime, model, api_key, timeout=900):
    """Optical character recognition on the runner. No account, no key, no network.

    PyMuPDF is already a pinned dependency for the deterministic path, so rendering pages
    costs no new package; Tesseract and its Latvian pack come from apt. `model` carries the
    language string so a caller can widen it (`lav+eng+rus`) without touching this code.

    Page headings match the deterministic extractor's (`## Lapa N`), so a downstream reader
    does not have to care which of the two produced a given document — only the manifest's
    `extraction` field says that, and it says it plainly.
    """
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract is not installed — apt-get install tesseract-ocr "
                           "tesseract-ocr-lav, or choose another --provider")

    workdir = tempfile.mkdtemp(prefix="ocr_")
    try:
        source = os.path.join(workdir, "source")
        with open(source, "wb") as fh:
            fh.write(blob)

        pages = []
        if mime == "application/pdf":
            import fitz                                        # PyMuPDF, already pinned
            with fitz.open(source) as doc:
                for number, page in enumerate(doc, 1):
                    if number > OCR_MAX_PAGES:
                        break
                    image = os.path.join(workdir, "p%04d.png" % number)
                    page.get_pixmap(dpi=OCR_DPI).save(image)
                    pages.append((number, image))
        else:
            pages.append((1, source))

        parts, read = [], 0
        for number, image in pages:
            done = subprocess.run(
                ["tesseract", image, "stdout", "-l", model or OCR_LANGS, "--psm", "1"],
                capture_output=True, timeout=timeout)
            text = (done.stdout or b"").decode("utf-8", "replace").strip()
            # A page that yields nothing is a blank or a pure drawing. Recording it as an
            # empty heading would pad the output with the appearance of content.
            if text:
                read += 1
                parts.append("## Lapa %d\n\n%s" % (number, text))
        return "\n\n".join(parts).strip(), {"pages": len(pages), "pages_with_text": read,
                                            "dpi": OCR_DPI, "languages": model or OCR_LANGS}
    finally:
        import shutil as _shutil
        _shutil.rmtree(workdir, ignore_errors=True)


# The whole registry. Adding a reader is one entry here plus its function — callers,
# manifests and the cache do not change, which is what makes swapping one a secret change
# rather than a code change.
PROVIDERS = {
    # name:      (function,        needs a key?, default model//languages,   size ceiling)
    "tesseract": (tesseract_read,  False,        OCR_LANGS,                  None),
    "gemini":    (gemini_send,     True,         DEFAULT_MODEL,              INLINE_LIMIT),
}
DEFAULT_PROVIDER = "tesseract"


# --------------------------------------------------------------------------------- the run
def read_cached(lane, digest):
    md = os.path.join(lane, digest + ".md")
    prov = os.path.join(lane, digest + ".provenance.json")
    if os.path.exists(md) and os.path.exists(prov):
        with open(md, encoding="utf-8") as fh:
            text = fh.read()
        with open(prov, encoding="utf-8") as fh:
            return text, json.load(fh)
    return None, None


def write_result(lane, digest, text, provenance):
    os.makedirs(lane, exist_ok=True)
    with open(os.path.join(lane, digest + ".md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(lane, digest + ".provenance.json"), "w", encoding="utf-8") as fh:
        json.dump(provenance, fh, ensure_ascii=False, indent=2)


def run(pack, send_fn=None, model=None, api_key=None, reasons=DEFAULT_REASONS,
        limit=None, pace=6.0, dry_run=False, provider=DEFAULT_PROVIDER):
    """Work the queue. Returns the lane manifest; never raises on quota.

    `send_fn` overrides the registry — the tests inject a recorder through it, and nothing
    else should need to.
    """
    reader, needs_key, default_model, size_limit = PROVIDERS.get(
        provider, PROVIDERS[DEFAULT_PROVIDER])
    if send_fn is None:
        send_fn = reader
        if needs_key and not api_key:
            raise RuntimeError("provider %r needs a key and none was given" % provider)
    model = model or default_model
    # The local reader is not paced: nothing is being asked of anyone else's service.
    if provider == "tesseract":
        pace = 0

    pack = os.path.abspath(pack)
    lane = os.path.join(pack, "llm")
    unreadable, by_digest = load_pack(pack)
    send, skipped = queue(unreadable, by_digest, reasons, limit, size_limit)

    results, deferred, spent = [], [], 0
    for position, entry in enumerate(send):
        digest = entry["sha256"]
        text, provenance = read_cached(lane, digest)
        if text is not None:
            results.append({"file": entry["file"], "sha256": digest, "chars": len(text),
                            "markdown": "llm/%s.md" % digest, "source": "cache",
                            "model": provenance.get("model")})
            continue

        if dry_run:
            deferred.append(dict(entry, deferred="dry-run"))
            continue

        # Free tiers are rate limited per minute as well as per day. Pacing costs seconds
        # on a queue this size and is the difference between finishing and being throttled.
        if spent:
            time.sleep(pace)

        with open(os.path.join(pack, entry["path"]), "rb") as fh:
            blob = fh.read()
        try:
            text, usage = send_fn(blob, mime_for(entry["path"]), model, api_key)
        except Quota as exc:
            # Everything not yet done is tomorrow's work, and the cache means tomorrow
            # starts where today stopped.
            for rest in send[position:]:
                deferred.append(dict(rest, deferred=str(exc)[:160]))
            break
        except RuntimeError as exc:
            skipped.append(dict(entry, skipped="provider-error: %s" % str(exc)[:160]))
            continue

        spent += 1
        if not text:
            skipped.append(dict(entry, skipped="provider-returned-no-text"))
            continue

        provenance = {"provider": provider, "model": model,
                      "prompt_version": PROMPT_VERSION, "prompt_sha256": prompt_digest(),
                      "source_file": entry["file"], "source_sha256": digest,
                      "source_bytes": entry.get("bytes"), "at": now(),
                      "usage": usage,
                      # The one field downstream must read: which characters a decoder
                      # recovered, and which a reader guessed at from a picture.
                      "extraction": "ocr-fallback" if provider == "tesseract"
                                    else "llm-fallback"}
        write_result(lane, digest, text, provenance)
        results.append({"file": entry["file"], "sha256": digest, "chars": len(text),
                        "markdown": "llm/%s.md" % digest, "source": "provider",
                        "model": model})
        say("  read %-52s %8d chars" % (entry["file"][-52:], len(text)))

    doc = {"schema": 1, "provider": provider,
           "extraction": "ocr-fallback" if provider == "tesseract" else "llm-fallback",
           "at": now(), "model": model,
           "prompt_version": PROMPT_VERSION, "requests_spent": spent,
           "read": len(results), "deferred": len(deferred), "skipped": len(skipped),
           "chars": sum(r["chars"] for r in results),
           "documents": results, "deferred_files": deferred, "skipped_files": skipped}
    if not dry_run or results:
        os.makedirs(lane, exist_ok=True)
        with open(os.path.join(lane, "manifest_llm.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    return doc


def main(argv=None):
    utf8_streams()

    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("--pack", required=True, help="a finished fetch+normalize directory")
    ap.add_argument("--provider", default=os.environ.get("ASSIST_PROVIDER", DEFAULT_PROVIDER),
                    choices=sorted(PROVIDERS),
                    help="who reads the scans (default: local OCR, no account needed)")
    ap.add_argument("--model", default=None,
                    help="model name, or OCR language string like lav+eng")
    ap.add_argument("--reasons", default=",".join(DEFAULT_REASONS),
                    help="comma-separated substrings of the extractor's reason to act on")
    ap.add_argument("--max-files", type=int, default=None, help="stop after this many files")
    ap.add_argument("--pace", type=float, default=6.0, help="seconds between requests")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be read; call nothing")
    args = ap.parse_args(argv)

    # One env var per provider, so a key is never passed on a command line where it would
    # end up in a process list and a CI log.
    _, needs_key, _, _ = PROVIDERS[args.provider]
    api_key = os.environ.get("%s_API_KEY" % args.provider.upper()) or \
        os.environ.get("ASSIST_API_KEY")
    if needs_key and not api_key and not args.dry_run:
        print("provider %r needs %s_API_KEY and it is not set. This lane is optional: "
              "without it the pack is still complete, its scans simply stay unread — or "
              "use the default provider, which needs no key at all."
              % (args.provider, args.provider.upper()), file=sys.stderr)
        return 3

    try:
        doc = run(args.pack, model=args.model, api_key=api_key, provider=args.provider,
                  reasons=tuple(r.strip() for r in args.reasons.split(",") if r.strip()),
                  limit=args.max_files, pace=args.pace, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 3

    print("%s lane · %d read (%d calls) · %d deferred · %d skipped · %s chars"
          % (doc["provider"], doc["read"], doc["requests_spent"], doc["deferred"],
             doc["skipped"], f"{doc['chars']:,}"))
    for s in doc["skipped_files"][:10]:
        print("   skipped  %-52s %s" % ((s.get("file") or "?")[-52:], s["skipped"]))
    # Deferred work is not an error: the cache means the next run resumes, and a pack whose
    # scans are unread is exactly as complete as it was before this step existed.
    return 0


if __name__ == "__main__":
    sys.exit(main())
