#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deliver a Lithuanian day to a Graph drive, and give the day a memory.

    python3 deliver_lt.py --out work --date 2026-08-20

WHY THIS IS NOT `deliver_graph`. The two deliveries agree about almost everything — the
same Graph client, the same home layout, the same rule that an index is written after the
files it names — and they are separate for three reasons, each of which was found by
reading what the Latvian delivery would have done to a Lithuanian tender:

  * `deliver_graph` REBUILDS `index.json` from `procurement.json` and the normalized
    manifest. Lithuania's index is not derivable from those: it carries the amendment
    number that placed each document and the address a person clicks, both of which come
    from the EPPS catalogue and are dropped by the time `procurement.json` is written. A
    rebuilt index is a correct-looking index with the two Lithuanian facts missing.
  * `deliver_graph.download_url` is `https://www.eis.gov.lv/EKEIS/Document/...` — a
    literal. Pointed at Lithuania it does not fail; it stamps a working Latvian URL shape
    onto a Lithuanian procurement, and the card carries a link to a document that is not
    the one it names.
  * A shard is in its path — `<date>/shards/eis-batch-shard-N/` — because four runners
    draw four addresses at a portal that refuses a third of them. EPPS refuses none, so
    Lithuania has one runner, no shard index to reconcile, and no `collect_day` step.

So this file delivers WHAT THE FETCH ALREADY BUILT rather than reassembling it. `lt_fetch`
writes the index, and the index it wrote is the one that ships.

WHERE THE MEMORY LIVES, AND WHY IT CANNOT BE THE DISK. `lt_day` compares each procurement
against `state.json` in its own home — correct on a workstation that keeps `work/` between
runs, and worthless on a runner, whose disk is new every night. Every procurement would
come back `new`, `changes.json` would be a copy of the day for ever, and the half of the
night that reports what moved would have nothing true to say.

The drive is the only durable thing in the arrangement, so the comparison is made against
the drive, here, at delivery — exactly as the Latvian delivery already does it. The local
`changes.json` is overwritten with the result before it is uploaded, so the file a reader
finds is the one that was compared against what the reader can actually see.

WHAT IT KNOWS ABOUT THE DESTINATION: NOTHING, in the same way and for the same reason as
`deliver_graph`. Tenant, client, drive and root arrive in the environment; the country
picks the folder under the root; and this file prints counts rather than paths.
"""

import argparse
import json
import os
import sys
import time

import changes
import country
# The Graph client, the upload session, the retry set and the archive builder are shared
# rather than copied. Two clients would drift, and the first sign of the drift would be one
# country retrying a code the other had learned to retry.
import deliver_graph as graph


# WHAT TRAVELS, AND WHAT STAYS BEHIND. The same principle as `deliver_graph.KEEP_NAMES`:
# everything a reader opens and nothing it does not. `originals/` is the published files
# themselves — the overwhelming majority of the bytes — and no reader opens them, because
# every document is already delivered as Markdown and the index carries the portal address
# for anyone who wants the original itself. The raw EPPS archive stays behind for the same
# reason, and its name is reused for the archive of what did travel: a reader that takes
# `<pid>.zip` wants the procurement readable in one request, not a folder of .docx.
FLAT = ("procurement.json", "manifest.json")
NORMALIZED = "normalized/manifest_normalized.json"


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def delivered_index(home, pid, date, run_id):
    """The index that ships: the one `lt_fetch` wrote, addressed for the drive.

    Two edits, both because the drive holds less than the pack did. `original` pointed at
    `originals/<file>`, which is not delivered, and a pointer to something absent is worse
    than no pointer — `download` already names the portal's own address for that document.
    The delivery fields are added for parity with Latvia, so a reader that has learned one
    home has learned both.
    """
    with open(os.path.join(home, "index.json"), encoding="utf-8") as fh:
        index = json.load(fh)
    documents = []
    for entry in index.get("documents", []):
        entry = dict(entry)
        entry.pop("original", None)
        documents.append(entry)
    index["documents"] = documents
    index.update({"home": "tenders/%s" % pid, "archive": "%s.zip" % pid,
                  "index_file": "index.json", "run_file": "runs/%s.json" % date,
                  "date": date, "run_id": run_id})
    return index


def members(home, pid, date, run_id, index=None):
    """Everything one procurement publishes, in the order it is published.

    ONE LIST, TWO RENDERINGS, exactly as Latvia does it: the home somebody can open without
    downloading anything, and the archive somebody can take whole, are built from this same
    list, so a file that reaches one always reaches the other.

    `index.json` is last because it is the reader's proof that the rest arrived.
    """
    out = []
    for name in FLAT:
        path = os.path.join(home, name)
        if os.path.exists(path):
            out.append((name, _read(path)))
    normalized = os.path.join(home, "normalized", "manifest_normalized.json")
    if os.path.exists(normalized):
        out.append((NORMALIZED, _read(normalized)))
    doc_dir = os.path.join(home, "doc")
    if os.path.isdir(doc_dir):
        for name in sorted(os.listdir(doc_dir)):
            if name.endswith(".md"):
                out.append(("doc/%s" % name, _read(os.path.join(doc_dir, name))))
    index = index if index is not None else delivered_index(home, pid, date, run_id)
    out.append(("index.json",
                json.dumps(index, ensure_ascii=False).encode("utf-8")))
    return out


def deliver(out_root, date, run_id, drive, base, tok):
    """One day's homes and one day's two files. Returns the counts worth printing."""
    day_dir = os.path.join(out_root, date)
    with open(os.path.join(day_dir, "day.json"), encoding="utf-8") as fh:
        day = json.load(fh)
    with open(os.path.join(day_dir, "changes.json"), encoding="utf-8") as fh:
        day_changes = json.load(fh)

    # THE DAY NAMES WHAT IS DELIVERED, NOT THE DIRECTORY. `work/LT/tenders/` accumulates
    # every procurement ever fetched onto this disk, and on a workstation that is months of
    # them. Only what this day actually delivered is this day's business.
    pids = [str(t["pid"]) for t in day.get("tenders", [])]

    records = {}
    files = sent = carried = 0
    bytes_sent = 0
    tally = {"new": 0, "changed": 0, "unchanged": 0}

    for pid in pids:
        home = os.path.join(out_root, "tenders", pid)
        state_path = os.path.join(home, "state.json")
        if not os.path.exists(state_path):
            # A procurement the day names and the disk does not hold is a delivery that did
            # not finish. Named, never skipped in silence.
            print("  no state for %s — not delivered" % pid, file=sys.stderr)
            continue
        with open(state_path, encoding="utf-8") as fh:
            current = json.load(fh)

        home_root = "%s/tenders/%s" % (base, pid)
        # WHAT THIS PROCUREMENT LOOKED LIKE LAST TIME, ASKED OF THE DRIVE. Absent for one
        # nobody has fetched before, which is the answer for every one of them on the first
        # night this runs and for none of them afterwards.
        was_seen = graph.json_at(drive, "%s/seen.json" % home_root, tok)
        record = changes.diff(graph.json_at(drive, "%s/state.json" % home_root, tok),
                              current, date=date, run_id=run_id, seen=was_seen)
        record["home"] = "tenders/%s" % pid
        records[pid] = record
        tally[record["status"]] = tally.get(record["status"], 0) + 1

        index = delivered_index(home, pid, date, run_id)
        contents = members(home, pid, date, run_id, index=index)

        # ONLY WHAT IS NOT ALREADY THERE. A document's name is its digest, so the bytes at
        # that name cannot have become different bytes, and a document uploaded the day it
        # appeared is never uploaded again.
        wanted = {"doc/%s.md" % k for k in changes.documents_to_send(record, current)}
        to_send = [(rel, data) for rel, data in contents
                   if not rel.startswith("doc/") or rel in wanted]
        sent += len(wanted)
        carried += 0 if record.get("reextracted") else record.get("carried_over", 0)

        # A PROCUREMENT THAT DID NOT MOVE WRITES ONLY THE TWO SMALL PER-RUN FILES.
        # Everything else on the drive already describes it correctly, and rewriting it
        # would cost megabytes a night to advance a timestamp.
        #
        # INDEX AFTER THE DOCUMENTS, STATE AFTER THE INDEX: `index.json` is the reader's
        # proof the home is whole, `state.json` is tomorrow's proof of what it may skip, and
        # a state file that landed before the documents it vouches for would let tomorrow
        # carry over text that never arrived. `contents` ends with the index, so the loop
        # below is already in the order of the rule.
        if changes.refreshed(record):
            archive = graph.tender_archive(contents)
            graph.upload(drive, "%s/%s.zip" % (home_root, pid), archive, tok)
            files += 1
            bytes_sent += len(archive)
            for rel, data in to_send:
                graph.upload(drive, "%s/%s" % (home_root, rel), data, tok)
                files += 1
                bytes_sent += len(data)
            state_bytes = json.dumps(current, ensure_ascii=False).encode("utf-8")
            graph.upload(drive, "%s/state.json" % home_root, state_bytes, tok)
            files += 1
            bytes_sent += len(state_bytes)

        for dest, payload in (("seen.json", changes.seen(pid, was_seen, record, date)),
                              ("runs/%s.json" % date, record)):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            graph.upload(drive, "%s/%s" % (home_root, dest), data, tok)
            files += 1
            bytes_sent += len(data)

    # THE DAY'S TWO FILES, WITH THE VERDICT THE DRIVE GAVE RATHER THAN THE ONE THE DISK
    # GUESSED. `lt_day` wrote `status` against a local `state.json` that a runner never has;
    # every one of them says `new`. Replacing them here is the whole point of the exercise,
    # and the file that lands is therefore the one a reader can act on.
    for row in day_changes.get("tenders", []):
        record = records.get(str(row["pid"]))
        if record is None:
            continue
        row["status"] = record["status"]
        row["moved"] = record.get("moves") or []
        row["change"] = changes.summary(record)
    for row in day.get("tenders", []):
        record = records.get(str(row["pid"]))
        if record is not None:
            row["status"] = record["status"]
    day_changes["counts"] = dict(day_changes.get("counts") or {}, **tally)
    day_changes["compared_against"] = "drive"

    root = "%s/%s" % (base, date)
    # CHANGES FIRST, DAY LAST. `day.json` is the proof there is a day to read, so it is
    # written after everything it vouches for — the same rule as the index, one level up.
    for name, payload in (("changes.json", day_changes), ("day.json", day)):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        graph.upload(drive, "%s/%s" % (root, name), data, tok)
        files += 1
        bytes_sent += len(data)

    return {"files": files, "bytes": bytes_sent, "tally": tally,
            "sent": sent, "carried": carried, "tenders": len(records)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deliver a Lithuanian day to a Graph drive.")
    ap.add_argument("--out", default="work",
                    help="the runtime root the fetch wrote into; the country folder is "
                         "under it, exactly as the fetch derived it")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD, the day being delivered")
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""),
                    help="the workflow run this delivery came from")
    country.add_argument(ap)
    args = ap.parse_args(argv)

    try:
        code = country.resolve(args.country, os.environ)
    except country.Mismatch as exc:
        print("deliver_lt: %s" % exc, file=sys.stderr)
        return 2
    if code != "LT":
        # Refused rather than obeyed. This file knows the EPPS shape — one archive, an index
        # carrying amendments, no shards — and a Latvian pack put through it would deliver a
        # home missing the accounting `collect_day` reads.
        print("deliver_lt: this lane is Lithuania's; %s is delivered by deliver_graph.py"
              % code, file=sys.stderr)
        return 2

    drive = graph.env("GRAPH_DRIVE_ID")
    base = country.destination(graph.env("GRAPH_DEST_ROOT"), code)
    tok = graph.graph_token()

    out_root = os.path.join(args.out, code)
    result = deliver(out_root, args.date, args.run_id or None, drive, base, tok)

    print("delivered %s for %s: %d procurement(s), %d SharePoint files, %.1f MB"
          % (code, args.date, result["tenders"], result["files"], result["bytes"] / 1e6))
    print("  %d new, %d changed, %d unchanged · %d document(s) sent, %d carried over"
          % (result["tally"]["new"], result["tally"]["changed"],
             result["tally"]["unchanged"], result["sent"], result["carried"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
