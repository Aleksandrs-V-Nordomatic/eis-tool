#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The id space: what the register cannot see, and how to look at it without a sweep.

Discovery walks the register, so a procurement that carries no register notice is not
discovered — `eis_tool.discover` says so in as many words. Measured by walking ids
179550-179800 and putting every page to the register itself, that is a fifth of what the
platform publishes: mostly unregulated procurements, market consultations, and closed
competitions inside a dynamic purchasing system.

The only other way to enumerate them is to ask the platform for ids, which `eis_page.walk_ids`
does. This file decides WHICH ids to ask about tonight, and remembers the answers so the
same dead id is not asked about every night forever. It performs no I/O: `idwalk.py` fetches,
`collect_day.py` writes the state back, and both leave the arithmetic here where it can be
tested.

WHY A FRONTIER ALONE IS NOT ENOUGH, MEASURED. An id is assigned when the record is created
and the record publishes whenever the buyer is ready, so publication order is not id order
and the space below the newest id keeps filling in. Over 72 published procurements sampled on 1 Sep 2026,
the gap between a notice's own id and the highest id published by that date was 77 at the
median, 1 760 at the ninth decile — and 9 972 once. A walk that started at the newest id and
only ever went up would have missed that one entirely, and it was the best of the 72.

So the state is not a number. It is the frontier, the ids known to be live, and the ids that
have been asked and were not published yet — because "not published yet" is the answer that
expires, and it is the whole reason to ask again.
"""

SCHEMA = "idspace/1"

# HOW FAR BELOW THE FRONTIER TO KEEP ASKING, and how many questions a night costs.
#
# The window is where the answer can still change: an id far enough below the frontier that
# it never published is one whose record was abandoned, and asking about it again buys
# nothing. 2 000 covers the ninth decile of the measured lag; the tail beyond it is reached
# by the rotation over several nights rather than by a wider sweep, because a wider sweep is
# paid to a public portal every night and the tail is one tender in seventy.
DEFAULT_WIDTH = 2000
DEFAULT_BUDGET = 500
# HOW MANY OF THE ANSWERS MAY REACH THE FETCH IN ONE RUN.
#
# The budget caps the questions; this caps what the answers cost. Half the ids in the window
# are live, so a first walk over an unknown window finds them by the hundred — and hands them
# to a fetch that downloads every document of every one. The first night would be an order of
# magnitude larger than the day the pipeline is sized for, against a public portal, and every
# night after it would be nearly nothing.
#
# So a found id is remembered as live and queued, and the queue drains at a steady rate. The
# work is the same work; it arrives at the speed the rest of the run was built for.
DEFAULT_HANDOVER = 10
# Above the frontier there is no history to consult, so the walk goes up until the misses
# say the space has run out. `eis_page.walk_ids` stops after 40 consecutive misses; the
# forward reach here is that plus room for one more gap, and it is the only part of the plan
# that runs every night whatever the state says.
FORWARD_REACH = 80


def empty():
    """The state before anything has been asked."""
    return {"schema": SCHEMA, "frontier": None, "updated": None, "live": [], "blank": {},
            "pending": []}


def load(raw):
    """A state dict from whatever was stored, or a fresh one.

    Anything unreadable is a fresh state rather than an error: losing the memory costs a few
    nights of re-probing, and refusing to run costs every night after it.
    """
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        return empty()
    state = empty()
    state["frontier"] = raw.get("frontier") if isinstance(raw.get("frontier"), int) else None
    state["updated"] = raw.get("updated")
    state["live"] = sorted({int(x) for x in (raw.get("live") or []) if str(x).isdigit()})
    blank = {}
    for key, entry in (raw.get("blank") or {}).items():
        if not str(key).isdigit() or not isinstance(entry, dict):
            continue
        blank[str(int(key))] = {"n": int(entry.get("n") or 1), "last": entry.get("last")}
    state["blank"] = blank
    state["pending"] = sorted({int(x) for x in (raw.get("pending") or []) if str(x).isdigit()})
    return state


def plan(state, budget=DEFAULT_BUDGET, width=DEFAULT_WIDTH, forward=FORWARD_REACH):
    """The ids to ask about tonight, in the order they matter.

    Forward first, because an id above the frontier has never been asked about and is where
    today's publications land. Then the backfill, ordered by how little is known about an
    id: never asked before anything asked once, and among equals the newest, because the
    lag measurement says a young id is the likelier to publish.

    Returns [] when there is no frontier yet — the first night has nothing to walk from, and
    guessing a start would either sweep the whole space or skip most of it. `collect_day.py`
    seeds the frontier from the day it just delivered, so the walk begins on the next run.
    """
    if not state.get("frontier"):
        return []
    frontier = int(state["frontier"])
    live = set(state.get("live") or [])
    blank = state.get("blank") or {}

    ahead = [pid for pid in range(frontier + 1, frontier + forward + 1) if pid not in live]

    def rank(pid):
        entry = blank.get(str(pid))
        # (times asked, oldest answer first, newest id first) — a tuple that puts the
        # never-asked at the front without a special case for them.
        return (entry["n"] if entry else 0, (entry or {}).get("last") or "", -pid)

    behind = sorted((pid for pid in range(max(frontier - width, 1), frontier + 1)
                     if pid not in live), key=rank)

    return (ahead + behind)[:max(int(budget), 0)]


def take_shard(ids, shard, of, owner=None):
    """This runner's slice of tonight's questions.

    `owner(pid)` names the runner an id belongs to, and the caller passes the SAME rule the
    fetch uses. That is not tidiness: a published id found here is handed to `batch.py` as a
    target, and `batch.take_shard` slices the target list again by its own digest. A walk
    that sliced by anything else would find an id on one runner and hand it to another,
    which then does not have it — the id would be asked about and never fetched.

    Without an owner the slice is positional, which is enough for a caller with one runner
    and for the tests.
    """
    if not of or int(of) <= 1:
        return list(ids)
    if owner is not None:
        return [pid for pid in ids if owner(pid) == int(shard)]
    return [pid for index, pid in enumerate(ids) if index % int(of) == (int(shard) - 1) % int(of)]


def hand_over(state, found, limit=DEFAULT_HANDOVER, owner=None, shard=None):
    """Which live ids this run gives the fetch: the queue first, then tonight's finds.

    The queue first because an id that has waited a night has already waited longer than the
    one found a minute ago, and a queue that is never drained in order is a queue that loses
    its tail. Newest first inside each, because a young id is the likelier to still be open.

    `owner` and `shard` slice the queue the way the fetch will slice the targets, so a runner
    never hands over an id that another runner would have to fetch.
    """
    queued = [pid for pid in sorted(set(state.get("pending") or []), reverse=True)]
    fresh = [pid for pid in sorted(set(found), reverse=True) if pid not in set(queued)]
    mine = queued + fresh
    if owner is not None and shard is not None:
        mine = [pid for pid in mine if owner(pid) == int(shard)]
    return mine[:max(int(limit), 0)]


def merge(state, probes, today, discovered=(), handed=()):
    """The state after tonight: what was asked, what answered, and what the day delivered.

    `probes` is {id: bool} — True when the page was published. `discovered` is every id the
    register produced today, folded in so the walk never spends a question on a tender the
    register was going to hand over anyway. `handed` is what actually reached the fetch, and
    it is the only thing that takes an id off the queue: an id found and not handed over is
    remembered as owed, or the cap on the handover would be a quiet way of losing tenders.
    """
    state = load(state)
    live = set(state["live"]) | {int(pid) for pid in discovered if str(pid).isdigit()}
    blank = dict(state["blank"])
    pending = set(state.get("pending") or [])
    pending -= {int(pid) for pid in discovered if str(pid).isdigit()}

    for pid, published in (probes or {}).items():
        pid = int(pid)
        if published:
            live.add(pid)
            pending.add(pid)
            blank.pop(str(pid), None)
        else:
            entry = blank.get(str(pid)) or {"n": 0, "last": None}
            blank[str(pid)] = {"n": entry["n"] + 1, "last": today}

    # An id that is only known blank does not move the frontier: the space above the newest
    # LIVE id is where the walk is still looking, and letting a run of dead ids push the
    # frontier up would quietly retire the window they sit in.
    frontier = max(state["frontier"] or 0, max(live) if live else 0)

    pending -= {int(pid) for pid in handed if str(pid).isdigit()}

    return {"schema": SCHEMA, "frontier": frontier or None, "updated": today,
            "live": sorted(live), "pending": sorted(pending),
            # An id asked about many times and never published is a record nobody finished.
            # Keeping it costs a line of state forever and it can no longer be asked about
            # anyway once it falls out of the window, so it is forgotten there.
            "blank": {pid: entry for pid, entry in blank.items()
                      if frontier - int(pid) <= DEFAULT_WIDTH}}
