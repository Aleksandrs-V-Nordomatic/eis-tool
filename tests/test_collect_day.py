#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The day's list, tested without a drive.

`day.json` exists because folders lie: a date fetched twice holds tenders from both runs and
nothing in a folder says which. These tests hold the properties a reader depends on — the list
names every tender that landed and only those, a missing shard is visible rather than absent,
and the four slices are levelled by characters rather than by count.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collect_day


def tender(pid, docs, status="new", change=None):
    """One line of a shard index, as `deliver_graph` writes it.

    The change record rides inside the entry rather than beside it. Reading it back would be
    one more Graph request per tender against an endpoint that already answers 429 under this
    day's load, and it is a few hundred bytes the shard index is already fetching.
    """
    return {"pid": pid, "key": "EIS:%s" % pid, "title": "Tender %s" % pid,
            "documents": [{"chars": c} for c in docs], "unreadable": [],
            "home": "tenders/%s" % pid, "archive": "%s.zip" % pid,
            "index_file": "index.json", "run_file": "runs/2026-08-11.json",
            "status": status,
            "change": change or {"schema": "tender-change/1", "pid": pid, "status": status}}


class FakeDrive(object):
    """Just enough drive to answer the three reads collect() makes."""

    def __init__(self, shards, failed=None):
        self.shards = shards                      # {shard number: [tender dicts]}
        self.failed = failed or {}

    def install(self, test):
        test.addCleanup(setattr, collect_day, "json_at", collect_day.json_at)
        test.addCleanup(setattr, collect_day, "text_at", collect_day.text_at)
        test.addCleanup(setattr, collect_day, "item_at", collect_day.item_at)
        collect_day.json_at = self._json_at
        collect_day.text_at = self._text_at
        collect_day.item_at = self._item_at

    def _shard_of(self, path):
        for n in self.shards:
            if "eis-batch-shard-%d/" % n in path + "/":
                return n
        return None

    def _json_at(self, drive, path, tok):
        n = self._shard_of(path)
        if n is None or n not in self.shards:
            return None
        if path.endswith("/index.json") and path.count("/") == 4:
            body = self.shards[n]
            return body if isinstance(body, dict) else {"tenders": body}
        return None

    def _text_at(self, drive, path, tok):
        n = self._shard_of(path)
        return self.failed.get(n, "") if path.endswith("failed.txt") else ""

    def _item_at(self, drive, path, tok):
        return {"id": "item-" + path.rsplit("/", 1)[-1]}


class TheList(unittest.TestCase):
    def both(self, shards, failed=None, slices=4, expected=4):
        FakeDrive(shards, failed).install(self)
        return collect_day.collect("drive1", "base", "2026-08-11", expected, slices, "run7", "t")

    def collect(self, *a, **kw):
        return self.both(*a, **kw)[0]

    def changes(self, *a, **kw):
        return self.both(*a, **kw)[1]

    def test_it_names_every_tender_that_landed(self):
        day = self.collect({1: [tender("111", [10])], 2: [tender("222", [20])],
                            3: [tender("333", [30])], 4: [tender("444", [40])]})
        self.assertEqual([t["pid"] for t in day["tenders"]], ["111", "222", "333", "444"])
        self.assertEqual(day["counts"]["tenders"], 4)
        self.assertTrue(day["complete"])
        self.assertEqual(day["shards_missing"], [])

    def test_a_shard_that_delivered_nothing_is_visible(self):
        # The failure this file exists for: three shards each look perfect on their own.
        day = self.collect({1: [tender("111", [10])], 2: [tender("222", [20])],
                            4: [tender("444", [40])]})
        self.assertFalse(day["complete"])
        self.assertEqual(day["shards_missing"], [3])
        self.assertEqual(day["shards_present"], [1, 2, 4])
        self.assertEqual(day["counts"]["tenders"], 3)

    def test_what_a_shard_lost_travels_with_the_list(self):
        day = self.collect({1: [tender("111", [10])]}, failed={1: "999 timed out\n"},
                           expected=1)
        self.assertEqual(day["lost"], [{"shard": 1, "entry": "999 timed out"}])

    def test_every_tender_carries_the_address_a_reader_uses(self):
        # The address is the tender's home, not this day's folder. A day says which tenders
        # moved; the tender itself lives in one place and is complete there whether it was
        # first fetched this morning or four months ago.
        day = self.collect({1: [tender("111", [10])]}, expected=1)
        t = day["tenders"][0]
        self.assertEqual(t["uri"], "file:///drive1/item-111.zip")
        self.assertEqual(t["archive_uri"], t["uri"])
        self.assertEqual(t["index_uri"], "file:///drive1/item-index.json")
        self.assertEqual(t["index_path"], "index.json")
        self.assertEqual(t["path"], "tenders/111/111.zip")
        self.assertEqual(t["folder_path"], "tenders/111")
        # This day's record for this tender lives in the tender, not in the day folder.
        self.assertEqual(t["run_path"], "tenders/111/runs/2026-08-11.json")
        self.assertEqual(day["changes_path"], "2026-08-11/changes.json")
        self.assertEqual(day["shards_path"], "2026-08-11/shards")
        self.assertNotIn("shards_archive_path", day)

    def test_one_tender_delivered_twice_is_one_tender(self):
        day = self.collect({1: [tender("111", [10])], 2: [tender("111", [10])]}, expected=2)
        self.assertEqual([t["pid"] for t in day["tenders"]], ["111"])

    def test_the_list_survives_a_day_with_nothing_in_it(self):
        day = self.collect({1: [], 2: [], 3: [], 4: []})
        self.assertEqual(day["counts"]["tenders"], 0)
        self.assertTrue(day["complete"])          # nothing published is a sound day
        self.assertEqual(len(day["slice_load"]), 4)


class AShardFromAnEarlierRun(unittest.TestCase):
    """A day folder outlives the run that filled it, and the list must not be fooled by that.

    Measured: a shard died mid-delivery on the second fetch of one date, its index from the
    first fetch was still on the drive, and `day.json` reported all four shards present while
    missing a quarter of the day's tenders. That is the exact failure this file exists to
    prevent, arriving by a door it did not have before.
    """

    def collect(self, shards, run_id="run7"):
        FakeDrive(shards).install(self)
        return collect_day.collect("drive1", "base", "2026-08-11", 2, 4, run_id, "t")

    def index(self, run_id, pids=("111",)):
        return {"run_id": run_id, "tenders": [tender(p, [10]) for p in pids]}

    def test_an_index_left_by_an_earlier_run_is_not_this_run(self):
        day, _ = self.collect({1: self.index("run7"), 2: self.index("run6", ("222",))})
        self.assertFalse(day["complete"])
        self.assertEqual(day["shards_missing"], [2])
        self.assertEqual(day["shards_stale"], [2])
        self.assertEqual([t["pid"] for t in day["tenders"]], ["111"])

    def test_this_run_own_shards_are_present(self):
        day, _ = self.collect({1: self.index("run7"), 2: self.index("run7", ("222",))})
        self.assertTrue(day["complete"])
        self.assertEqual(day["shards_stale"], [])

    def test_an_index_naming_no_run_is_trusted(self):
        # A delivery run by hand names no run. Refusing its index would make this check the
        # thing that loses a day.
        day, _ = self.collect({1: self.index(None), 2: self.index(None, ("222",))})
        self.assertTrue(day["complete"])

    def test_and_so_is_every_index_when_the_collector_names_no_run(self):
        day, _ = self.collect({1: self.index("run6"), 2: self.index("run5", ("222",))},
                              run_id="")
        self.assertTrue(day["complete"])


class ATargetNobodyFetched(unittest.TestCase):
    """The shards divide the day without talking, and sometimes they lose a piece of it.

    Each walks the register for itself and bin-packs the result; one notice weighed
    differently by one shard reshuffles much of the assignment. Measured on a four-shard run:
    all four agreed on 93 targets, the slices came to 21+23+23+23, and ninety assignments
    covered sixty-eight distinct tenders — about two dozen fetched by nobody, on a day that
    called itself complete because every shard had delivered something.
    """

    def collect(self, shards):
        FakeDrive(shards).install(self)
        return collect_day.collect("drive1", "base", "2026-08-11", len(shards), 4, "run7", "t")

    def index(self, pids, targets, failed=(), withdrawn=()):
        return {"run_id": "run7",
                "accounts": {"targets": list(targets), "failed": list(failed),
                             "withdrawn": list(withdrawn)},
                "tenders": [tender(p, [10]) for p in pids]}

    ALL = ["eis:111", "eis:222", "eis:333"]

    def test_a_target_that_reached_no_shard_makes_the_day_short(self):
        day, _ = self.collect({1: self.index(["111"], self.ALL),
                               2: self.index(["222"], self.ALL)})
        self.assertFalse(day["complete"])
        self.assertEqual(day["coverage"]["unaccounted"], ["eis:333"])
        self.assertEqual(day["coverage"]["targets"], 3)
        self.assertEqual(day["coverage"]["delivered"], 2)

    def test_a_day_that_covered_its_targets_is_whole(self):
        day, _ = self.collect({1: self.index(["111"], self.ALL),
                               2: self.index(["222", "333"], self.ALL)})
        self.assertTrue(day["complete"])
        self.assertEqual(day["coverage"]["unaccounted"], [])

    def test_a_tender_the_portal_refused_is_not_counted_against_the_day(self):
        # It was asked for, it was answered, and the answer was no. That is settled work,
        # not a slice that fell between two shards.
        day, _ = self.collect({1: self.index(["111"], self.ALL, withdrawn=["eis:333"]),
                               2: self.index(["222"], self.ALL)})
        self.assertTrue(day["complete"])
        self.assertEqual(day["coverage"]["excused"], 1)

    def test_nor_is_one_whose_extraction_failed(self):
        day, _ = self.collect({1: self.index(["111"], self.ALL, failed=["eis:333"]),
                               2: self.index(["222"], self.ALL)})
        self.assertTrue(day["complete"])

    def test_a_shard_index_from_before_this_existed_costs_nothing(self):
        # No accounts means nothing to compare, not a day declared short on no evidence.
        day, _ = self.collect({1: {"run_id": "run7", "tenders": [tender("111", [10])]},
                               2: {"run_id": "run7", "tenders": [tender("222", [10])]}})
        self.assertTrue(day["complete"])
        self.assertEqual(day["coverage"]["targets"], 0)

    def test_the_diff_says_so_too(self):
        _, changes = self.collect({1: self.index(["111"], self.ALL),
                                   2: self.index(["222"], self.ALL)})
        self.assertFalse(changes["complete"])
        self.assertEqual(changes["unaccounted"], ["eis:333"])


class TheDiff(unittest.TestCase):
    """`changes.json` — the file a consumer that has read this day's tenders before opens.

    The list says what a day contains; this says what it did. On an ordinary day those are
    very different lengths, and the difference is the entire argument for fetching a tender
    that was fetched last week.
    """

    def collect(self, shards, expected=4):
        FakeDrive(shards).install(self)
        return collect_day.collect("drive1", "base", "2026-08-11", expected, 4, "run7", "t")

    def test_it_carries_one_record_per_tender_the_day_touched(self):
        _, changes = self.collect({1: [tender("111", [10], status="new")],
                                   2: [tender("222", [20], status="changed")],
                                   3: [tender("333", [30], status="unchanged")],
                                   4: []})
        self.assertEqual([t["pid"] for t in changes["tenders"]], ["111", "222", "333"])
        self.assertEqual(changes["counts"],
                         {"new": 1, "changed": 1, "unchanged": 1, "tenders": 3})
        self.assertTrue(changes["complete"])

    def test_the_status_reaches_the_list_as_well(self):
        # A consumer holding day.json can stop on "unchanged" without opening anything.
        day, _ = self.collect({1: [tender("111", [10], status="unchanged")]}, expected=1)
        self.assertEqual(day["tenders"][0]["status"], "unchanged")
        self.assertEqual(day["counts"]["unchanged"], 1)

    def test_a_short_day_is_marked_short_here_too(self):
        # The list already refuses to look whole when a shard is missing. The diff must
        # refuse just as loudly, because it is the file most likely to be read alone.
        _, changes = self.collect({1: [tender("111", [10])]})
        self.assertFalse(changes["complete"])
        self.assertEqual(changes["shards_missing"], [2, 3, 4])

    def test_each_record_says_where_it_can_be_read_on_its_own(self):
        _, changes = self.collect({1: [tender("111", [10])]}, expected=1)
        self.assertEqual(changes["tenders"][0]["path"], "tenders/111/runs/2026-08-11.json")




class Slices(unittest.TestCase):
    def test_the_split_levels_characters_not_tenders(self):
        # Two of these four are twenty times the others. Handing out two apiece would give one
        # slice 190 characters and the other 15; levelling the load gives 105 and 100.
        tenders = [{"pid": "a", "chars": 100}, {"pid": "b", "chars": 90},
                   {"pid": "c", "chars": 10}, {"pid": "d", "chars": 5}]
        bins = collect_day.balance(tenders, 2)
        self.assertEqual(sorted(b["chars"] for b in bins), [100, 105])
        self.assertEqual(sorted(len(b["pids"]) for b in bins), [2, 2])

    def test_every_tender_lands_in_exactly_one_slice(self):
        tenders = [{"pid": str(i), "chars": i * 7 % 13} for i in range(20)]
        bins = collect_day.balance(tenders, 4)
        placed = [pid for b in bins for pid in b["pids"]]
        self.assertEqual(sorted(placed), sorted(t["pid"] for t in tenders))
        self.assertEqual(len(placed), len(set(placed)))
        for t in tenders:
            self.assertIn(t["slice"], (1, 2, 3, 4))

    def test_more_slices_than_tenders_leaves_empty_ones_rather_than_failing(self):
        bins = collect_day.balance([{"pid": "a", "chars": 1}], 4)
        self.assertEqual(sum(len(b["pids"]) for b in bins), 1)
        self.assertEqual(len(bins), 4)


class TheIdSpace(unittest.TestCase):
    """The one writer of the walk's memory, and what it may write.

    Every shard asks about a slice of the night's ids and leaves its answers behind; this is
    the only step that sees all four, and the only one that runs after all four.
    """

    def install(self, reports, prior=None, day_pids=()):
        stored = {}

        def json_at(drive, path, tok):
            if path.endswith("/idspace.json"):
                return prior
            for n, report in reports.items():
                if "eis-batch-shard-%d/idprobe.json" % n in path:
                    return report
            return None

        self.addCleanup(setattr, collect_day, "json_at", collect_day.json_at)
        collect_day.json_at = json_at
        day = {"tenders": [{"pid": p} for p in day_pids]}
        return collect_day.idspace_after("drive1", "base", "2026-09-02", 4, day, "t"), stored

    def test_the_shards_answers_are_merged(self):
        after, _ = self.install({1: {"probes": {"1001": True}},
                                 3: {"probes": {"1002": False, "1003": True}}},
                                prior={"schema": "idspace/1", "frontier": 1000, "live": [1000]})
        self.assertEqual(after["live"], [1000, 1001, 1003])
        self.assertEqual(after["blank"]["1002"]["n"], 1)
        self.assertEqual(after["frontier"], 1003)

    def test_the_day_seeds_a_frontier_before_the_walk_has_ever_run(self):
        """First night: nothing was planned because there was no frontier. What the
        register delivered becomes one, and the walk starts on the next run."""
        after, _ = self.install({}, prior=None, day_pids=["1200", "1198"])
        self.assertEqual(after["frontier"], 1200)

    def test_nothing_asked_and_nothing_delivered_writes_nothing(self):
        self.assertIsNone(self.install({}, prior=None)[0])

    def test_a_shard_that_never_reported_is_simply_absent(self):
        after, _ = self.install({2: {"probes": {"1001": True}}},
                                prior={"schema": "idspace/1", "frontier": 1000, "live": [1000]})
        self.assertEqual(after["live"], [1000, 1001])


if __name__ == "__main__":
    unittest.main()
