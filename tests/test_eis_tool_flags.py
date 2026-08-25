#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A flag the parser accepts and the run never sees.

This failed twice in one day: `--policy` was declared on the command, printed in `--help`,
accepted without complaint, and dropped on the way to the function that needed it. The run
came back green having fetched everything, which is exactly what a correctly gated run
looks like from outside if you do not count the rows. Nothing raises, so the only defence
is asserting that what the caller typed arrives where it is used.
"""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eis_tool


class Recorder(object):
    """Stands in for the day runner and remembers how it was called."""

    def __init__(self):
        self.calls = []

    def run(self, date, out, limit=None, keep=None, run_id=None, policy=None, watch=None):
        self.calls.append({"date": date, "out": out, "limit": limit, "policy": policy,
                           "watch": watch})
        return ({"date": date, "complete": True,
                 "coverage": {"delivered": 0, "targets": 0, "gated": 0, "failed": 0},
                 "counts": {"documents": 0}}, {})


class DayPassesWhatItWasGiven(unittest.TestCase):
    def setUp(self):
        import lt_day
        self.recorder = Recorder()
        self.original = lt_day.run
        lt_day.run = self.recorder.run
        self.addCleanup(setattr, lt_day, "run", self.original)

    def call(self, *extra):
        eis_tool.main(["day", "2026-08-20", "--country", "LT", "--out", "work", *extra])
        return self.recorder.calls[-1]

    def test_the_policy_reaches_the_run(self):
        self.assertEqual(self.call("--policy", "rules.json")["policy"], "rules.json")

    def test_no_policy_is_no_policy_not_a_stray_string(self):
        self.assertIsNone(self.call()["policy"])

    def test_the_limit_reaches_the_run(self):
        self.assertEqual(self.call("--limit", "7")["limit"], 7)

    def test_the_watch_list_reaches_the_run_as_bare_ids(self):
        self.assertEqual(self.call("--targets", "EPPS:11, 22")["watch"], ["11", "22"])

    def test_no_watch_list_is_an_empty_one(self):
        self.assertEqual(self.call()["watch"], [])


    def test_the_country_lands_in_the_output_path(self):
        """The destination carries the country for the same reason the source does."""
        self.assertTrue(self.call()["out"].replace("\\", "/").endswith("work/LT"))

    def test_a_run_without_a_country_never_reaches_the_runner(self):
        code = eis_tool.main(["day", "2026-08-20", "--out", "work"])
        self.assertEqual(code, 2)
        self.assertEqual(self.recorder.calls, [])


class AShortDayNamesWhatItLost(unittest.TestCase):
    """"5 of 41" is also what a heavily gated day looks like.

    So a day that came up short cannot report only a number: the count is the same shape as
    a normal day's, and the difference — which procurement did not arrive, and why — is the
    only part anybody can act on.
    """

    def setUp(self):
        import lt_day
        self.original = lt_day.run
        lt_day.run = self.short
        self.addCleanup(setattr, lt_day, "run", self.original)

    @staticmethod
    def short(date, out, limit=None, keep=None, run_id=None, policy=None, watch=None):
        return ({"date": date, "complete": False,
                 "coverage": {"delivered": 5, "targets": 41, "gated": 35, "failed": 1},
                 "counts": {"documents": 30},
                 "lost": [{"pid": "9320336", "kind": None, "watched": True,
                           "reason": "no view served it"}]}, {})

    def test_the_exit_code_says_the_day_is_short(self):
        self.assertEqual(eis_tool.main(["day", "2026-08-20", "--country", "LT"]), 1)

    def test_the_procurement_and_the_reason_are_printed(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            eis_tool.main(["day", "2026-08-20", "--country", "LT"])
        self.assertIn("9320336", err.getvalue())
        self.assertIn("no view served it", err.getvalue())


if __name__ == "__main__":
    unittest.main()
