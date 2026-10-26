"""What one tender folder in the library holds, built from a converted pack.

    tenders/<pid>/
      procurement.json, manifest.json        facts and the files the batch carried
      normalized/manifest_normalized.json    what was converted, and how
      doc/<key>.md                           one Markdown file per document
      structure.json                         Word numbering, when a document had any
      index.json                             what is here, the skipped files, written last
      <pid>.zip                              the whole folder in one file
      state.json, seen.json, runs/<date>.json
"""

import io
import json
import os
import urllib.parse
import zipfile

import changes

KEEP_NAMES = {"manifest.json", "procurement.json", "manifest_normalized.json", "document.md",
              "structure.json"}
EIS_DOCUMENT = "https://www.eis.gov.lv/EKEIS/Document/%s?%s"


def load_json(pack, *rel):
    path = os.path.join(pack, *rel)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def zip_write(zf, name, data):
    info = zipfile.ZipInfo(name.replace("\\", "/"), (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def tender_members(pack_files, pid, structures, entry_bytes):
    """Everything one tender publishes, in order; `index.json` last."""
    members = sorted(pack_files)
    if structures:
        members.append(("structure.json",
                        json.dumps({"schema": "structure/1", "pid": pid, "documents": structures},
                                   ensure_ascii=False).encode("utf-8")))
    members.append(("index.json", entry_bytes))
    return members


def tender_archive(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", allowZip64=True) as zf:
        for rel, data in members:
            zip_write(zf, rel, data)
    return out.getvalue()


def selection(pack):
    """(relative path, absolute path) of every file of the pack a reader needs."""
    for root, _, names in os.walk(pack):
        for name in sorted(names):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, pack).replace(os.sep, "/")
            if name in KEEP_NAMES and not rel.startswith("normalized/_unpacked/"):
                yield rel, path


def flatten(pack):
    """(files as (path, bytes), normalized manifest, structures) with documents under doc/.

    A document is named by `changes.document_key`, from its bytes and its source, so an
    unchanged document keeps its address from one batch to the next.
    """
    out, renamed, manifest_rel, manifest = [], {}, None, None
    docs, structures = [], {}
    for rel, path in selection(pack):
        if rel.endswith("/document.md") or rel == "document.md":
            docs.append((rel, path))
        elif rel.endswith("manifest_normalized.json"):
            manifest_rel = rel
            with open(path, "rb") as fh:
                manifest = json.loads(fh.read().decode("utf-8"))
        elif os.path.basename(rel) == "structure.json":
            with open(path, "rb") as fh:
                structures[os.path.dirname(rel)] = fh.read()
        else:
            with open(path, "rb") as fh:
                out.append((rel, fh.read()))

    key_of = {}
    for entry in (manifest or {}).get("documents", []):
        mp = entry.get("markdown_path")
        if not mp or entry.get("also_listed_under"):
            continue
        key_of["normalized/" + mp.lstrip("/")] = changes.document_key(
            entry.get("original_sha256"), entry.get("source"))

    for rel, path in sorted(docs):
        short = "doc/%s.md" % (key_of.get(rel) or changes.document_key("", rel))
        renamed[rel] = short
        with open(path, "rb") as fh:
            out.append((short, fh.read()))

    if manifest is not None:
        for entry in manifest.get("documents", []):
            mp = entry.get("markdown_path")
            was = "normalized/" + mp.lstrip("/") if mp else None
            if was in renamed:
                entry["markdown_path"] = renamed[was]
        out.append((manifest_rel, json.dumps(manifest, ensure_ascii=False).encode("utf-8")))

    merged = {}
    for directory, blob in structures.items():
        short = renamed.get(directory + "/document.md")
        if not short:
            continue
        try:
            merged[short] = json.loads(blob.decode("utf-8"))
        except ValueError:
            continue
    return out, manifest, merged


def ocr_documents(pack):
    """[(index line, bytes)] for the scans text recognition read, from `llm/`."""
    lane = load_json(pack, "llm", "manifest_llm.json") or {}
    out = []
    for doc in lane.get("documents", []):
        path = os.path.join(pack, doc["markdown"])
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        out.append(({"path": "doc/ocr_%s.md" % doc["sha256"][:16], "file": doc["file"],
                     "chars": doc["chars"], "reader": lane.get("extraction")}, data))
    return out


def download_url(pid, downloaded, entry):
    """The EIS address of the file a document came from, or None when it cannot be placed."""
    record = next((r for r in (downloaded or {}).get("documents") or []
                   if str(r.get("id")) == str(entry.get("record_id"))
                   and r.get("section") == entry.get("section")), None)
    if record is None:
        return None
    files = [f for f in record.get("files") or [] if f.get("filename") == entry.get("original_file")]
    if not files:
        return None
    file_id = files[0].get("file_id")
    route = "DownloadDocumentFile" if file_id is not None else "DownloadDocumentFilesInZip"
    params = [("Id", record.get("id"))]
    if file_id is not None:
        params.append(("FileId", file_id))
    params += [("DocumentLinkTypeCode", record.get("document_link_type_code") or "PRCDOC"),
               ("ProcurementIdentifier", pid)]
    return EIS_DOCUMENT % (route, urllib.parse.urlencode(params))


def index_entry(pid, pack, manifest):
    """The tender's index: its facts, its documents with their texts, the files not read."""
    proc = load_json(pack, "procurement.json") or {}
    downloaded = load_json(pack, "manifest.json") or {}
    docs = []
    for e in (manifest or {}).get("documents", []):
        mp = e.get("markdown_path")
        if not mp or e.get("also_listed_under"):
            continue
        doc = {"path": mp.lstrip("/"),
               "name": os.path.basename(e.get("source") or ""),
               "source": e.get("source"),
               "section": e.get("section"),
               "record": e.get("record_title"),
               "chars": e.get("markdown_chars")}
        original = next((f for r in downloaded.get("documents", []) for f in r.get("files", [])
                         if f.get("filename") == e.get("original_file")), None)
        if original:
            doc["kind"] = original.get("kind")
            if original.get("archive"):
                doc["archive"] = original["archive"]
                doc["member"] = original.get("member")
        url = download_url(pid, downloaded, e)
        if url:
            doc["download"] = url
        docs.append(doc)

    return {
        "pid": pid,
        "key": "EIS:%s" % pid,
        "title": proc.get("title"),
        "buyer": proc.get("buyer"),
        "buyer_reg": proc.get("buyer_reg"),
        "deadline": proc.get("deadline"),
        "value": proc.get("value"),
        "currency": proc.get("currency"),
        "cpv": proc.get("cpv_main") or proc.get("cpv"),
        "cpv_additional": proc.get("cpv_additional"),
        "ref": proc.get("ref"),
        "link": proc.get("link"),
        "iub_uuid": proc.get("iub_uuid"),
        "procedure": proc.get("procedure"),
        "profile": proc.get("profile"),
        "work_kind": proc.get("work_kind"),
        "documents": docs,
        "unreadable": [{"file": g.get("file"), "reason": g.get("reason")}
                       for g in (manifest or {}).get("unreadable_files", [])],
        "skipped": downloaded.get("skipped", []),
    }
