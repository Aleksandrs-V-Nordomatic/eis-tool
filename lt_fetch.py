"""Transport for Lithuania: one procurement, whole, into a home.

`eis_fetch` is 640 lines because EIS resists — a third of runner addresses are refused, the
documents come one at a time behind a per-record archive negotiation, and the pacing,
retries and address lottery that make it work are most of the file. EPPS does not resist.
The whole tender is one archive from one anonymous address, so this file is short, and it
is short for a reason worth writing down rather than for lack of care.

WHAT IS THE SOURCE OF TRUTH ABOUT WHAT A TENDER CONTAINS. The archive, not the catalogue.
Measured on 9320336: the document list showed eight rows, the archive held eleven members —
among them `Klausimai-atsakymai.docx`, the buyer's answers to suppliers' questions, which is
exactly the kind of document that changes a decision. So members are enumerated from the
archive and the catalogue is joined onto them for what only it knows: the amendment number
that placed a document there, and the address a person clicks. A member the catalogue does
not list is delivered anyway, marked `catalogued: false`, because a document nobody listed
still exists and silence about it is the failure this tool is written against.

MEMBERS CARRY AN ORDINAL PREFIX — `3_3. SS 1 priedas. Technine_specifikacija (TS).docx` —
which is the portal's ordering, not part of the name. It is stripped for matching and kept
in `member` so the archive can be addressed again without guessing.

The output is `eis-tool`'s own delivery shape, so `changes.fingerprint`, `normalize` and the
delivery read it without knowing which country it came from.

    python3 lt_fetch.py 9320336 --out work/LT
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile

import lt_page

try:
    import changes as changes_mod
except Exception:                          # the fingerprint is optional for a bare fetch
    changes_mod = None

_ORDINAL = re.compile(r"^\d+_")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _safe(name):
    """A member name that cannot climb out of the home it is written into."""
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f]", "", name).strip().lstrip(".")
    return name or "unnamed"


def archive(pid, timeout=300):
    """The whole tender, one request. Returns the bytes and what the portal called them."""
    request = urllib.request.Request(lt_page.ARCHIVE % pid,
                                     headers={"User-Agent": lt_page.UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        disposition = response.headers.get("content-disposition") or ""
        kind = response.headers.get("content-type") or ""
    name = re.search(r'filename="?([^";]+)', disposition)
    if not data.startswith(b"PK"):
        # EPPS answers 200 with its login form for a resource it will not serve, so the
        # status line proves nothing and the magic number is the check that does.
        raise RuntimeError("lt_fetch: %s did not answer with an archive (%s, %d bytes)"
                           % (pid, kind.split(";")[0], len(data)))
    return data, (name.group(1) if name else "%s.zip" % pid)


def unpack(data, home, notice):
    """Every member to `originals/`, joined to the catalogue where the catalogue knows it."""
    catalogue = {}
    for document in (notice or {}).get("documents", []):
        catalogue[(document.get("filename") or "").casefold()] = document

    originals = os.path.join(home, "originals")
    os.makedirs(originals, exist_ok=True)
    records, seen = [], set()
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        for member in bundle.namelist():
            if member.endswith("/"):
                continue
            payload = bundle.read(member)
            bare = _ORDINAL.sub("", member.replace("\\", "/").split("/")[-1])
            filename = _safe(bare)
            # Two members can reduce to one name once the ordinal is gone.
            stem, dot, ext = filename.rpartition(".")
            n = 1
            while filename.casefold() in seen:
                n += 1
                filename = "%s (%d)%s%s" % (stem or filename, n, dot, ext)
            seen.add(filename.casefold())

            with open(os.path.join(originals, filename), "wb") as fh:
                fh.write(payload)
            entry = catalogue.get(bare.casefold())
            records.append({
                "id": (entry or {}).get("doc_id") or "member:%s" % member,
                "title": (entry or {}).get("title") or bare,
                "type_code": (entry or {}).get("amendment"),
                "section": "current",
                "publish_date": None,
                "catalogued": entry is not None,
                "member": member,
                "download": (entry or {}).get("download"),
                "files": [{"filename": filename,
                           # `path` is what the extractor opens, relative to the home.
                           "path": "originals/%s" % filename,
                           "original_name": bare,
                           "sha256": _sha256(payload),
                           "bytes": len(payload)}],
            })
    return records


def split_generated(records):
    """Buyer documents apart from what the portal generated for itself.

    MEASURED, AND IT WOULD HAVE COST A FALSE ALARM EVERY NIGHT: the archive carries the
    portal's own rendering of the notice — `<buyer>_<pid>.pdf` and the contract-notice PDF —
    and those come back with a DIFFERENT sha256 on every request, because each is produced
    fresh and stamped with the moment it was made. Compared like a buyer's document, they
    report the tender as changed every single night, forever, and the one update a person
    actually needs to read drowns among them.

    The test is the catalogue, not the filename. Every document a buyer published is a row
    in `listContractDocuments`; the portal's artefacts are not. So an uncatalogued member is
    delivered and listed — a person may well want the notice as a PDF — and left out of the
    fingerprint, the way the Latvian side delivers its OCR lane without comparing it.
    """
    documents = [r for r in records if r.get("catalogued")]
    generated = [r for r in records if not r.get("catalogued")]
    return documents, generated


def extract(home):
    """Turn the originals into Markdown, then give each one its permanent address.

    `normalize.py` is the same code for both countries and knows nothing about either — it
    reads `manifest.json` and writes `normalized/`. What it does not do is name the results
    the way a reader addresses them, so that happens here: `doc/<digest>.md`, where the
    digest is `changes.document_key` over the ORIGINAL bytes and the document's place. An
    unchanged document therefore keeps the same address forever and costs nothing to
    re-deliver, and a superseded one stays readable instead of being overwritten.
    """
    try:
        import normalize
    except ImportError as exc:
        return {"documents": [], "unreadable_files": [],
                "skipped": "normalize unavailable: %s" % exc}

    out = os.path.join(home, "normalized")
    argv = ["--in", home, "--out", out]
    stdout = sys.stdout
    try:
        sys.stdout = io.StringIO()             # the extractor narrates; a fetch does not
        normalize.main(argv)
    finally:
        sys.stdout = stdout

    with open(os.path.join(out, "manifest_normalized.json"), encoding="utf-8") as fh:
        normalized = json.load(fh)

    doc_dir = os.path.join(home, "doc")
    os.makedirs(doc_dir, exist_ok=True)
    for entry in normalized.get("documents", []):
        if entry.get("also_listed_under") or not entry.get("markdown_path"):
            continue
        source = os.path.join(out, entry["markdown_path"])
        if not os.path.exists(source):
            continue
        key = (changes_mod.document_key(entry.get("original_sha256"), entry.get("source"))
               if changes_mod else entry.get("original_sha256", "")[:16])
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(doc_dir, "%s.md" % key), "w", encoding="utf-8") as fh:
            fh.write(text)
        entry["doc"] = "doc/%s.md" % key
    return normalized


def _with_text(documents, normalized):
    """Point each catalogue entry at the Markdown its bytes produced."""
    by_sha = {}
    for entry in normalized.get("documents", []):
        if entry.get("doc"):
            by_sha.setdefault(entry.get("original_sha256"), []).append(entry)
    out = []
    for row in documents:
        texts = by_sha.get(row["sha256"], [])
        out.append(dict(row,
                        doc=texts[0]["doc"] if texts else None,
                        chars=sum(t.get("markdown_chars", 0) for t in texts) or None))
    return out


def fetch(pid, out_root, kind="tender", with_text=True, notice=None):
    """One tender into `<out_root>/tenders/<pid>/`, in the delivery shape.

    `index.json` is written LAST and `state.json` after it, exactly as the Latvian side
    promises: the index existing is a reader's proof that the home is whole, and a
    fingerprint that landed before the documents it vouches for would let the next run skip
    text that is not there.
    """
    notice = notice or lt_page.collect(pid, kind)
    if notice is not None and "documents" not in notice:
        # A caller that gated on the card hands it back; only the catalogue is still owed.
        notice["documents"] = lt_page.parse_documents(lt_page.fetch(lt_page.DOCS % pid), pid)
    if notice is None:
        raise RuntimeError("lt_fetch: %s is not published, or EPPS answered with a login "
                           "form" % pid)

    home = os.path.join(out_root, "tenders", str(pid))
    os.makedirs(home, exist_ok=True)

    data, archive_name = archive(pid)
    with open(os.path.join(home, "%s.zip" % pid), "wb") as fh:
        fh.write(data)

    records = unpack(data, home, notice)
    catalogue = list(notice.pop("documents", []))

    documents, generated = split_generated(records)
    # `documents` is what the fingerprint reads. `generated` rides along in the manifest
    # under a key it does not look at, so the files are delivered without being compared.
    manifest = {"pid": str(pid), "procurement_id": str(pid), "archive": archive_name,
                "archive_sha256": _sha256(data), "archive_bytes": len(data),
                "documents": documents, "generated": generated, "withheld_records": []}
    _write(home, "procurement.json", notice)
    _write(home, "manifest.json", manifest)

    index = {
        "schema": "index/1",
        "pid": str(pid),
        "country": "LT",
        "kind": kind,
        "source": "EPPS",
        "link": notice.get("link"),
        "archive": notice.get("archive"),
        "title": notice.get("title"),
        "buyer": notice.get("buyer"),
        "deadline": notice.get("deadline"),
        "cpv_main": notice.get("cpv_main"),
        # What the catalogue said and what the archive held, kept apart on purpose: a gap
        # between them is a fact about the delivery, not a rounding error.
        "catalogued_documents": len(catalogue),
        "generated_documents": len(generated),
        "documents": [{
            "id": r["id"],
            "name": r["files"][0]["original_name"],
            "sha256": r["files"][0]["sha256"],
            "bytes": r["files"][0]["bytes"],
            "original": "originals/%s" % r["files"][0]["filename"],
            "amendment": r["type_code"],
            "catalogued": r["catalogued"],
            "download": r["download"],
        } for r in records],
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write(home, "index.json", index)

    normalized = extract(home) if with_text else {"documents": [], "unreadable_files": []}
    if normalized.get("documents"):
        index["documents"] = _with_text(index["documents"], normalized)
        index["text_documents"] = sum(1 for e in normalized["documents"]
                                      if e.get("markdown_path"))
        index["chars"] = normalized.get("chars", 0)
        _write(home, "index.json", index)          # rewritten with the text it now names

    state = None
    if changes_mod is not None:
        state = changes_mod.fingerprint(pid, notice, manifest, normalized)
        _write(home, "state.json", state)
    return {"pid": str(pid), "kind": kind, "home": home,
            "documents": len(records), "catalogued": len(catalogue),
            "uncatalogued": sum(1 for r in records if not r["catalogued"]),
            "bytes": len(data), "index": index, "state": state,
            "title": notice.get("title"), "buyer": notice.get("buyer"),
            "deadline": notice.get("deadline"), "cpv_main": notice.get("cpv_main")}


def _write(home, name, payload):
    with open(os.path.join(home, name), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def main(argv=None):
    ap = argparse.ArgumentParser(description="One Lithuanian procurement into a home.")
    ap.add_argument("pid")
    ap.add_argument("--out", default="work/LT")
    ap.add_argument("--consultation", action="store_true")
    args = ap.parse_args(argv)
    done = fetch(args.pid, args.out, "consultation" if args.consultation else "tender")
    print("%s: %d document(s), %d not in the catalogue, %.1f MB -> %s"
          % (done["pid"], done["documents"], done["uncatalogued"],
             done["bytes"] / 1048576.0, done["home"]))
    return done


if __name__ == "__main__":
    main()
