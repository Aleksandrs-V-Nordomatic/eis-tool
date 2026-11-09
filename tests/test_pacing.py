#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Courtesy sized by the load we impose, not by how the platform sliced its metadata.

A procurement split into many lots gets one document container per lot, so it carries a
large number of small records. Under a flat pause that costs many minutes of waiting while
the bytes stay ordinary: the wait was for a data model, not for any load we imposed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eis_fetch


class Pause(unittest.TestCase):
    def test_a_small_record_costs_the_floor(self):
        self.assertAlmostEqual(eis_fetch.pause_for(95 * 1024), eis_fetch.PAUSE_FLOOR, places=1)

    def test_a_large_record_still_costs_the_full_pause(self):
        self.assertEqual(eis_fetch.pause_for(40 * 1048576), eis_fetch.PAUSE_BETWEEN_RECORDS)

    def test_it_never_exceeds_the_old_flat_value(self):
        # The change may only ever make us more polite per megabyte, never less.
        for mb in (0, 1, 5, 10, 100, 1000):
            self.assertLessEqual(eis_fetch.pause_for(int(mb * 1048576)),
                                 eis_fetch.PAUSE_BETWEEN_RECORDS)

    def test_it_never_drops_below_the_floor(self):
        for size in (0, -1, 1, 1024):
            self.assertGreaterEqual(eis_fetch.pause_for(size), eis_fetch.PAUSE_FLOOR)

    def test_it_rises_with_size(self):
        sizes = [0, 1048576, 4 * 1048576, 8 * 1048576]
        waits = [eis_fetch.pause_for(s) for s in sizes]
        self.assertEqual(waits, sorted(waits))

    def test_a_zero_pause_disables_it_entirely(self):
        old = eis_fetch.PAUSE_BETWEEN_RECORDS
        eis_fetch.PAUSE_BETWEEN_RECORDS = 0
        try:
            self.assertEqual(eis_fetch.pause_for(10 * 1048576), 0.0)
        finally:
            eis_fetch.PAUSE_BETWEEN_RECORDS = old

    def test_the_lot_explosion_costs_minutes_instead_of_ten_minutes(self):
        # A heavily subdivided tender: many waits after many small records.
        by_volume = sum(eis_fetch.pause_for(95 * 1024) for _ in range(135))
        flat = 135 * eis_fetch.PAUSE_BETWEEN_RECORDS
        self.assertLess(by_volume, flat / 4)


if __name__ == "__main__":
    unittest.main()
