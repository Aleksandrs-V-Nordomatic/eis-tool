#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A code can recall, and the day of 24 August 2026 is why.

Recall was title-only. A code could exclude a notice, or rescue one from an exclusion, but it
could never bring anything in — so the gate's whole sensitivity rested on a buyer choosing
words we had guessed in advance. That works after a title list has been tuned against a live
register for weeks, which Latvia's has been, and fails on a country whose roots were written
in one sitting.

Two real Lithuanian procurements measured that failure on one day. `Stebejimo sistema` — three
words, no `vaizdo` — carries CPV 32323500, which is literally "video-surveillance system". And
`LoRaWAN technologijos diegimas bevielei perdavimo sistemos objektu parametru kontrolei`
carries 32440000, telemetry. Both are our line, both were dropped before a byte moved, and no
plausible title list would have caught either.

The property that matters here is not that the new clause recalls, but that it can only widen:
every exclusion still returns before it is reached.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import policy as gate


def policy(**kw):
    base = {"recall_title_terms": ["valdymo sistem", "automatik"]}
    base.update(kw)
    return gate.load_policy(__import__("json").dumps(base))


def notice(title, *codes):
    return {"title": title, "cpv": list(codes)}


class ACodeCanRecall(unittest.TestCase):

    def test_a_short_title_with_an_exact_code_is_no_longer_invisible(self):
        """`Stebejimo sistema` under 32323500 — the measured miss."""
        rules = policy(recall_cpv_prefixes=["32323"])
        self.assertFalse(gate.outside_scope(notice("Stebejimo sistema", "32323500"), rules))

    def test_telemetry_the_same_way(self):
        rules = policy(recall_cpv_prefixes=["32440"])
        self.assertFalse(gate.outside_scope(
            notice("LoRaWAN technologijos diegimas bevielei perdavimo sistemos objektu "
                   "parametru kontrolei", "32440000"), rules))

    def test_without_the_code_list_that_notice_is_still_dropped(self):
        """The old behaviour, unchanged — which is what makes this safe for Latvia."""
        self.assertTrue(gate.outside_scope(notice("Stebejimo sistema", "32323500"), policy()))

    def test_a_matching_title_still_wins_on_its_own(self):
        rules = policy(recall_cpv_prefixes=["32323"])
        self.assertFalse(gate.outside_scope(
            notice("Pastato valdymo sistemos irengimas", "45331000"), rules))

    def test_a_notice_that_matches_neither_is_still_dropped(self):
        rules = policy(recall_cpv_prefixes=["32323"])
        self.assertTrue(gate.outside_scope(notice("Sviezios darzoves", "15331000"), rules))


class RecallNeverBeatsAnExclusion(unittest.TestCase):
    """The clause widens the gate. It must not be able to reopen a door the policy shut."""

    def test_an_excluded_title_term_still_wins(self):
        rules = policy(recall_cpv_prefixes=["32323"],
                       hard_exclude_title_terms=["maisto produkt"])
        self.assertTrue(gate.outside_scope(
            notice("Maisto produktu stebejimo sistema", "32323500"), rules))

    def test_an_all_excluded_code_set_still_wins(self):
        rules = policy(recall_cpv_prefixes=["33"], hard_exclude_prefixes=["33"])
        self.assertTrue(gate.outside_scope(notice("Tonometras", "33100000"), rules))


class TheShapeStaysBackwardCompatible(unittest.TestCase):

    def test_a_policy_without_the_field_parses_and_recalls_nothing_extra(self):
        rules = policy()
        self.assertEqual(len(rules), 5)
        self.assertEqual(rules[4], ())

    def test_a_three_field_policy_tuple_is_still_accepted(self):
        """`outside_scope` reads the tail defensively, as it already did for overrides."""
        legacy = (("valdymo sistem",), (), ())
        self.assertFalse(gate.outside_scope(notice("Valdymo sistemos darbai", "45331000"),
                                             legacy))
        self.assertTrue(gate.outside_scope(notice("Sviezios darzoves", "15331000"), legacy))

    def test_every_committed_example_still_parses(self):
        """Whichever example policy this repository ships, the gate must still load it.

        Found rather than named: each country tool carries its own illustration, and a test
        that hard-codes one file name passes in the repository it was written in and fails
        in the fork the moment the split happens.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        examples = sorted(f for f in os.listdir(root)
                          if f.endswith("policy.example.json"))
        self.assertTrue(examples, "this repository ships no example recall policy")
        for name in examples:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                rules = gate.load_policy(fh.read())
            self.assertIsNotNone(rules, name)
            self.assertEqual(len(rules), 5, name)


if __name__ == "__main__":
    unittest.main()
