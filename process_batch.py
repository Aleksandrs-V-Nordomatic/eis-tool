"""Tender document processing: one handed-over batch, on a GitHub runner.

    python process_batch.py --batch-id LV-20261026-0001

Reads the batch from the staging folder of the library, checks it against its manifest,
converts each tender's documents to text, publishes the tender folders, writes the day files
and the completion record, removes the staged batch and starts the analysis routine.
It never connects to IUB or EIS.

Environment: GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_DRIVE_ID,
GRAPH_DEST_ROOT (the folder holding the country folders), and optionally ROUTINE_FIRE_URL
and ROUTINE_FIRE_TOKEN.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

import changes
import country
import eis_fetch
import graph
import publish

FIRE_TRIES = 3
OCR_FILES = 40


def say(line):
    print(line, flush=True)


class Incomplete(Exception):
    """The staged batch does not match its manifest."""


def check(drive, inbox, manifest, work):
    """Download every kept file of the batch and compare it with the manifest."""
    for tender in manifest["tenders"]:
        if tender["status"] != "downloaded":
            continue
        folder = os.path.join(work, tender["pid"], "documents", "actual")
        os.makedirs(folder, exist_ok=True)
        for f in tender["files"]:
            if f["decision"] != "download":
                continue
            local = os.path.join(folder, f["stored"].split("/", 1)[1])
            if not drive.download("%s/%s" % (inbox, f["stored"]), local):
                raise Incomplete("a kept file of tender %s is missing" % tender["pid"])
            if os.path.getsize(local) != f["size"] or eis_fetch.sha256_file(local) != f["sha256"]:
                raise Incomplete("a kept file of tender %s differs from the manifest" % tender["pid"])


RECORD_FIELDS = ("id", "section", "title", "document_link_type_code", "type_title", "type_code",
                 "publish_date")


def pack_manifest(tender):
    """The tender's files in the shape the converter and the change comparison read.

    Every record is listed; a record's kept files are under `files`, the ones not downloaded
    under `skipped_files`, so a new skipped file still shows as a change of its record.
    """
    documents = {}
    for r in tender["records"]:
        if r.get("downloadable", True):
            documents[str(r["id"])] = dict({k: r.get(k) for k in RECORD_FIELDS},
                                           files=[], skipped_files=[])
    skipped = []
    for f in tender["files"]:
        record = documents.setdefault(str(f["record_id"]), {
            "id": f["record_id"], "section": f["section"], "title": "", "files": [],
            "skipped_files": []})
        if f["decision"] == "download":
            name = f["stored"].split("/", 1)[1]
            record["files"].append({"filename": name, "original_name": f["name"],
                                    "path": "documents/actual/%s" % name, "size": f["size"],
                                    "sha256": f["sha256"], "file_id": f["file_id"],
                                    "kind": f["kind"], "archive": f["archive"],
                                    "member": f["member"]})
        elif f["decision"] == "skip":
            item = {"name": f["member"] or f["name"], "archive": f["archive"],
                    "kind": f["kind"], "record": record.get("title"),
                    "section": f["section"], "reason": f["reason"], "file_id": f["file_id"]}
            record["skipped_files"].append(item)
            skipped.append(item)
    withheld = [{k: r.get(k) for k in RECORD_FIELDS} for r in tender["records"]
                if not r.get("downloadable", True)]
    return {"schema": 3, "procurement_id": tender["pid"], "source_url": tender.get("url"),
            "documents": [documents[k] for k in sorted(documents)],
            "withheld_records": withheld, "skipped": skipped}


def convert(work, tender):
    """Build the pack and convert it. Returns the pack folder."""
    pack = os.path.join(work, tender["pid"])
    with open(os.path.join(pack, "procurement.json"), "w", encoding="utf-8") as fh:
        json.dump(tender.get("procurement") or {}, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(pack_manifest(tender), fh, ensure_ascii=False, indent=2)
    import normalize
    normalize.main(["--in", pack, "--out", os.path.join(pack, "normalized"), "--keep-unpacked"])
    read_scans(pack)
    return pack


def read_scans(pack):
    """Text recognition over the files no decoder could read. Never fails the tender."""
    import assist
    try:
        assist.run(pack, limit=OCR_FILES)
    except Exception as exc:
        say("  scans not read: %s" % type(exc).__name__)
    return 0


def publish_tender(drive, base, pack, pid, date, run_id):
    """Upload one tender folder. Returns (change record, day entry)."""
    files, manifest, structures = publish.flatten(pack)
    recognised = publish.ocr_documents(pack)
    files += [(doc["path"], data) for doc, data in recognised]
    home = "tenders/%s" % pid
    root = "%s/%s" % (base, home)
    current = changes.fingerprint(pid, publish.load_json(pack, "procurement.json"),
                                  publish.load_json(pack, "manifest.json"), manifest,
                                  tool=changes.pipeline_version(), parser=changes.parser_version())
    was_seen = drive.read_json(root + "/seen.json")
    record = changes.diff(drive.read_json(root + "/state.json"), current, date=date,
                          run_id=run_id, seen=was_seen)
    record["home"] = home
    entry = publish.index_entry(pid, pack, manifest)
    for field in ("title", "buyer", "deadline"):
        record[field] = entry.get(field)
    entry.update({"home": home, "archive": "%s.zip" % pid, "index_file": "index.json",
                  "run_file": "runs/%s.json" % date,
                  "recognised": [doc for doc, _ in recognised]})
    contents = publish.tender_members(files, pid, structures,
                                      json.dumps(dict(entry, date=date), ensure_ascii=False)
                                      .encode("utf-8"))
    if changes.refreshed(record):
        wanted = {"doc/%s.md" % k for k in changes.documents_to_send(record, current)}
        wanted |= {doc["path"] for doc, _ in recognised}
        drive.upload("%s/%s.zip" % (root, pid), publish.tender_archive(contents))
        for rel, data in contents:
            if not rel.startswith("doc/") or rel in wanted:
                drive.upload("%s/%s" % (root, rel), data)
        drive.upload(root + "/state.json", json.dumps(current, ensure_ascii=False).encode("utf-8"))
    drive.upload(root + "/seen.json", json.dumps(changes.seen(pid, was_seen, record, date),
                                                 ensure_ascii=False).encode("utf-8"))
    drive.upload("%s/runs/%s.json" % (root, date),
                 json.dumps(record, ensure_ascii=False).encode("utf-8"))
    return record, entry


def fire(batch_id):
    """Start the analysis routine for the batch. False leaves it for the scheduled run."""
    url, token = os.environ.get("ROUTINE_FIRE_URL"), os.environ.get("ROUTINE_FIRE_TOKEN")
    if not url or not token:
        say("the routine trigger is not configured; the scheduled run picks the batch up")
        return False
    body = json.dumps({"text": "batch_id=%s" % batch_id}).encode()
    for attempt in range(FIRE_TRIES):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "experimental-cc-routine-2026-04-01"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    say("analysis requested")
                    return True
        except urllib.error.HTTPError as exc:
            say("routine start refused: HTTP %d" % exc.code)
            if exc.code < 500 and exc.code != 429:
                return False
        except OSError as exc:
            say("routine start failed: %s" % type(exc).__name__)
        time.sleep(10 * (attempt + 1))
    return False


def process(drive, base, batch_id, run_id, work):
    inbox = "%s/inbox/%s" % (base, batch_id)
    done = "%s/batches/%s/complete.json" % (base, batch_id)
    if drive.read(done) is not None:
        say("batch %s was processed before" % batch_id)
        return 0
    manifest = drive.read_json(inbox + "/manifest.json")
    if not manifest:
        say("batch incomplete: no manifest")
        return 2
    try:
        check(drive, inbox, manifest, work)
    except Incomplete as exc:
        say("batch incomplete: %s" % exc)
        return 2
    say("batch %s complete: %d tenders" % (batch_id, len(manifest["tenders"])))

    date = manifest.get("date") or manifest["window"]["to"][:10]
    published, failed, moves, entries = [], [], [], []
    for tender in manifest["tenders"]:
        if tender["status"] != "downloaded":
            continue
        try:
            pack = convert(work, tender)
        except Exception as exc:
            failed.append({"pid": tender["pid"], "reason": "conversion: %s" % type(exc).__name__})
            say("  %s not converted: %s" % (tender["pid"], type(exc).__name__))
            continue
        record, entry = publish_tender(drive, base, pack, tender["pid"], date, run_id)
        record["watched"] = tender.get("watched", False)
        moves.append(dict(record, pid=tender["pid"],
                          path="tenders/%s/runs/%s.json" % (tender["pid"], date)))
        entries.append((tender, entry, record))
        published.append(tender["pid"])
        say("  %s published (%s)" % (tender["pid"], record["status"]))
        shutil.rmtree(pack, ignore_errors=True)

    for tender in manifest["tenders"]:
        if tender["status"] == "failed":
            failed.append({"pid": tender["pid"], "reason": "download: %s" % (tender.get("error") or "")[:200]})
    withdrawn = [t["pid"] for t in manifest["tenders"] if t["status"] == "withdrawn"]
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    counts = {}
    for m in moves:
        counts[m["status"]] = counts.get(m["status"], 0) + 1

    drive.upload("%s/%s/changes.json" % (base, date), json.dumps({
        "schema": "day-changes/2", "date": date, "batch_id": batch_id, "run_id": run_id,
        "written_at": stamp, "counts": dict(counts, tenders=len(moves)), "tenders": moves},
        ensure_ascii=False).encode("utf-8"))
    drive.upload("%s/%s/day.json" % (base, date), json.dumps({
        "schema": "day/2", "date": date, "batch_id": batch_id, "run_id": run_id,
        "written_at": stamp, "window": manifest["window"],
        "changes_path": "%s/changes.json" % date, "tenders_path": "tenders",
        "counts": dict(counts, tenders=len(entries),
                       documents=sum(len(e["documents"]) for _, e, _ in entries),
                       skipped=sum(len(e["skipped"]) for _, e, _ in entries)),
        "tenders": [{"pid": t["pid"], "key": e["key"], "title": e.get("title"),
                     "status": r["status"], "watched": t.get("watched", False),
                     "folder_path": e["home"], "index_path": "%s/index.json" % e["home"],
                     "path": "%s/%s" % (e["home"], e["archive"]),
                     "run_path": "%s/runs/%s.json" % (e["home"], date),
                     "documents": len(e["documents"]), "unreadable": len(e["unreadable"]),
                     "skipped": len(e["skipped"])} for t, e, r in entries],
        "failed": failed, "withdrawn": withdrawn}, ensure_ascii=False).encode("utf-8"))
    drive.upload(done, json.dumps({
        "schema": "batch-complete/1", "batch_id": batch_id, "country": manifest["country"],
        "window": manifest["window"], "date": date, "run_id": run_id, "written_at": stamp,
        "day_path": "%s/day.json" % date, "changes_path": "%s/changes.json" % date,
        "counts": {"published": len(published), "failed": len(failed),
                   "withdrawn": len(withdrawn)},
        "published": published, "failed": failed, "withdrawn": withdrawn},
        ensure_ascii=False).encode("utf-8"))
    drive.delete(inbox)
    say("batch %s recorded: %d published, %d failed, %d withdrawn"
        % (batch_id, len(published), len(failed), len(withdrawn)))
    fire(batch_id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Process one handed-over batch.")
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    args = ap.parse_args(argv)
    code = args.batch_id.split("-", 1)[0]
    if not args.batch_id.replace("-", "").isalnum() or len(args.batch_id) > 40:
        say("not a batch id")
        return 2
    base = country.destination(graph.env("GRAPH_DEST_ROOT"), country.resolve(code, {}))
    work = tempfile.mkdtemp(prefix="batch_")
    try:
        return process(graph.Drive(), base, args.batch_id, args.run_id or None, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
