#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Two answers that are not the same, and used to be filed as one.

Discovery walks the register, then asks each notice for its EIS link. A notice that answers
"no link" is a procurement conducted somewhere else — a fact, and discovery skips it by
design. A notice we never reached answers nothing, and used to be recorded identically.

That gap sat exactly where the coverage proof stops: coverage is proven against the
register's own x-total-count BEFORE resolution, so a connection reset during resolution
shrank the day and every completeness check downstream still agreed the day was whole. It
is the quiet version of the failure that took a shard out loudly, and the worse one.

These tests hold the line: a window that could not be fully asked does not ship, and one
that was fully asked ships with the notices that genuinely have no link left in place.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eis_tool
import harvest
import net


def notice(uuid, kind="competition"):
    return {"identifier": uuid, "externalId": uuid, "type": kind, "name": "t",
            "organizationName": "b", "publicationDate": "2026-08-31T09:00:00"}


class Resolution(unittest.TestCase):

    def setUp(self):
        self.addCleanup(setattr, harvest, "fetch_window", harvest.fetch_window)
        self.addCleanup(setattr, eis_tool, "resolve", eis_tool.resolve)
        harvest.fetch_window = lambda a, b: (
            [notice("u1"), notice("u2"), notice("u3")],
            {"expected": 3, "unique": 3, "pages": 1, "drift": 0, "proven": True})

    def answers(self, table):
        """`table` maps uuid -> a URL, None, or an exception to raise. Records each ask."""
        self.asked = []

        def fake(uuid, timeout=45, strict=False):
            self.asked.append(uuid)
            answer = table[uuid].pop(0) if isinstance(table[uuid], list) else table[uuid]
            if isinstance(answer, BaseException):
                raise answer
            return answer
        eis_tool.resolve = fake

    def test_one_unreachable_notice_is_carried_rather_than_dropped_or_fatal(self):
        # The register is plainly up — two notices answered. So this is about u2 and not
        # about the run, and refusing the whole window would cost the day to save a notice.
        # It ships, marked, and the fetch stage asks again.
        self.answers({"u1": "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/1",
                      "u2": net.Unreachable("reset"),
                      "u3": "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/3"})
        found = eis_tool.discover(days=1)
        self.assertEqual(found["resolution"]["unreachable"], 1)
        self.assertFalse(found["resolution"]["proven"])
        stranded = [n for n in found["notices"] if n.get("unreachable")]
        self.assertEqual([n["uuid"] for n in stranded], ["u2"])
        # And it is NOT filed as a procurement conducted elsewhere, which is the confusion
        # the whole change exists to stop.
        self.assertEqual(found["resolution"]["unlinked"], 0)

    def test_a_register_that_answers_nothing_stands_the_runner_down(self):
        # Nothing resolved at all: this is not a fact about any notice, it is this runner
        # not reaching the register. A window of nothing but gaps is not a window.
        self.answers({"u%d" % n: net.Unreachable("reset") for n in (1, 2, 3)})
        with self.assertRaises(RuntimeError) as caught:
            eis_tool.discover(days=1)
        self.assertIn("not reaching the register", str(caught.exception))

    def test_an_unreachable_notice_is_asked_once_more_before_the_window_is_refused(self):
        # The register recovers on the scale of seconds, and one more pass costs nothing on
        # the ordinary day where the list is empty.
        self.answers({"u1": "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/1",
                      "u2": [net.Unreachable("reset"),
                             "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/2"],
                      "u3": "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/3"})
        found = eis_tool.discover(days=1)
        self.assertEqual(self.asked.count("u2"), 2)
        self.assertEqual(found["resolution"]["linked"], 3)
        self.assertEqual(found["resolution"]["retried"], 1)

    def test_a_procurement_conducted_elsewhere_is_still_not_an_error(self):
        # `None` from a page we did read: the notice names no EIS procurement, which is a
        # fact about the purchase. It ships, counted, exactly as it always did.
        self.answers({"u1": "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/1",
                      "u2": None,
                      "u3": "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/3"})
        found = eis_tool.discover(days=1)
        self.assertEqual(len(found["notices"]), 3)
        self.assertEqual(found["resolution"], {"biddable": 3, "linked": 2, "unlinked": 1,
                                               "unreachable": 0, "retried": 0, "proven": True})

    def test_discovery_asks_strictly_so_the_two_answers_stay_apart(self):
        asked_strict = []

        def fake(uuid, timeout=45, strict=False):
            asked_strict.append(strict)
            return None
        eis_tool.resolve = fake
        eis_tool.discover(days=1)
        self.assertEqual(asked_strict, [True, True, True])


class Doors(unittest.TestCase):
    """The gate has to ask about the door the run opens first, not the one it opens last."""

    def setUp(self):
        self.addCleanup(setattr, eis_tool, "_reach", eis_tool._reach)

    def test_a_refused_register_stands_the_runner_down_even_when_eis_answers(self):
        # This is what happened: the probe asked EIS, EIS said yes and was telling the
        # truth, and the run's first request went to infob.iub.gov.lv, which said no.
        eis_tool._reach = lambda url, timeout=40: (
            ("iub.gov.lv" not in url), "answered 200" if "iub.gov.lv" not in url
            else "no TCP connection — this address is refused")
        reachable, detail = eis_tool.probe()
        self.assertFalse(reachable)
        self.assertIn("register search", detail)

    def test_a_runner_that_can_reach_everything_goes(self):
        eis_tool._reach = lambda url, timeout=40: (True, "answered 200")
        reachable, detail = eis_tool.probe()
        self.assertTrue(reachable)
        for name, _ in eis_tool.DOORS:
            self.assertIn(name, detail)

    def test_the_gate_asks_about_every_host_the_run_uses(self):
        # A door added to the pipeline and not to this list is a gate that passes a runner
        # which then cannot make the request. Kept as an explicit inventory.
        self.assertEqual([name for name, _ in eis_tool.DOORS],
                         ["register search", "register notices", "EIS"])
        hosts = [url for _, url in eis_tool.DOORS]
        self.assertTrue(any(harvest.API in h for h in hosts))
        self.assertTrue(any("eformsb.pvs.iub.gov.lv" in h for h in hosts))


if __name__ == "__main__":
    unittest.main()
