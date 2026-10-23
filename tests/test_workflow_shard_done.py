#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A shard that fetched is delivered without an artifact, and later draws stand down on `done`.

WHY THIS EXISTS. On 2026-10-23 the self-hosted runners fetched shards for up to two hours and
then failed at "Upload the text": the private repository's artifact quota was hit, and
GitHub counts expired artifacts until it recalculates hours later. The failed step skipped
the delivery, and the chain's next draw — which stood down only when the shard's artifact
existed — fetched the whole shard again, to fail the same way.

WHAT IT GUARDS. On a runner named by SHARD_RUNS_ON the artifact is not uploaded at all (the
day is collected from the delivery). The shard reports `done` once its text reached the
delivery or the artifact, the reusable workflow passes that up, and the chain runs draw 2 and
draw 3 only while no earlier draw said `done`.

Read as text rather than through a YAML parser, like test_workflow_country_gate.py: the
runner image carries no PyYAML.
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(os.path.dirname(HERE), ".github", "workflows")


def text(name):
    return open(os.path.join(WORKFLOWS, name), encoding="utf-8").read()


def step(workflow, step_id):
    """The block of the step whose `id:` is `step_id`, up to the next step."""
    found = re.search(r"\n      - name: [^\n]*\n        id: %s\n(.*?)(?=\n      - name: |\Z)"
                      % re.escape(step_id), workflow, re.S)
    if not found:
        raise AssertionError("no step with id %r in eis-shard.yml" % step_id)
    return found.group(0)


def condition(block):
    found = re.search(r"^\s+if: (.+)$", block, re.M)
    if not found:
        raise AssertionError("step has no if:\n%s" % block)
    return found.group(1)


class SelfHostedShard(unittest.TestCase):
    def setUp(self):
        self.shard = text("eis-shard.yml")

    def test_no_artifact_where_the_shards_run_on_a_named_runner(self):
        self.assertIn("vars.SHARD_RUNS_ON == ''", condition(step(self.shard, "upload")))

    def test_the_delivery_does_not_wait_for_the_artifact(self):
        self.assertNotIn("steps.upload", condition(step(self.shard, "deliver")))

    def test_done_needs_the_text_to_have_reached_a_reader(self):
        when = condition(step(self.shard, "done"))
        self.assertIn("steps.deliver.outcome == 'success'", when)
        self.assertIn("steps.upload.outcome == 'success'", when)
        self.assertIn('echo "done=true" >> "$GITHUB_OUTPUT"', step(self.shard, "done"))

    def test_done_is_passed_up_to_the_caller(self):
        self.assertIn("done: ${{ steps.done.outputs.done }}", self.shard)
        self.assertIn("value: ${{ jobs.shard.outputs.done }}", self.shard)


class Chain(unittest.TestCase):
    def setUp(self):
        self.chain = text("eis-shard-chain.yml")

    def draw(self, n):
        found = re.search(r"\n  draw-%d:\n(.*?)(?=\n  draw-|\Z)" % n, self.chain, re.S)
        self.assertTrue(found, "no draw-%d in eis-shard-chain.yml" % n)
        return found.group(1)

    def test_draw_two_stands_down_after_draw_one_is_done(self):
        self.assertIn("needs.draw-1.outputs.done != 'true'", condition(self.draw(2)))

    def test_draw_three_stands_down_after_either_earlier_draw_is_done(self):
        block = self.draw(3)
        self.assertIn("needs: [draw-1, draw-2]", block)
        when = condition(block)
        self.assertIn("needs.draw-1.outputs.done != 'true'", when)
        self.assertIn("needs.draw-2.outputs.done != 'true'", when)

    def test_every_later_draw_still_runs_after_a_failed_one(self):
        for n in (2, 3):
            self.assertIn("always()", condition(self.draw(n)))


if __name__ == "__main__":
    unittest.main()
