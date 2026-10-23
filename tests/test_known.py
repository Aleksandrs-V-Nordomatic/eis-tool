#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Which notices the window's dates already delivered — the list a shard leaves out.

The dates read are the window's and the folder's, never more than a month of them: a laptop
that was off for a week asks for a week, and a typo in a date must not walk a year of folders.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import known


class Window(unittest.TestCase):
    def test_the_window_is_every_date_from_its_start_to_its_end(self):
        self.assertEqual(known.window("2026-10-22", "2026-10-23", "2026-10-23"),
                         ["2026-10-22", "2026-10-23"])

    def test_the_folder_the_run_is_filed_under_is_read_too(self):
        self.assertEqual(known.window("2026-10-22", "2026-10-22", "2026-10-23"),
                         ["2026-10-22", "2026-10-23"])

    def test_no_window_reads_nothing(self):
        self.assertEqual(known.window("", "", "2026-10-23"), [])

    def test_a_long_window_is_capped_at_its_newest_dates(self):
        days = known.window("2026-01-01", "2026-10-23", "2026-10-23")
        self.assertEqual(len(days), known.MAX_DAYS)
        self.assertEqual(days[-1], "2026-10-23")


class Known(unittest.TestCase):
    def test_each_dates_notices_are_unioned_and_casefolded(self):
        lists = {"b/2026-10-22/day.json": {"notices": ["A1"]},
                 "b/2026-10-23/day.json": {"notices": ["b2", "a1"]}}
        self.addCleanup(setattr, known, "json_at", known.json_at)
        known.json_at = lambda drive, path, tok: lists.get(path)
        self.assertEqual(known.known("d", "b", ["2026-10-22", "2026-10-23", "2026-10-24"], "t"),
                         ["a1", "b2"])

    def test_without_graph_coordinates_the_file_is_written_empty(self):
        out = os.path.join(tempfile.mkdtemp(prefix="eis_known_"), "known.txt")
        saved = os.environ.pop("GRAPH_DRIVE_ID", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "GRAPH_DRIVE_ID", saved)
        self.assertEqual(known.main(["--from", "2026-10-23", "--to", "2026-10-23",
                                     "--date", "2026-10-23", "--out", out, "--country", "LV"]), 0)
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")


if __name__ == "__main__":
    unittest.main()
