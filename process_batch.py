"""Tender document processing: one handed-over tender, on a GitHub runner.

    python process_batch.py --handover-id 900001-2 --country LV

Reads the tender's hand-over `inbox/<pid>-<lease>/` from the staging folder of the library and
checks it against its manifest. An incomplete hand-over is put back on the day's work list at
the Tender download coordinator and nothing else is written. A complete one is converted to
text, published as `tenders/<pid>/` with this publication's list of changes, recorded in
`published/<pid>-<lease>.json` last, and its hand-over removed. It never connects to IUB or
EIS and starts nothing.

Environment: GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_DRIVE_ID,
GRAPH_DEST_ROOT (the folder holding the country folders), COORDINATOR_URL,
COORDINATOR_CLIENT_ID, COORDINATOR_CLIENT_SECRET, EIS_COUNTRY (or --country).
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import time

import changes
import coordinator
import country
import eis_fetch
import graph
import publish

OCR_FILES = 40
HANDOVER_ID = re.compile(r"^(\d{1,12})-(\d{1,9})$")


def say(line):
    print(line, flush=True)


class Incomplete(Exception):
    """The hand-over does not match its manifest (Tender complete, rules 1 and 2)."""


def check(drive, inbox, manifest, work):
    """Download every kept file of the tender and compare it with the manifest."""
    tender = manifest["tender"]
    folder = os.path.join(work, tender["pid"], "documents", "actual")
    os.makedirs(folder, exist_ok=True)
    for f in tender["files"]:
        if f["decision"] != "download":
            continue
        local = os.path.join(folder, f["stored"].split("/", 1)[1])
        if not drive.download("%s/%s" % (inbox, f.get("inbox") or f["stored"]), local):
            raise Incomplete("a kept file is missing")
        if os.path.getsize(local) != f["size"] or eis_fetch.sha256_file(local) != f["sha256"]:
            raise Incomplete("a kept file differs from the manifest")


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
    os.makedirs(pack, exist_ok=True)
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


def publish_tender(drive, base, pack, tender, date, run_id):
    """Upload one tender folder, with this publication's list of changes, `index.json` last.
    Returns the list of changes."""
    pid = tender["pid"]
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
    index = None
    if changes.refreshed(record):
        wanted = {"doc/%s.md" % k for k in changes.documents_to_send(record, current)}
        wanted |= {doc["path"] for doc, _ in recognised}
        drive.upload("%s/%s.zip" % (root, pid), publish.tender_archive(contents))
        for rel, data in contents:
            if rel == "index.json":
                index = data
            elif not rel.startswith("doc/") or rel in wanted:
                drive.upload("%s/%s" % (root, rel), data)
        drive.upload(root + "/state.json", json.dumps(current, ensure_ascii=False).encode("utf-8"))
    drive.upload(root + "/seen.json", json.dumps(changes.seen(pid, was_seen, record, date),
                                                 ensure_ascii=False).encode("utf-8"))
    drive.upload("%s/runs/%s.json" % (root, date),
                 json.dumps(record, ensure_ascii=False).encode("utf-8"))
    moved = dict(record, pid=pid, watched=bool(tender.get("watched")),
                 path="%s/runs/%s.json" % (home, date))
    drive.upload(root + "/changes.json", json.dumps(moved, ensure_ascii=False).encode("utf-8"))
    if index is not None:
        drive.upload(root + "/index.json", index)
    return moved


def put_back(handover_id, date, reason, client=None):
    """Tell the coordinator to give the tender out again. False when it could not be told."""
    try:
        client = client or coordinator.Client(os.environ.get("COORDINATOR_URL"),
                                              os.environ.get("COORDINATOR_CLIENT_ID"),
                                              os.environ.get("COORDINATOR_CLIENT_SECRET"))
        client.put_back(date, handover_id, reason)
        return True
    except coordinator.Error as exc:
        say("the coordinator was not told: %s" % exc)
        return False


def riga_today():
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Europe/Riga")
    except Exception:
        zone = dt.timezone(dt.timedelta(hours=2))
    return dt.datetime.now(dt.timezone.utc).astimezone(zone).date().isoformat()


def process(drive, base, handover_id, run_id, work, client=None):
    pid, lease = HANDOVER_ID.match(handover_id).groups()
    inbox = "%s/inbox/%s" % (base, handover_id)
    done = "%s/published/%s.json" % (base, handover_id)
    if drive.read(done) is not None:
        say("hand-over %s was processed before" % handover_id)
        drive.delete(inbox)
        return 0
    manifest = drive.read_json(inbox + "/manifest.json")
    try:
        if not manifest:
            raise Incomplete("no tender manifest")
        if str(manifest.get("pid")) != pid or str(manifest.get("lease")) != lease \
                or str((manifest.get("tender") or {}).get("pid")) != pid:
            raise Incomplete("the manifest is for another hand-over")
        if manifest["tender"]["status"] == "downloaded":
            check(drive, inbox, manifest, work)
    except Incomplete as exc:
        # A hand-over with no manifest names no date; it can only be of today's list.
        date = (manifest or {}).get("date") or riga_today()
        say("hand-over %s incomplete: %s; put back on the day's work list" % (handover_id, exc))
        put_back(handover_id, date, str(exc), client)
        return 2
    tender = manifest["tender"]
    date = manifest["date"]
    say("hand-over %s complete" % handover_id)

    outcome, reason, moved = "published", None, None
    if tender["status"] != "downloaded":
        outcome, reason = "failed", "%s: %s" % (tender["status"], (tender.get("error") or "")[:200])
    else:
        try:
            pack = convert(work, tender)
        except Exception as exc:
            outcome, reason = "failed", "conversion: %s" % type(exc).__name__
        else:
            moved = publish_tender(drive, base, pack, tender, date, run_id)
            shutil.rmtree(pack, ignore_errors=True)
    drive.upload(done, json.dumps({
        "schema": "tender-published/1", "country": manifest["country"], "date": date,
        "pid": pid, "lease": manifest["lease"], "computer": manifest.get("computer"),
        "outcome": outcome, "reason": reason, "run_id": run_id,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tender_path": "tenders/%s/" % pid,
        "changes_path": "tenders/%s/changes.json" % pid if moved else None},
        ensure_ascii=False).encode("utf-8"))
    drive.delete(inbox)
    say("tender %s %s%s" % (pid, outcome, " (%s)" % moved["status"] if moved else
                            ": %s" % reason.split(":", 1)[0]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Process one handed-over tender.")
    ap.add_argument("--handover-id", required=True, help="<pid>-<lease>, for example 900001-2")
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    country.add_argument(ap)
    args = ap.parse_args(argv)
    if not HANDOVER_ID.match(args.handover_id):
        say("not a hand-over id")
        return 2
    base = country.destination(graph.env("GRAPH_DEST_ROOT"),
                               country.resolve(args.country, os.environ))
    work = tempfile.mkdtemp(prefix="tender_")
    try:
        return process(graph.Drive(), base, args.handover_id, args.run_id or None, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
