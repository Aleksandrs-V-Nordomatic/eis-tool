#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The shard leaves out what its date already delivered, and only out of a window.

WHY THIS EXISTS. Since 2026-10-23 a run of the same date is dispatched every half hour. Each
asks the register for the whole window again, and without this every run would fetch again
every tender the date already holds and record it `unchanged` over the record that said `new`.

WHAT IT GUARDS. A step reads the date's settled notices before the fetch and may fail without
stopping it; the fetch passes them to `batch.py --skip-notices`; the Graph secret stays in that
step and never reaches the fetch's environment.

Read as text rather than through a YAML parser, like the other workflow tests: the runner
image carries no PyYAML.
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(os.path.dirname(HERE), ".github", "workflows")


def text(name):
    return open(os.path.join(WORKFLOWS, name), encoding="utf-8").read()


def named_step(workflow, name):
    """The block of the step whose name starts with `name`, up to the next step."""
    found = re.search(r"\n      - name: %s[^\n]*\n(.*?)(?=\n      - name: |\Z)" % re.escape(name),
                      workflow, re.S)
    if not found:
        raise AssertionError("no step named %r in eis-shard.yml" % name)
    return found.group(0)


def condition(block):
    found = re.search(r"^\s+if: (.+)$", block, re.M)
    if not found:
        raise AssertionError("step has no if:\n%s" % block)
    return found.group(1)


class KnownNotices(unittest.TestCase):
    def setUp(self):
        self.shard = text("eis-shard.yml")
        self.known = named_step(self.shard, "What this date already holds")
        self.fetch = named_step(self.shard, "Fetch this shard")

    def test_the_step_reads_the_dates_of_the_window(self):
        self.assertIn("python3 known.py", self.known)
        for flag in ('--from "$DATE_FROM"', '--to "$DATE_TO"', "--out known.txt"):
            self.assertIn(flag, self.known)

    def test_it_runs_only_with_a_window_and_a_drive(self):
        when = condition(self.known)
        self.assertIn("steps.go.outputs.go == 'yes'", when)
        self.assertIn("env.HAVE_GRAPH == 'true'", when)
        self.assertIn("inputs.date_from != ''", when)

    def test_failing_to_read_it_never_stops_the_fetch(self):
        self.assertIn("continue-on-error: true", self.known)

    def test_it_comes_before_the_fetch(self):
        self.assertLess(self.shard.index("- name: What this date already holds"),
                        self.shard.index("- name: Fetch this shard"))

    def test_the_fetch_passes_the_list_on(self):
        self.assertIn('SKIP="--skip-notices known.txt"', self.fetch)
        self.assertRegex(self.fetch, r"python3 batch\.py [^\n]*\$SKIP")

    def test_the_graph_secret_stays_out_of_the_fetch(self):
        self.assertNotIn("GRAPH_CLIENT_SECRET", self.fetch)


if __name__ == "__main__":
    unittest.main()
