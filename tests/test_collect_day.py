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
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collect_day


def tender(pid, docs):
    return {"pid": pid, "key": "EIS:%s" % pid, "title": "Tender %s" % pid,
            "documents": [{"chars": c} for c in docs], "unreadable": []}


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
            return {"tenders": self.shards[n]}
        return None

    def _text_at(self, drive, path, tok):
        n = self._shard_of(path)
        return self.failed.get(n, "") if path.endswith("failed.txt") else ""

    def _item_at(self, drive, path, tok):
        return {"id": "item-" + path.rsplit("/", 1)[-1]}


class TheList(unittest.TestCase):
    def collect(self, shards, failed=None, slices=4, expected=4):
        FakeDrive(shards, failed).install(self)
        return collect_day.collect("drive1", "base", "2026-08-11", expected, slices, "run7", "t")

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
        day = self.collect({1: [tender("111", [10])]}, expected=1)
        t = day["tenders"][0]
        self.assertEqual(t["uri"], "file:///drive1/item-111.zip")
        self.assertEqual(t["archive_uri"], t["uri"])
        self.assertEqual(t["index_uri"], "file:///drive1/item-111.index.json")
        self.assertEqual(t["index_path"], "111.index.json")
        self.assertEqual(t["path"], "2026-08-11/shards/eis-batch-shard-1/111.zip")
        self.assertEqual(day["shards_archive_path"], "2026-08-11/shards.zip")
        self.assertIsNone(day["shards_archive_uri"])

    def test_one_tender_delivered_twice_is_one_tender(self):
        day = self.collect({1: [tender("111", [10])], 2: [tender("111", [10])]}, expected=2)
        self.assertEqual([t["pid"] for t in day["tenders"]], ["111"])

    def test_the_list_survives_a_day_with_nothing_in_it(self):
        day = self.collect({1: [], 2: [], 3: [], 4: []})
        self.assertEqual(day["counts"]["tenders"], 0)
        self.assertTrue(day["complete"])          # nothing published is a sound day
        self.assertEqual(len(day["slice_load"]), 4)




class ShardsArchive(unittest.TestCase):
    def setUp(self):
        self.root = "base/2026-08-11/shards/eis-batch-shard-1"
        index = {"tenders": [{"pid": "111", "archive": "111.zip",
                               "index_file": "111.index.json"}]}
        self.files = {
            self.root + "/done.txt": b"111\n",
            self.root + "/failed.txt": b"",
            self.root + "/resolved.tsv": b"url\t111\n",
            self.root + "/111.zip": b"tender archive bytes",
            self.root + "/111.index.json": b'{"pid":"111"}',
            self.root + "/index.json": json.dumps(index).encode("utf-8"),
        }
        self._bytes_at = collect_day.bytes_at
        collect_day.bytes_at = lambda drive, path, tok: self.files.get(path)
        fh = tempfile.NamedTemporaryFile(prefix="eis_shards_test_", suffix=".zip",
                                         delete=False)
        self.path = fh.name
        fh.close()

    def tearDown(self):
        collect_day.bytes_at = self._bytes_at
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_it_mirrors_the_shards_folder(self):
        count, size = collect_day.build_shards_archive(
            "drive", "base", "2026-08-11", 1, "token", self.path)
        self.assertEqual(count, 6)
        self.assertGreater(size, 0)
        with zipfile.ZipFile(self.path) as zf:
            self.assertEqual(zf.namelist(), [
                "shards/eis-batch-shard-1/done.txt",
                "shards/eis-batch-shard-1/failed.txt",
                "shards/eis-batch-shard-1/resolved.tsv",
                "shards/eis-batch-shard-1/111.zip",
                "shards/eis-batch-shard-1/111.index.json",
                "shards/eis-batch-shard-1/index.json",
            ])
            self.assertEqual(zf.read("shards/eis-batch-shard-1/111.zip"),
                             b"tender archive bytes")

    def test_a_missing_named_archive_fails_the_day(self):
        del self.files[self.root + "/111.zip"]
        with self.assertRaises(SystemExit):
            collect_day.build_shards_archive(
                "drive", "base", "2026-08-11", 1, "token", self.path)

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


if __name__ == "__main__":
    unittest.main()
