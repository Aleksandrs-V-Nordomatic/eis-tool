#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Which register notices the dates of this window already settled, one uuid per line.

    python3 known.py --from 2026-10-22 --to 2026-10-23 --date 2026-10-23 --out known.txt

A date is fetched many times a day — every half hour from the owner's laptop since 2026-10-23
— and every run asks the register for the whole window again. `collect_day.py` keeps each
date's settled notices in its `day.json`; this reads them for every date of the window and
the folder the run is filed under, so `batch.py --skip-notices` leaves those tenders out. A
new notice on a known tender is not in the list, so that tender is fetched: it is a change.

Reads only, and prints a count, never a path. Without Graph coordinates the file is written
empty, and an empty file means: fetch the whole window.
"""

import argparse
import datetime as dt
import os
import sys

import country
from deliver_graph import env, graph_token, json_at

MAX_DAYS = 31


def window(date_from, date_to, date):
    """The dates to read: the window's own, its newest `MAX_DAYS`, and the folder's."""
    if not (date_from or date_to):
        return []
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    end = dt.date.fromisoformat(date_to or date or today)
    start = dt.date.fromisoformat(date_from or date_to)
    start = max(start, end - dt.timedelta(days=MAX_DAYS - 1))
    days = {(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}
    if date:
        days.add(date)
    return sorted(days)


def known(drive, base, days, tok):
    found = set()
    for day in days:
        listed = json_at(drive, "%s/%s/day.json" % (base, day), tok) or {}
        found.update(str(u).casefold() for u in listed.get("notices") or [])
    return sorted(found)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write the notices this window's dates already hold.")
    ap.add_argument("--from", dest="date_from", default="")
    ap.add_argument("--to", dest="date_to", default="")
    ap.add_argument("--date", default="", help="the folder this run is filed under")
    ap.add_argument("--out", required=True)
    country.add_argument(ap)
    args = ap.parse_args(argv)

    days = window(args.date_from, args.date_to, args.date)
    found = []
    if days and (os.environ.get("GRAPH_DRIVE_ID") or "").strip():
        drive = env("GRAPH_DRIVE_ID")
        base = country.destination(env("GRAPH_DEST_ROOT"),
                                   country.resolve(args.country, os.environ))
        found = known(drive, base, days, graph_token())
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("".join(u + "\n" for u in found))
    print("%d notice(s) already settled in %d date(s) - not fetched again"
          % (len(found), len(days)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
