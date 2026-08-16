#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deliver a pack tree to a Microsoft Graph drive, so a consumer that cannot fetch artifacts
can still read the day. Each tender is one ZIP rather than hundreds of SharePoint items;
its small index is also delivered beside it for consumers that choose before downloading.

    python3 deliver_graph.py --packs packs --shard 1 --date 2026-08-10

WHY THIS EXISTS. A GitHub artifact's metadata is served by api.github.com but its bytes are
a 302 to `*.blob.core.windows.net` — a different host, from a rotating pool. A consumer
allowed to reach the first and not the second can list an artifact, see its size, see that
it has not expired, and download none of it. That is not a permissions problem anyone can
grant their way out of; the bytes have to arrive somewhere the consumer may already talk to.

WHAT THIS KNOWS ABOUT THE DESTINATION: NOTHING. Tenant, client, drive and path all arrive in
the environment. This file names no organisation, no site and no folder, and it prints
counts rather than paths, so neither the repository nor the run log says where the day went.
The one thing it cannot hide is the TLS connection itself: a runner talking to Graph resolves
`login.microsoftonline.com` and `graph.microsoft.com`, and whoever can watch the runner's
network sees that much. Scope the credential to one site (`Sites.Selected`) and a leak buys
the reader that site and nothing else.

WHY THE DOCUMENT PATHS ARE FLATTENED. Paths inside a pack run deep — an archive nested a
few levels holds documents whose relative path is already hundreds of characters — and a
SharePoint destination prefix adds its own on top of a hard limit for the whole
server-relative path. A minority of a day's files therefore fail to upload while the rest
succeed, and a delivery that loses a fraction of itself and reports success is the failure
mode this pipeline exists to refuse. So `normalized/<deep>/<path>/document.md` is written
as `normalized/n/<NNNN>.md` and `manifest_normalized.json` is rewritten to match.

That rewrite is safe because of how a consumer reads: it opens `entry["markdown_path"]`
and never parses its shape, while the name a person sees and cites is `entry["source"]`,
which is left exactly as the carrier wrote it.

PARTIAL SUCCESS IS FAILURE, here as everywhere else in this tool. Any file that does not
land fails the run, because a day that looks delivered and is not costs more than one that
plainly broke.
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com/%s/oauth2/v2.0/token"

# Everything a consumer reads, and nothing it does not. The originals, the unpacked media
# and the drawing binaries stay in the artifact: they are 97% of the bytes and no reader
# opens them.
KEEP_NAMES = {
    "manifest.json", "summary.json", "procurement.json",
    "manifest_normalized.json", "document.md",
    "structure.json",
    "done.txt", "failed.txt", "resolved.tsv",
}
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
# Microsoft Graph requires non-final upload fragments to be a multiple of 320 KiB.
# 32 such blocks are 10 MiB: efficient and far below Graph's fragment ceiling.
UPLOAD_CHUNK = 32 * 320 * 1024


def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit("missing environment: %s" % name)
    return v


def token(tenant, client_id, client_secret):
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(LOGIN % tenant, data=body)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["access_token"]


def put(url, data, tok, content_type="application/octet-stream", tries=5):
    """One upload, retried on the throttling and transient codes Graph actually returns.

    Graph answers 429 with Retry-After under sustained writes, and this delivery is a couple
    of thousand of them in a row. Honouring the header is the difference between a delivery
    that finishes and one that half-finishes.
    """
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method="PUT")
        req.add_header("Authorization", "Bearer " + tok)
        req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                wait = int(e.headers.get("Retry-After") or (2 ** attempt))
                time.sleep(min(wait, 60))
                continue
            # The body can quote the destination path; report the code only.
            raise SystemExit("upload failed: HTTP %d after %d attempt(s)"
                             % (e.code, attempt + 1))
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit("upload failed: transport error after %d attempts" % tries)
    raise SystemExit("upload failed: retries exhausted")


def upload_stream(drive, dest, stream, size, tok):
    """PUT one seekable stream without keeping a day-sized archive in memory."""
    safe = "/".join(urllib.parse.quote(p, safe="") for p in dest.split("/"))
    if size < SIMPLE_UPLOAD_LIMIT:
        data = stream.read()
        put("%s/drives/%s/root:/%s:/content" % (GRAPH, drive, safe), data, tok)
        return

    req = urllib.request.Request(
        "%s/drives/%s/root:/%s:/createUploadSession" % (GRAPH, drive, safe),
        data=json.dumps({"item": {"@microsoft.graph.conflictBehavior": "replace"}}).encode(),
        method="POST")
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        url = json.load(r)["uploadUrl"]

    offset = 0
    while offset < size:
        data = stream.read(min(UPLOAD_CHUNK, size - offset))
        if not data:
            raise SystemExit("upload session failed: source ended at %d of %d bytes"
                             % (offset, size))
        end = offset + len(data) - 1
        for attempt in range(5):
            creq = urllib.request.Request(url, data=data, method="PUT")
            creq.add_header("Content-Range", "bytes %d-%d/%d" % (offset, end, size))
            try:
                with urllib.request.urlopen(creq, timeout=600) as r:
                    if r.status not in (200, 201, 202):
                        raise SystemExit("upload session failed: HTTP %d" % r.status)
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    wait = int(e.headers.get("Retry-After") or (2 ** attempt))
                    time.sleep(min(wait, 60))
                    continue
                raise SystemExit("upload session failed: HTTP %d after %d attempt(s)"
                                 % (e.code, attempt + 1))
            except urllib.error.URLError:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit("upload session failed: transport error after 5 attempts")
        offset = end + 1


def upload(drive, dest, data, tok):
    """PUT one in-memory file. Paths are escaped, and destinations never reach stdout."""
    upload_stream(drive, dest, io.BytesIO(data), len(data), tok)


def upload_file(drive, dest, path, tok):
    """PUT one file from disk, streaming upload-session chunks when it is large."""
    with open(path, "rb") as fh:
        upload_stream(drive, dest, fh, os.path.getsize(path), tok)


def zip_write(zf, name, data):
    """One deterministic ZIP member with a portable path and permissions."""
    info = zipfile.ZipInfo(name.replace("\\", "/"), (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def tender_archive(pack_files, pid, structures, entry_bytes):
    """The former tender folder as one ZIP; `index.json` is deliberately last."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", allowZip64=True) as zf:
        for rel, data in sorted(pack_files):
            zip_write(zf, rel, data)
        if structures:
            sidecar = json.dumps({"schema": "structure/1", "pid": pid,
                                  "documents": structures},
                                 ensure_ascii=False).encode("utf-8")
            zip_write(zf, "structure.json", sidecar)
        zip_write(zf, "index.json", entry_bytes)
    return out.getvalue()


def selection(pack):
    """(relative path, absolute path) for everything a consumer reads in one pack."""
    for root, _, names in os.walk(pack):
        for n in sorted(names):
            ap = os.path.join(root, n)
            rel = os.path.relpath(ap, pack).replace(os.sep, "/")
            if n in KEEP_NAMES or "/precis/" in "/" + rel:
                yield rel, ap


def flatten(pack):
    """Short names for the deep ones, plus the manifest rewritten to match.

    Returns (list of (destination-relative path, bytes)) for one pack. The manifest is
    emitted from memory rather than from disk precisely because its `markdown_path` values
    must agree with where the files actually land.
    """
    out, renamed, manifest_rel, manifest = [], {}, None, None
    docs, structures = [], {}
    for rel, ap in selection(pack):
        if rel.endswith("/document.md") or rel == "document.md":
            docs.append((rel, ap))
        elif rel.endswith("manifest_normalized.json"):
            manifest_rel = rel
            with open(ap, "rb") as fh:
                manifest = json.loads(fh.read().decode("utf-8"))
        elif os.path.basename(rel) == "structure.json":
            # Held back, not uploaded where it lies. One sidecar per document would add a PUT
            # per readable Word file — hundreds a day against a delivery that already retries
            # on 429 — so they are merged into one file at the tender's root below. Keyed by
            # the directory, because that is what a sidecar shares with its document.
            with open(ap, "rb") as fh:
                structures[os.path.dirname(rel)] = fh.read()
        else:
            with open(ap, "rb") as fh:
                out.append((rel, fh.read()))

    for i, (rel, ap) in enumerate(sorted(docs)):
        short = "normalized/n/%04d.md" % i
        renamed[rel] = short
        with open(ap, "rb") as fh:
            out.append((short, fh.read()))

    if manifest is not None:
        for entry in manifest.get("documents", []):
            mp = entry.get("markdown_path")
            if not mp:
                continue
            was = "normalized/" + mp.lstrip("/")
            if was in renamed:
                # `source` is untouched: it is what a person opens and what a citation names.
                entry["markdown_path"] = renamed[was][len("normalized/"):]
        out.append((manifest_rel,
                    json.dumps(manifest, ensure_ascii=False).encode("utf-8")))
    # The merged sidecar keys on the FLATTENED document name, because that is the only name a
    # reader ever sees on the drive. A structure whose document did not survive selection is
    # dropped rather than delivered pointing at nothing.
    merged = {}
    for directory, blob in structures.items():
        short = renamed.get(directory + "/document.md")
        if not short:
            continue
        try:
            merged[short] = json.loads(blob.decode("utf-8"))
        except ValueError:
            continue

    # `renamed` goes back with the rest because the precis sidecar keys on the ORIGINAL
    # markdown path, and by the time anything else sees this manifest the path has been
    # rewritten. Joining without it silently yields a precis for nothing.
    return out, manifest, renamed, merged


def load_json(pack, *rel):
    p = os.path.join(pack, *rel)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def index_entry(pid, pack, manifest, renamed=None):
    """One tender: what is here, and what it is worth opening.

    Delivered twice — once inside the tender as its own `index.json`, and once as a line in
    the shard index. The shard index is how a reader learns which tenders exist; the copy
    inside the tender is what it opens when it judges one, so that reading about ten
    documents does not cost reading about six hundred.

    THIS IS THE WHOLE POINT OF THE DELIVERY, AND IT IS A TINY FRACTION OF IT. A day of
    extracted text runs to tens of millions of characters; no reader holds a hundredth of
    that in one context window. This index is a few hundred kilobytes for a whole day. So
    the reader reads the index, decides which documents are worth the window, and opens
    only those.

    Everything here is the carrier's own data — the normalized manifest, the precis sidecar
    and the procurement page. No judgement is made and none can be: which tenders matter is
    the consumer's business and never travels through this repository.
    """
    proc = load_json(pack, "procurement.json") or {}
    precis = load_json(pack, "precis", "manifest_precis.json") or {}

    # The sidecar keys on the ORIGINAL markdown path and the manifest now carries the
    # flattened one, so the join runs through `renamed`. An absent precis means "unknown",
    # never "not relevant".
    renamed = renamed or {}
    by_flat = {}
    for e in (precis.get("documents") or precis.get("entries") or []):
        orig = e.get("markdown_path")
        if not orig or not e.get("precis"):
            continue
        flat = renamed.get("normalized/" + orig.lstrip("/"))
        if flat:
            by_flat[flat[len("normalized/"):]] = e["precis"]

    docs = []
    for e in (manifest or {}).get("documents", []):
        mp = e.get("markdown_path")
        if not mp or e.get("also_listed_under"):
            continue
        docs.append({
            "path": "normalized/" + mp.lstrip("/"),   # already flattened; open this
            "name": os.path.basename(e.get("source") or ""),
            "source": e.get("source"),                # the real path, for a citation
            "section": e.get("section"),
            "record": e.get("record_title"),
            "chars": e.get("markdown_chars"),
            "precis": by_flat.get(mp.lstrip("/")),
        })

    return {
        "pid": pid,
        "key": "EIS:%s" % pid,
        "title": proc.get("title"),
        "buyer": proc.get("buyer"),
        "buyer_reg": proc.get("buyer_reg"),
        "deadline": proc.get("deadline"),
        "value": proc.get("value"),
        "currency": proc.get("currency"),
        "cpv": proc.get("cpv"),
        "ref": proc.get("ref"),
        "link": proc.get("link"),
        "iub_uuid": proc.get("iub_uuid"),
        # How it is bought and what is bought, in the buyer's own words. Both are read off the
        # page, so a consumer showing them to a person is quoting rather than deciding.
        #
        # `profile` rides along because it is the only one of the three that does not change
        # language: EIS serves some pages in English, and the same tender then says
        # `Construction works` where another says `Būvdarbi`. A column keyed on the display
        # string quietly grows two labels for one thing; `PIL_Atklāts_konkurss` is stable
        # whatever language the page was served in.
        "procedure": proc.get("procedure"),
        "profile": proc.get("profile"),
        "work_kind": proc.get("work_kind"),
        "documents": docs,
        # A file nobody could decode is not an absent file, and the reader has to know.
        "unreadable": [
            {"file": g.get("file"), "reason": g.get("reason")}
            for g in (manifest or {}).get("unreadable_files", [])
        ],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deliver a pack tree to a Graph drive.")
    ap.add_argument("--packs", required=True, help="directory holding <pid>/ pack folders")
    ap.add_argument("--shard", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD, the run's own date")
    args = ap.parse_args(argv)

    drive = env("GRAPH_DRIVE_ID")
    base = env("GRAPH_DEST_ROOT").strip("/")
    tok = token(env("GRAPH_TENANT_ID"), env("GRAPH_CLIENT_ID"), env("GRAPH_CLIENT_SECRET"))

    root = "%s/%s/shards/eis-batch-shard-%s" % (base, args.date, args.shard)
    files = bytes_sent = members = 0

    # The shard's own arithmetic rides at the top, beside the packs, exactly as it sits in
    # the artifact: `collect` reads it to prove a whole day arrived.
    for name in ("done.txt", "failed.txt", "resolved.tsv"):
        p = os.path.join(args.packs, name)
        if os.path.exists(p):
            with open(p, "rb") as fh:
                data = fh.read()
            upload(drive, "%s/%s" % (root, name), data, tok)
            files += 1
            bytes_sent += len(data)

    index = {"date": args.date, "shard": args.shard, "tenders": []}
    for pid in sorted(os.listdir(args.packs)):
        pack = os.path.join(args.packs, pid)
        if not os.path.isdir(pack):
            continue
        pack_files, manifest, renamed, structures = flatten(pack)
        # One file per tender, keyed by the flattened document name — what Word keeps as a
        # paragraph property rather than as text, so a consumer can rebuild a clause number
        # instead of counting paragraphs and being wrong. Absent when nothing in the tender
        # was a numbered Word document, which is the ordinary case for a pack of PDFs.

        entry = index_entry(pid, pack, manifest, renamed)
        archive_name = "%s.zip" % pid
        index_name = "%s.index.json" % pid
        entry["archive"] = archive_name
        entry["index_file"] = index_name

        # THE SAME LINE AGAIN, ONE TENDER WIDE, INSIDE THE TENDER — because of who reads it.
        # A reader that judges tender by tender opens one tender at a time, and making it
        # pull the whole shard index to find one entry costs 4k-30k tokens to learn about
        # documents it will not open. Its own copy is a few hundred bytes to a few thousand.
        # The shard index stays: it is what enumerates the tenders in the first place.
        #
        # It goes after that tender's files, for the same reason the shard index goes last:
        # an index that exists was written after every document it names.
        entry_bytes = json.dumps(dict(entry, date=args.date, shard=args.shard),
                                 ensure_ascii=False).encode("utf-8")
        archive_bytes = tender_archive(pack_files, pid, structures, entry_bytes)
        upload(drive, "%s/%s" % (root, archive_name), archive_bytes, tok)
        upload(drive, "%s/%s" % (root, index_name), entry_bytes, tok)
        files += 2
        members += len(pack_files) + 1 + (1 if structures else 0)
        bytes_sent += len(archive_bytes) + len(entry_bytes)


        index["tenders"].append(entry)

    # THE INDEX GOES LAST, ON PURPOSE. Its presence is the reader's proof that this shard
    # arrived whole: a delivery that died halfway leaves documents without an index, and an
    # index that exists was written after every document it names.
    index_bytes = json.dumps(index, ensure_ascii=False).encode("utf-8")
    upload(drive, "%s/index.json" % root, index_bytes, tok)
    files += 1
    bytes_sent += len(index_bytes)

    # Counts, never the destination.
    print("delivered shard %s for %s: %d SharePoint files, %.1f MB, %d ZIP members "
          "(index: %d tenders, %.0f KB)"
          % (args.shard, args.date, files, bytes_sent / 1e6, members,
             len(index["tenders"]), len(index_bytes) / 1e3))
    return 0


if __name__ == "__main__":
    sys.exit(main())
