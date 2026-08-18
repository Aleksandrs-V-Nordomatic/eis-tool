#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write the day's list, after the shards have landed.

    python3 collect_day.py --date 2026-08-11 --shards 4 --slices 4 --run-id 31478237531

DELIVERY SHAPE. `day.json` stays beside `shards/` and `shards.zip`. Each shard folder holds
every tender twice — as `<pid>/`, which can be opened one document at a time, and as
`<pid>.zip`, which is one request for the whole tender — plus the small index sidecars and
the shard-level accounting files.

`shards.zip` carries the archives and the sidecars, NOT a second copy of the unpacked
folders: a reader taking the whole day wants it once, and mirroring the folders as well
would roughly double a file that is already tens of megabytes. So the folders are for
looking, the ZIP is for taking, and neither is a subset of the other by accident.

WHY THIS EXISTS. A reader that lists folders to find its work reads the wrong day. Delivery
overwrites files and never removes folders, so a date fetched twice holds tenders from both
runs: a shard can hold more tender folders than its own `done.txt` names, and one tender's
`summary.json` can return two different digests on the same path an hour apart. Nothing in a
folder says which run put it there. This file is the list that does.

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
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from deliver_graph import GRAPH, env, graph_token, upload, upload_file


def get(url, tok, tries=4):
    """One GET, retried on the codes Graph returns under load. Returns the body as bytes."""
    for attempt in range(tries):
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + tok)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                wait = int(e.headers.get("Retry-After") or (2 ** attempt))
                time.sleep(min(wait, 60))
                continue
            raise SystemExit("read failed: HTTP %d after %d attempt(s)" % (e.code, attempt + 1))
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit("read failed: transport error after %d attempts" % tries)
    raise SystemExit("read failed: retries exhausted")


def escaped(path):
    return "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))


def item_at(drive, path, tok):
    """The drive item at `path`, or None. Its `id` is half of the address a reader needs."""
    body = get("%s/drives/%s/root:/%s" % (GRAPH, drive, escaped(path)), tok)
    return json.loads(body.decode("utf-8")) if body else None


def json_at(drive, path, tok):
    body = get("%s/drives/%s/root:/%s:/content" % (GRAPH, drive, escaped(path)), tok)
    return json.loads(body.decode("utf-8")) if body else None


def text_at(drive, path, tok):
    body = get("%s/drives/%s/root:/%s:/content" % (GRAPH, drive, escaped(path)), tok)
    return body.decode("utf-8") if body else ""


def bytes_at(drive, path, tok):
    """Binary content at a Graph path, or None."""
    return get("%s/drives/%s/root:/%s:/content" % (GRAPH, drive, escaped(path)), tok)


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
    """Read every shard's delivered index and turn it into one list."""
    present, missing, lost, tenders = [], [], [], []

    for n in range(1, shards + 1):
        root = "%s/%s/shards/eis-batch-shard-%d" % (base, date, n)
        index = json_at(drive, "%s/index.json" % root, tok)
        if not index:
            missing.append(n)
            continue
        present.append(n)

        for line in text_at(drive, "%s/failed.txt" % root, tok).splitlines():
            if line.strip():
                lost.append({"shard": n, "entry": line.strip()})

        for entry in index.get("tenders", []):
            pid = str(entry.get("pid") or "")
            if not pid:
                continue
            archive_name = entry.get("archive") or "%s.zip" % pid
            index_name = entry.get("index_file") or "%s.index.json" % pid
            archive_item = item_at(drive, "%s/%s" % (root, archive_name), tok)
            index_item = item_at(drive, "%s/%s" % (root, index_name), tok)
            archive_uri = uri(drive, archive_item)
            tenders.append({
                "pid": pid,
                "key": entry.get("key") or "EIS:%s" % pid,
                "title": entry.get("title"),
                "shard": n,
                "path": "%s/shards/eis-batch-shard-%d/%s" % (date, n, archive_name),
                # The same tender unpacked, for a reader that wants one document rather
                # than the whole thing. Named here so nobody has to guess it exists.
                "folder_path": "%s/shards/eis-batch-shard-%d/%s"
                               % (date, n, entry.get("folder") or pid),
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

    bins = balance(tenders, slices)

    return {
        "schema": "day/1",
        "date": date,
        "shards_path": "%s/shards" % date,
        "shards_archive_path": "%s/shards.zip" % date,
        "shards_archive_uri": None,
        "run_id": run_id,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "complete": not missing,
        "shards_expected": shards,
        "shards_present": present,
        "shards_missing": missing,
        "slices": slices,
        "counts": {
            "tenders": len(tenders),
            "documents": sum(t["documents"] for t in tenders),
            "chars": sum(t["chars"] for t in tenders),
            "unreadable": sum(t["unreadable"] for t in tenders),
        },
        "slice_load": [{"slice": b["slice"], "tenders": len(b["pids"]), "chars": b["chars"]}
                       for b in bins],
        "lost": lost,
        "tenders": tenders,
    }


def archive_member(zf, name, data):
    """One deterministic member; nested ZIPs are stored rather than recompressed."""
    info = zipfile.ZipInfo(name.replace("\\", "/"), (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED if name.endswith(".zip") else zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def build_shards_archive(drive, base, date, shards, tok, out_path):
    """Mirror the delivered `shards/` folder into one `shards.zip`."""
    files = 0
    with zipfile.ZipFile(out_path, "w", allowZip64=True) as zf:
        for n in range(1, shards + 1):
            root = "%s/%s/shards/eis-batch-shard-%d" % (base, date, n)
            index_path = "%s/index.json" % root
            index_bytes = bytes_at(drive, index_path, tok)
            if index_bytes is None:
                continue
            try:
                index = json.loads(index_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise SystemExit("archive assembly failed: shard %d index is invalid" % n)

            prefix = "shards/eis-batch-shard-%d" % n
            for name in ("done.txt", "failed.txt", "withdrawn.txt", "resolved.tsv"):
                data = bytes_at(drive, "%s/%s" % (root, name), tok)
                if data is not None:
                    archive_member(zf, "%s/%s" % (prefix, name), data)
                    files += 1

            for entry in index.get("tenders", []):
                pid = str(entry.get("pid") or "")
                if not pid:
                    continue
                names = (entry.get("archive") or "%s.zip" % pid,
                         entry.get("index_file") or "%s.index.json" % pid)
                for name in names:
                    data = bytes_at(drive, "%s/%s" % (root, name), tok)
                    if data is None:
                        raise SystemExit("archive assembly failed: shard %d missing %s" % (n, name))
                    archive_member(zf, "%s/%s" % (prefix, name), data)
                    files += 1

            # Like the folder, the shard index is written after everything it names.
            archive_member(zf, "%s/index.json" % prefix, index_bytes)
            files += 1

    return files, os.path.getsize(out_path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write day.json from the shards that landed.")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD, the day this batch belongs to")
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--slices", type=int, default=4, help="how many consumer runs share the day")
    ap.add_argument("--run-id", default="", help="the workflow run this day came from")
    args = ap.parse_args(argv)

    drive = env("GRAPH_DRIVE_ID")
    base = env("GRAPH_DEST_ROOT").strip("/")
    tok = graph_token()

    day = collect(drive, base, args.date, args.shards, args.slices, args.run_id, tok)
    fd, archive_path = tempfile.mkstemp(prefix="eis_shards_", suffix=".zip")
    os.close(fd)
    try:
        archive_files, archive_size = build_shards_archive(
            drive, base, args.date, args.shards, tok, archive_path)
        archive_dest = "%s/%s/shards.zip" % (base, args.date)
        upload_file(drive, archive_dest, archive_path, tok)
        archive_item = item_at(drive, archive_dest, tok)
        if not archive_item:
            raise SystemExit("archive upload finished but shards.zip is not readable")
        day["shards_archive_uri"] = uri(drive, archive_item)
    finally:
        if os.path.exists(archive_path):
            os.remove(archive_path)

    upload(drive, "%s/%s/day.json" % (base, args.date),
           json.dumps(day, ensure_ascii=False).encode("utf-8"), tok)

    # Counts, never the destination.
    print("day %s: %d tenders, %d documents, %.1f MB of text, shards %s present%s"
          % (args.date, day["counts"]["tenders"], day["counts"]["documents"],
             day["counts"]["chars"] / 1e6, day["shards_present"],
             "" if day["complete"] else ", MISSING %s" % day["shards_missing"]))
    print("  shards.zip: %d files, %.1f MB" %
          (archive_files, archive_size / 1e6))
    for row in day["slice_load"]:
        print("  slice %d: %d tenders, %.1f MB" % (row["slice"], row["tenders"],
                                                   row["chars"] / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
