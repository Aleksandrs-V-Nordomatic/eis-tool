#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The day's list is written on the runner the shards ran on.

WHY THIS EXISTS. Since 2026-10-22 the shards run on self-hosted runners on the owner's laptop
(SHARD_RUNS_ON), and since 2026-10-23 a run is dispatched every half hour. The repository is
private, so a `collect` job left on ubuntu-latest spent Actions minutes forty-eight times a day
for a few seconds of Python. It runs where the shards run, in bash like them.

Read as text rather than through a YAML parser: the runner image carries no PyYAML.
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(os.path.dirname(HERE), ".github", "workflows")


def text(name):
    return open(os.path.join(WORKFLOWS, name), encoding="utf-8").read()


def job(workflow, name):
    found = re.search(r"\n  %s:\n(.*?)(?=\n  [a-z][\w-]*:\n|\Z)" % re.escape(name), workflow, re.S)
    if not found:
        raise AssertionError("no job %r" % name)
    return found.group(1)


class Collect(unittest.TestCase):
    def setUp(self):
        self.collect = job(text("eis-batch.yml"), "collect")

    def test_it_runs_on_the_runner_the_shards_run_on(self):
        shard = text("eis-shard.yml")
        expression = re.search(r"runs-on: (\$\{\{ fromJSON\(vars\.SHARD_RUNS_ON[^\n]*)", shard).group(1)
        self.assertIn("runs-on: " + expression, self.collect)

    def test_it_no_longer_names_a_hosted_runner(self):
        self.assertNotIn("runs-on: ubuntu-latest", self.collect)

    def test_its_steps_are_bash_on_either_kind_of_runner(self):
        self.assertRegex(self.collect, r"defaults:\n\s+run:\n\s+shell: bash")

    def test_it_still_runs_after_a_failed_shard(self):
        self.assertIn("if: always()", self.collect)


if __name__ == "__main__":
    unittest.main()
