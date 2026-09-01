#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The walk, without a portal.

What matters here is not the fetching — that is `eis_fetch`, tested elsewhere — but the two
joints where the walk meets the rest of the run: an id found on one runner must be fetched
by that same runner, and a page nobody could reach must not be recorded as an answer.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import batch
import eis_page
import idspace
import idwalk


class TheSliceMatchesTheFetch(unittest.TestCase):
    """A published id becomes a target, and `batch.take_shard` slices targets again.

    If the walk sliced by anything else, a runner would ask about an id whose target then
    belongs to a different runner — asked, found, and never fetched by anybody.
    """

    def test_every_walked_id_would_be_fetched_by_the_runner_that_found_it(self):
        state = idspace.load({"schema": idspace.SCHEMA, "frontier": 180000,
                              "live": [180000], "blank": {}})
        planned = idspace.plan(state, budget=200, width=400)
        for shard in (1, 2, 3, 4):
            mine = idspace.take_shard(planned, shard, 4,
                                      owner=lambda pid: batch.shard_of(eis_page.PAGE % pid, 4))
            targets = [eis_page.PAGE % pid for pid in mine]
            self.assertEqual(batch.take_shard(targets, shard, 4), targets,
                             "shard %d asked about an id it would not fetch" % shard)

    def test_the_four_runners_between_them_ask_the_whole_plan(self):
        state = idspace.load({"schema": idspace.SCHEMA, "frontier": 180000,
                              "live": [180000], "blank": {}})
        planned = idspace.plan(state, budget=200, width=400)
        asked = []
        for shard in (1, 2, 3, 4):
            asked += idspace.take_shard(planned, shard, 4,
                                        owner=lambda pid: batch.shard_of(eis_page.PAGE % pid, 4))
        self.assertEqual(sorted(asked), sorted(planned))


class WhatTheRunWrites(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="idwalk_test_")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "idspace.json")
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump({"schema": idspace.SCHEMA, "frontier": 500, "live": [500], "blank": {}}, fh)

    def run_with(self, answers):
        """Run the walk against a fake portal. `answers` maps id -> (published, why)."""
        calls = []

        def probe(pid, pause):
            calls.append(pid)
            return answers.get(pid, (idwalk.BLANK, None))

        original, idwalk.probe = idwalk.probe, probe
        self.addCleanup(setattr, idwalk, "probe", original)
        out = os.path.join(self.dir, "targets.txt")
        report = os.path.join(self.dir, "idprobe.json")
        idwalk.main(["--state", self.state, "--budget", "4", "--width", "10",
                     "--pause", "0", "--out", out, "--report", report])
        with open(report, encoding="utf-8") as fh:
            written = json.load(fh)
        targets = ""
        if os.path.exists(out):
            with open(out, encoding="utf-8") as fh:
                targets = fh.read()
        return calls, written, targets

    def test_a_published_id_is_handed_to_the_fetch_as_a_url(self):
        calls, report, targets = self.run_with({501: (idwalk.PUBLISHED, None)})
        self.assertIn(eis_page.PAGE % 501, targets)
        self.assertIs(report["probes"]["501"], True)
        self.assertTrue(calls)

    def test_an_unreachable_page_is_no_answer_at_all(self):
        """Recording a refused runner as "not published" would retire a live id for as long
        as the state remembers it."""
        _, report, targets = self.run_with({501: (idwalk.UNREACHABLE, "curl said no")})
        self.assertNotIn("501", report["probes"])
        self.assertEqual(report["unreachable"], 1)
        self.assertNotIn(eis_page.PAGE % 501, targets)

    def test_a_page_with_no_stage_a_guest_may_see_is_an_answer(self):
        """EIS's own fixed refusal is permanent as far as a guest is concerned, so it
        counts as asked — otherwise the rotation returns to it every night forever."""
        _, report, _ = self.run_with({501: (idwalk.BLANK, "no stage a guest may see")})
        self.assertIs(report["probes"]["501"], False)
        self.assertEqual(report["unreachable"], 0)

    def test_the_bot_check_stops_the_night_rather_than_skipping_one_id(self):
        """The same request keeps returning it, so the rest of the budget would be spent
        proving that. What was asked before it still stands."""
        asked = []

        def probe(pid, pause):
            asked.append(pid)
            return (idwalk.CHALLENGE, "slow down") if len(asked) > 1 else (idwalk.PUBLISHED, None)

        original, idwalk.probe = idwalk.probe, probe
        self.addCleanup(setattr, idwalk, "probe", original)
        report = os.path.join(self.dir, "idprobe.json")
        idwalk.main(["--state", self.state, "--budget", "4", "--width", "10",
                     "--pause", "0", "--report", report])
        with open(report, encoding="utf-8") as fh:
            written = json.load(fh)
        self.assertEqual(len(asked), 2, "it stopped at the challenge")
        self.assertEqual(len(written["probes"]), 1, "and kept the answer it already had")
        self.assertTrue(written["stopped"])

    def test_the_targets_file_is_appended_to_never_replaced(self):
        """The workflow writes the caller's own named targets first; the walk adds to them."""
        out = os.path.join(self.dir, "targets.txt")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("https://example.invalid/already-asked-for\n")
        original, idwalk.probe = idwalk.probe, lambda pid, pause: (
            idwalk.PUBLISHED if pid == 501 else idwalk.BLANK, None)
        self.addCleanup(setattr, idwalk, "probe", original)
        idwalk.main(["--state", self.state, "--budget", "3", "--width", "5",
                     "--pause", "0", "--out", out])
        with open(out, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("already-asked-for", body)
        self.assertIn(eis_page.PAGE % 501, body)


class BeforeThereIsAFrontier(unittest.TestCase):

    def test_the_first_night_asks_nothing_and_says_so(self):
        empty = tempfile.mkdtemp(prefix="idwalk_empty_")
        self.addCleanup(__import__("shutil").rmtree, empty, True)
        state = os.path.join(empty, "idspace.json")
        with open(state, "w", encoding="utf-8") as fh:
            json.dump(idspace.empty(), fh)
        calls = []
        original, idwalk.probe = idwalk.probe, lambda pid, pause: (
            calls.append(pid) or (idwalk.BLANK, None))
        self.addCleanup(setattr, idwalk, "probe", original)
        idwalk.main(["--state", state, "--pause", "0"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
