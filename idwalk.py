#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Step 0 of 4 — the ids the register cannot hand over. Deterministic. No model.

    python3 idwalk.py --shard 1 --of 4 --out extra_targets.txt

Discovery walks the register, and a procurement below the publication duty never reaches
the register: unregulated procurements, market consultations, and closed competitions inside
a dynamic purchasing system. `eis_tool.discover` names this and refuses to pretend otherwise.
This asks the platform directly instead — a page at a time, for a budgeted handful of ids a
night — and writes the ids that turned out to be published so the ordinary fetch can pick
them up in the same run.

WHAT IT IS NOT. Not a sweep, and never one: the id space is thousands wide and the portal is
a public service. `idspace.plan` decides which few to ask about, and this file only asks.

The state it plans against lives with the delivery, at `<country>/idspace.json`, because a
runner is new every night and the previous run's artifact is exactly what a consumer cannot
reach — the same reason `state.json` sits beside each tender. This step READS it; the day's
own collector writes it, once, after every shard has reported. One writer, no conflict.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

import batch
import country
import eis_fetch
import eis_page
import idspace


def say(line):
    """Progress goes to stderr; stdout carries the target list and nothing else."""
    print(line, file=sys.stderr, flush=True)


def env(name):
    value = os.environ.get(name, "").strip()
    return value or None


def read_state(path=None):
    """The id space as it stood after the last run.

    A local path is for tests and for a laptop. With none given the state comes off the
    drive the delivery writes to, and an absent or unreadable one is a fresh state: losing
    the memory costs a few nights of asking again, and refusing to run costs every night.
    """
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return idspace.load(json.load(fh))
        except (OSError, ValueError):
            return idspace.empty()

    drive, root = env("GRAPH_DRIVE_ID"), env("GRAPH_DEST_ROOT")
    if not (drive and root):
        return idspace.empty()
    import deliver_graph
    base = country.destination(root, env("EIS_COUNTRY"))
    return idspace.load(deliver_graph.json_at(drive, "%s/idspace.json" % base,
                                              deliver_graph.graph_token()))


# THE FOUR ANSWERS AN ID CAN GIVE, AND THEY ARE NOT THREE.
#
#   published   a procurement page a guest can read: a target
#   blank       not published, or published with no stage a guest may see. Either way there
#               is nothing to fetch today and there may be tomorrow, so it is written down
#               as asked-and-answered and asked again later, further down the rotation
#   challenge   EIS's bot check, dressed as HTTP 200. It means stop, not skip: the same
#               request keeps returning it, so the rest of the night's ids would all be
#               spent proving it again
#   unreachable curl could not get an answer at all. Not an answer, and recorded as none:
#               writing it down as "not published" would retire a live id for as long as
#               the state remembers it
PUBLISHED, BLANK, CHALLENGE, UNREACHABLE = "published", "blank", "challenge", "unreachable"


def probe(pid, pause):
    """One id, one page. (one of the four answers, what to say about it)."""
    url = eis_page.PAGE % pid
    workdir = tempfile.mkdtemp(prefix="eis_probe_")
    try:
        html = eis_fetch.Curl(workdir, url).get_text(url)
    except eis_fetch.Fail as exc:
        return UNREACHABLE, str(exc)[:120]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    time.sleep(pause)
    if eis_page.is_robot_check(html):
        return CHALLENGE, "the portal asked us to slow down"
    if eis_page.is_access_denied(html):
        # EIS's own fixed page for an id with no stage a guest may see. Permanent as far as
        # a guest is concerned, so it counts as answered rather than as a failure to ask.
        return BLANK, "no stage a guest may see"
    return (PUBLISHED if eis_page.is_published(html) else BLANK), None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ask the platform about ids the register has no "
                                             "notice for (deterministic).")
    ap.add_argument("--shard", type=int, default=1)
    ap.add_argument("--of", type=int, default=1, help="how many runners split tonight's ids")
    ap.add_argument("--budget", type=int, default=idspace.DEFAULT_BUDGET,
                    help="how many ids ALL runners together may ask about tonight")
    ap.add_argument("--width", type=int, default=idspace.DEFAULT_WIDTH,
                    help="how far below the frontier the backfill reaches")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between pages")
    ap.add_argument("--state", help="read the state from this file instead of the drive")
    ap.add_argument("--out", help="write the published ids here, one per line, for batch.py")
    ap.add_argument("--report", help="write what was asked and answered here, as JSON")
    args = ap.parse_args(argv)

    state = read_state(args.state)
    planned = idspace.plan(state, budget=args.budget, width=args.width)
    # SLICED BY THE FETCH'S OWN RULE. A published id found here becomes a target, and
    # `batch.take_shard` slices the target list again by a digest of the target. Asking on
    # one runner and handing the answer to another means the id is never fetched at all.
    mine = idspace.take_shard(planned, args.shard, args.of,
                              owner=lambda pid: batch.shard_of(eis_page.PAGE % pid, args.of))
    if not planned:
        say("id walk: no frontier yet - the day's collector seeds it, and the walk starts "
            "on the next run")
    say("id walk: %d id(s) planned for tonight, %d on this runner (frontier %s)"
        % (len(planned), len(mine), state.get("frontier")))

    probes, found, unanswered, stopped = {}, [], 0, None
    for pid in mine:
        answer, why = probe(pid, args.pause)
        if answer == CHALLENGE:
            # Everything after this would return the same page. What was asked before it
            # still stands and is reported.
            stopped = why
            break
        if answer == UNREACHABLE:
            unanswered += 1
            continue
        probes[pid] = answer == PUBLISHED
        if answer == PUBLISHED:
            found.append(pid)

    say("id walk: %d asked, %d published, %d unanswered" % (len(probes), len(found), unanswered))
    if stopped:
        say("id walk: stopped early - %s; %d id(s) left for the next run"
            % (stopped, len(mine) - len(probes) - unanswered))

    if args.out:
        with open(args.out, "a", encoding="utf-8") as fh:
            for pid in found:
                fh.write(eis_page.PAGE % pid + "\n")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"schema": "idprobe/1", "shard": args.shard, "of": args.of,
                       "frontier": state.get("frontier"), "planned": len(planned),
                       "probes": {str(pid): published for pid, published in probes.items()},
                       "unreachable": unanswered, "stopped": stopped}, fh,
                      ensure_ascii=False)
    for pid in found:
        print(eis_page.PAGE % pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
