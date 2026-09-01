#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write the day's list, after the shards have landed.

    python3 collect_day.py --date 2026-08-11 --shards 4 --slices 4 --run-id 31478237531

DELIVERY SHAPE. `day.json` and `changes.json` sit beside `shards/`. Each shard folder holds
its index and its own accounting files — not the tenders themselves, which live in
`tenders/<pid>/` and are addressed from here. The day folder holds no tender bytes at all.

TWO FILES, TWO QUESTIONS. `day.json` answers "which tenders does this day contain, and where
is each one" — the list, balanced into slices, with a drive address per tender. `changes.json`
answers "what actually moved", which is the question a consumer that has read this day's
tenders before is really asking, and on most days it is a far shorter answer.

WHY THIS EXISTS. A reader that lists folders to find its work reads the wrong day. Delivery
overwrites files and never removes them, so a date fetched twice holds change records from
both runs: a shard folder can name more tenders than its own `done.txt` does, and nothing in
the folder says which run put a record there. This file is the list that does, and it is the
only place that does: a tender's home is shared by every day that ever touched it, so nothing
else states that a particular day fetched a particular tender.

WHAT IT READS. Not the packs on this runner — the shards' own `index.json` **as delivered**,
through the same Graph drive the reader will use. So `day.json` describes what arrived, not
what was intended, and a document that failed to upload is absent from both.

WHY IT GOES LAST. Its presence is the reader's proof that the day is there to be read. A run
that dies mid-delivery leaves shards without a list, and a reader with no list stops and says
so rather than reading three quarters of a day as if it were all of it.

A SHORT DAY IS STILL A DAY, AND IT SAYS SO. If a shard delivered nothing, its number lands in
`shards_missing` and `complete` is false. The list is still written, because three shards of
work is still worth reading — but nothing downstream can mistake it for a whole day, which is
the failure this project keeps returning to.

WHAT IT DECIDES. One thing only: which slice of the day each tender belongs to, balanced by
extracted characters rather than by count, because tenders differ by orders of magnitude in
size. No judgement about tenders is made or can be: that is the consumer's business and
never travels through this repository.
"""

import argparse
import country
import os
import json
import sys
import time

# The Graph layer lives in one module, beside the delivery that writes through it. This file
# only ever reads, but reading is not a second dialect of the same protocol and two copies of
# the retry policy would drift the first time either was corrected.
import idspace
from deliver_graph import env, graph_token, item_at, json_at, text_at, upload


def uri(drive, item):
    """The address the reader's connector takes, built from what the drive just told us."""
    return "file:///%s/%s" % (drive, item["id"]) if item else None


def chars_of(entry):
    return sum(int(d.get("chars") or 0) for d in entry.get("documents", []))


def balance(tenders, slices):
    """Longest-processing-time first: the heaviest tender goes to the lightest slice.

    Counting tenders would be the obvious split and the wrong one — shards balanced by count
    come out wildly uneven by volume, because one tender can be tens of times another.
    Reading time follows characters, so characters are what gets levelled.
    """
    bins = [{"slice": i + 1, "chars": 0, "pids": []} for i in range(slices)]
    for t in sorted(tenders, key=lambda t: t["chars"], reverse=True):
        lightest = min(bins, key=lambda b: (b["chars"], b["slice"]))
        lightest["chars"] += t["chars"]
        lightest["pids"].append(t["pid"])
        t["slice"] = lightest["slice"]
    return bins


def collect(drive, base, date, shards, slices, run_id, tok):
    """Read every shard's delivered index and turn it into one list, and one diff."""
    present, missing, lost, tenders, moves, stale = [], [], [], [], [], []
    expected, excused, resolved = set(), set(), {}

    for n in range(1, shards + 1):
        root = "%s/%s/shards/eis-batch-shard-%d" % (base, date, n)
        index = json_at(drive, "%s/index.json" % root, tok)
        if not index:
            missing.append(n)
            continue
        # AN INDEX FROM AN EARLIER RUN IS NOT THIS RUN'S SHARD. The day folder outlives the
        # run that filled it, so a date fetched twice keeps the first run's indexes; a shard
        # that died mid-delivery this time is otherwise counted present on the strength of
        # them, and the day calls itself complete while missing a quarter of its tenders.
        # Only compared when both sides name a run — a hand-run delivery names none, and
        # refusing its index would make the check itself the thing that loses a day.
        if run_id and index.get("run_id") and str(index["run_id"]) != str(run_id):
            stale.append(n)
            missing.append(n)
            continue
        present.append(n)

        # What the whole day was asked for, as this shard saw it, and what it could not
        # deliver of its own slice. Unioned across shards because each one walks the register
        # for itself and they do not always agree.
        accounts = index.get("accounts") or {}
        expected.update(accounts.get("targets") or ())
        excused.update(accounts.get("failed") or ())
        excused.update(accounts.get("withdrawn") or ())
        resolved.update(accounts.get("resolved") or {})

        for line in text_at(drive, "%s/failed.txt" % root, tok).splitlines():
            if line.strip():
                lost.append({"shard": n, "entry": line.strip()})

        for entry in index.get("tenders", []):
            pid = str(entry.get("pid") or "")
            if not pid:
                continue
            # THE ADDRESSES ARE THE TENDER'S HOME, NOT THIS DAY'S FOLDER. A day names which
            # tenders moved; the tender itself lives in one place and is complete there
            # whether it was first fetched this morning or four months ago.
            home = entry.get("home") or "tenders/%s" % pid
            archive_name = entry.get("archive") or "%s.zip" % pid
            index_name = entry.get("index_file") or "index.json"
            archive_item = item_at(drive, "%s/%s/%s" % (base, home, archive_name), tok)
            index_item = item_at(drive, "%s/%s/%s" % (base, home, index_name), tok)
            archive_uri = uri(drive, archive_item)
            change = entry.get("change") or {}
            # WHERE THIS DAY'S RECORD FOR THIS TENDER LIVES, WHICH IS IN THE TENDER. The day
            # itself carries every record inline, here and in the shard index, so the only
            # copy written as a file of its own is the one indexed beside the procurement.
            run_path = "%s/%s" % (home, entry.get("run_file") or "runs/%s.json" % date)
            moves.append(dict(change, pid=pid, shard=n, path=run_path))
            tenders.append({
                "pid": pid,
                "key": entry.get("key") or "EIS:%s" % pid,
                "title": entry.get("title"),
                "shard": n,
                # What this day did to the tender. A consumer that has read it before can
                # stop here on "unchanged" without opening anything at all.
                "status": entry.get("status") or change.get("status"),
                "run_path": run_path,
                "path": "%s/%s" % (home, archive_name),
                # The same tender unpacked, for a reader that wants one document rather
                # than the whole thing. Named here so nobody has to guess it exists.
                "folder_path": home,
                "uri": archive_uri,
                "archive_uri": archive_uri,
                "index_uri": uri(drive, index_item),
                "index_path": index_name,
                "documents": len(entry.get("documents", [])),
                "chars": chars_of(entry),
                "unreadable": len(entry.get("unreadable", [])),
            })

    # A tender delivered twice under two shards is one tender. Keyed by pid, last wins, and the
    # count below is of tenders rather than of folders — which is the whole point of this file.
    unique = {}
    for t in tenders:
        unique[t["pid"]] = t
    tenders = sorted(unique.values(), key=lambda t: t["pid"])
    moved = {}
    for m in moves:
        moved[m["pid"]] = m
    moves = sorted(moved.values(), key=lambda m: m["pid"])

    # A TARGET NOBODY FETCHED. The shards divide the day by each computing the same plan from
    # a list each walks the register for itself, and one notice weighed differently by one of
    # them reshuffles a large part of the assignment: measured at ninety assignments covering
    # sixty-eight tenders out of ninety-three targets, with the day calling itself complete.
    # Subtracting what was delivered and what the shards reported as failed or withdrawn
    # leaves the tenders that fell between the slices.
    # A uuid this shard never owned is still in its target list under the name it was asked
    # by; the shard that did own it published what that resolved to. Normalising with the
    # union keeps a target from being counted missing because two shards spelled it
    # differently.
    named = lambda k: resolved.get(k, k)
    expected = {named(k) for k in expected}
    excused = {named(k) for k in excused}
    unaccounted = sorted(expected - excused - {"eis:%s" % t["pid"] for t in tenders})

    bins = balance(tenders, slices)
    by_status = {}
    for t in tenders:
        by_status[t.get("status") or "unknown"] = by_status.get(t.get("status") or "unknown", 0) + 1

    # THE DAY'S DIFF, AS ONE FILE. This is what a consumer reads to decide whether to open
    # anything: every tender the day touched, what moved about it, and where it lives. A day
    # on which two deadlines shifted is a few kilobytes here and nothing else worth fetching.
    changes = {
        "schema": "day-changes/1",
        "date": date,
        "run_id": run_id,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "complete": not missing and not unaccounted,
        "shards_missing": missing,
        "shards_stale": stale,
        "unaccounted": unaccounted,
        "counts": dict(by_status, tenders=len(moves)),
        "tenders": moves,
    }

    day = {
        "schema": "day/1",
        "date": date,
        "shards_path": "%s/shards" % date,
        # The same list one level down, and the file to read first. Named here so a consumer
        # holding day.json never has to guess that the diff exists.
        "changes_path": "%s/changes.json" % date,
        "tenders_path": "tenders",
        "run_id": run_id,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # Whole means every shard delivered AND every target the shards named reached the
        # day. A day short by a quarter used to satisfy the first alone.
        "complete": not missing and not unaccounted,
        "coverage": {"targets": len(expected), "delivered": len(tenders),
                     "excused": len(expected & excused), "unaccounted": unaccounted},
        "shards_expected": shards,
        "shards_present": present,
        "shards_missing": missing,
        # Named apart from the merely absent: a stale index means an earlier run of this date
        # left one behind, which reads very differently from a shard that never ran.
        "shards_stale": stale,
        "slices": slices,
        "counts": dict(
            by_status,
            tenders=len(tenders),
            documents=sum(t["documents"] for t in tenders),
            chars=sum(t["chars"] for t in tenders),
            unreadable=sum(t["unreadable"] for t in tenders),
        ),
        "slice_load": [{"slice": b["slice"], "tenders": len(b["pids"]), "chars": b["chars"]}
                       for b in bins],
        "lost": lost,
        "tenders": tenders,
    }
    return day, changes


def idspace_after(drive, base, date, shards, day, tok):
    """Tonight's answers merged into the id space, or None when there is nothing to write.

    Reads each shard's `idprobe.json` — absent on a runner that had no slice, or that died
    before delivering, and absent everywhere until the walk is switched on. An id nobody
    could reach is in no report at all: an unreachable page is not an answer, and recording
    it as "not published" would retire a live id for as long as the state remembers it.
    """
    probes, handed = {}, []
    for n in range(1, shards + 1):
        report = json_at(drive, "%s/%s/shards/eis-batch-shard-%d/idprobe.json" % (base, date, n), tok)
        for pid, published in ((report or {}).get("probes") or {}).items():
            probes[pid] = bool(published)
        # What actually reached the fetch. Only these come off the queue: an id found and not
        # handed over is owed, and forgetting it here would make the handover cap a quiet way
        # of losing tenders.
        handed.extend((report or {}).get("handed") or [])

    delivered = [t.get("pid") for t in day.get("tenders") or [] if str(t.get("pid") or "").isdigit()]
    if not probes and not delivered:
        return None
    prior = json_at(drive, "%s/idspace.json" % base, tok)
    return idspace.merge(prior, probes, date, discovered=delivered, handed=handed)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write day.json from the shards that landed.")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD, the day this batch belongs to")
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--slices", type=int, default=4, help="how many consumer runs share the day")
    ap.add_argument("--run-id", default="", help="the workflow run this day came from")
    country.add_argument(ap)
    args = ap.parse_args(argv)

    drive = env("GRAPH_DRIVE_ID")
    code = country.resolve(args.country, os.environ)
    base = country.destination(env("GRAPH_DEST_ROOT"), code)
    tok = graph_token()

    day, changes = collect(drive, base, args.date, args.shards, args.slices, args.run_id, tok)

    # The diff before the list, for the same reason the list goes after the shards: day.json
    # is the proof that the day is there to be read, and it names `changes.json`. A reader
    # holding a list that points at a file still arriving is the one state worth preventing.
    changes_bytes = json.dumps(changes, ensure_ascii=False).encode("utf-8")
    upload(drive, "%s/%s/changes.json" % (base, args.date), changes_bytes, tok)

    upload(drive, "%s/%s/day.json" % (base, args.date),
           json.dumps(day, ensure_ascii=False).encode("utf-8"), tok)

    # THE ID SPACE, WRITTEN ONCE AND HERE. Every shard asked the platform about a slice of
    # tonight's ids and left its answers in `idprobe.json`; this is the only place that sees
    # all of them, and it runs after all of them, so it is the only place that may write the
    # state without two runners disagreeing about it.
    #
    # The day's own tenders are folded in as live. That is what seeds the frontier on a
    # deployment that has never walked before — the first night plans nothing, this writes a
    # frontier from what the register delivered, and the walk starts on the next run.
    walked = idspace_after(drive, base, args.date, args.shards, day, tok)
    if walked:
        upload(drive, "%s/idspace.json" % base,
               json.dumps(walked, ensure_ascii=False).encode("utf-8"), tok)

    # Counts, never the destination.
    print("day %s: %d tenders, %d documents, %.1f MB of text, shards %s present%s"
          % (args.date, day["counts"]["tenders"], day["counts"]["documents"],
             day["counts"]["chars"] / 1e6, day["shards_present"],
             "" if day["complete"] else ", MISSING %s" % day["shards_missing"]))
    # A guard that fires silently is half a guard: an index left by an earlier run of this
    # date is the difference between a short day and a day that only looks whole.
    # Printed whether or not it found anything, because a silent check and a check that never
    # ran look identical in a log — and the second is how a short day passes for a whole one.
    print("  coverage: %d target(s), %d delivered, %d settled, %d unaccounted"
          % (day["coverage"]["targets"], day["coverage"]["delivered"],
             day["coverage"]["excused"], len(day["coverage"]["unaccounted"])))
    if day["coverage"]["unaccounted"]:
        print("  %d of %d target(s) reached no shard — the day is short: %s"
              % (len(day["coverage"]["unaccounted"]), day["coverage"]["targets"],
                 ", ".join(day["coverage"]["unaccounted"][:8])
                 + (" …" if len(day["coverage"]["unaccounted"]) > 8 else "")))
    if day["shards_stale"]:
        print("  shard(s) %s carried an index from an earlier run of this date and were "
              "counted missing" % day["shards_stale"])
    print("  changes.json: %d new, %d changed, %d unchanged (%.0f KB)"
          % (changes["counts"].get("new", 0), changes["counts"].get("changed", 0),
             changes["counts"].get("unchanged", 0), len(changes_bytes) / 1e3))
    for row in day["slice_load"]:
        print("  slice %d: %d tenders, %.1f MB" % (row["slice"], row["tenders"],
                                                   row["chars"] / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
