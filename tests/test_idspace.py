#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Which ids get asked about, and what is remembered about the answers.

The arithmetic is here rather than in `idwalk.py` so it can be proved without a portal. What
these protect is the one thing a frontier-only walk gets wrong: an id publishes long after
it is assigned, so the space BELOW the newest id keeps filling in and has to be asked again.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import idspace


class FirstNight(unittest.TestCase):

    def test_no_frontier_means_no_questions(self):
        """Nothing to walk from, and a guessed start either sweeps everything or skips
        most of it. The day's own delivery seeds the frontier instead."""
        self.assertEqual(idspace.plan(idspace.empty()), [])

    def test_rubbish_loads_as_a_fresh_state(self):
        for raw in (None, [], {"schema": "something/else"}, {"live": [1]}):
            self.assertEqual(idspace.load(raw)["frontier"], None)
            self.assertEqual(idspace.load(raw)["live"], [])


class WhatGetsAsked(unittest.TestCase):

    def setUp(self):
        self.state = idspace.load({"schema": idspace.SCHEMA, "frontier": 1000,
                                   "live": [990, 995, 1000], "blank": {}})

    def test_the_walk_goes_above_the_frontier_first(self):
        planned = idspace.plan(self.state, budget=10, width=100, forward=5)
        self.assertEqual(planned[:5], [1001, 1002, 1003, 1004, 1005])

    def test_ids_already_known_live_are_never_asked_again(self):
        planned = idspace.plan(self.state, budget=200, width=100)
        for known in (990, 995, 1000):
            self.assertNotIn(known, planned)

    def test_the_backfill_prefers_the_never_asked(self):
        state = idspace.load({"schema": idspace.SCHEMA, "frontier": 1000, "live": [1000],
                              "blank": {"999": {"n": 3, "last": "2026-08-30"}}})
        planned = idspace.plan(state, budget=12, width=10, forward=0)
        self.assertEqual(planned[0], 998, "the never-asked newest id comes before an old answer")
        self.assertEqual(planned[-1], 999, "the one already asked three times goes last")

    def test_the_budget_is_a_ceiling(self):
        self.assertEqual(len(idspace.plan(self.state, budget=7, width=500)), 7)
        self.assertEqual(idspace.plan(self.state, budget=0, width=500), [])

    def test_the_window_bounds_the_backfill(self):
        planned = idspace.plan(self.state, budget=500, width=10, forward=0)
        self.assertTrue(all(pid >= 990 for pid in planned))

    def test_a_shard_takes_a_scattering_and_the_shards_together_take_everything(self):
        planned = idspace.plan(self.state, budget=40, width=200)
        slices = [idspace.take_shard(planned, n, 4) for n in (1, 2, 3, 4)]
        self.assertEqual(sorted(sum(slices, [])), sorted(planned))
        for one in slices:
            self.assertTrue(one, "every runner gets work")
        self.assertNotEqual(slices[0][:2], planned[:2],
                            "consecutive ids must not all land on one runner")

    def test_one_shard_takes_the_whole_list(self):
        planned = idspace.plan(self.state, budget=9, width=200)
        self.assertEqual(idspace.take_shard(planned, 1, 1), planned)


class WhatIsRemembered(unittest.TestCase):

    def test_a_published_id_becomes_live_and_moves_the_frontier(self):
        after = idspace.merge({"schema": idspace.SCHEMA, "frontier": 1000, "live": [1000]},
                              {1001: True, 1002: False}, "2026-09-02")
        self.assertIn(1001, after["live"])
        self.assertEqual(after["frontier"], 1001)

    def test_a_blank_id_does_not_move_the_frontier(self):
        after = idspace.merge({"schema": idspace.SCHEMA, "frontier": 1000, "live": [1000]},
                              {1001: False, 1002: False}, "2026-09-02")
        self.assertEqual(after["frontier"], 1000)
        self.assertEqual(after["blank"]["1001"]["n"], 1)

    def test_asking_again_counts_again(self):
        first = idspace.merge({"schema": idspace.SCHEMA, "frontier": 1000, "live": [1000]},
                              {999: False}, "2026-09-02")
        second = idspace.merge(first, {999: False}, "2026-09-03")
        self.assertEqual(second["blank"]["999"]["n"], 2)
        self.assertEqual(second["blank"]["999"]["last"], "2026-09-03")

    def test_a_blank_that_publishes_stops_being_blank(self):
        first = idspace.merge({"schema": idspace.SCHEMA, "frontier": 1000, "live": [1000]},
                              {999: False}, "2026-09-02")
        second = idspace.merge(first, {999: True}, "2026-09-03")
        self.assertNotIn("999", second["blank"])
        self.assertIn(999, second["live"])

    def test_what_the_register_delivered_is_never_asked_about(self):
        """The walk exists for what the register cannot see. Spending a question on a
        tender the register hands over anyway is the one waste it must not have."""
        after = idspace.merge(idspace.empty(), {}, "2026-09-02", discovered=["1200", "1198"])
        self.assertEqual(after["frontier"], 1200)
        planned = idspace.plan(after, budget=50, width=10, forward=0)
        self.assertNotIn(1200, planned)
        self.assertNotIn(1198, planned)

    def test_an_id_that_falls_out_of_the_window_is_forgotten(self):
        old = str(2000 - idspace.DEFAULT_WIDTH - 1)
        after = idspace.merge({"schema": idspace.SCHEMA, "frontier": 2000, "live": [2000],
                               "blank": {old: {"n": 9, "last": "2026-01-01"}}},
                              {}, "2026-09-02")
        self.assertNotIn(old, after["blank"], "no longer askable, so no longer remembered")


if __name__ == "__main__":
    unittest.main()
