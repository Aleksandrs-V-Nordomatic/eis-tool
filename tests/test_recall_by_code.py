#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A code can recall, and a title alone is why it has to be able to.

Recall used to be title-only. A code could exclude a notice, or rescue one from an exclusion,
but it could never bring anything in — so the gate's whole sensitivity rested on a buyer
choosing words somebody had guessed in advance. That holds up after a title list has been
tuned against a live register for months, and fails on a register whose roots were written in
one sitting.

The failure is structural and needs no example to describe: a buyer writes a short, vague
title and classifies the purchase exactly. The title list misses it, the code says precisely
what the purchase is, and the gate has no way to hear the code. `recall_cpv_prefixes` gives
it one.

The property that matters here is not that the new clause recalls, but that it can only
widen: every exclusion still returns before it is reached.

THE FIXTURES BELOW ARE INVENTED, AND THAT IS A RULE RATHER THAN AN ACCIDENT. Nothing in this
repository names what any deployment actually looks for. The terms arrive in a secret, the
committed example policy is about something nobody hunts, and a test that reached for a real
notice to make its point would undo both in a file nobody thinks of as disclosure.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import policy as gate

# An invented profile and an invented division to exclude. What the tests prove is the shape
# of the rule, and the shape needs no real subject.
TERMS = ["alfa sistem", "beta"]
IN_CODE = "32323500"        # a code the invented profile recalls on
IN_PREFIX = "32323"
OUT_CODE = "15331000"       # a code inside the invented excluded division


def policy(**kw):
    base = {"recall_title_terms": list(TERMS)}
    base.update(kw)
    return gate.load_policy(__import__("json").dumps(base))


def notice(title, *codes):
    return {"title": title, "cpv": list(codes)}


class ACodeCanRecall(unittest.TestCase):

    def test_a_short_title_with_an_exact_code_is_no_longer_invisible(self):
        """The whole point: the title says nothing and the code says exactly what it is."""
        rules = policy(recall_cpv_prefixes=[IN_PREFIX])
        self.assertFalse(gate.outside_scope(notice("Gamma", IN_CODE), rules))

    def test_a_long_but_unhelpful_title_works_the_same_way(self):
        rules = policy(recall_cpv_prefixes=[IN_PREFIX])
        self.assertFalse(gate.outside_scope(
            notice("Delta diegimas objektu kontrolei", IN_CODE), rules))

    def test_without_the_code_list_that_notice_is_still_dropped(self):
        """The old behaviour, unchanged — which is what makes the clause safe to add."""
        self.assertTrue(gate.outside_scope(notice("Gamma", IN_CODE), policy()))

    def test_a_matching_title_still_wins_on_its_own(self):
        rules = policy(recall_cpv_prefixes=[IN_PREFIX])
        self.assertFalse(gate.outside_scope(
            notice("Alfa sistemos irengimas", "45331000"), rules))

    def test_a_notice_that_matches_neither_is_still_dropped(self):
        rules = policy(recall_cpv_prefixes=[IN_PREFIX])
        self.assertTrue(gate.outside_scope(notice("Omega", OUT_CODE), rules))


class RecallNeverBeatsAnExclusion(unittest.TestCase):
    """The clause widens the gate. It must not be able to reopen a door the policy shut."""

    def test_an_excluded_title_term_still_wins(self):
        rules = policy(recall_cpv_prefixes=[IN_PREFIX],
                       hard_exclude_title_terms=["omega"])
        self.assertTrue(gate.outside_scope(notice("Omega Gamma", IN_CODE), rules))

    def test_an_all_excluded_code_set_still_wins(self):
        rules = policy(recall_cpv_prefixes=["15"], hard_exclude_prefixes=["15"])
        self.assertTrue(gate.outside_scope(notice("Omega", OUT_CODE), rules))


class TheShapeStaysBackwardCompatible(unittest.TestCase):

    def test_a_policy_without_the_field_parses_and_recalls_nothing_extra(self):
        rules = policy()
        # The tuple grows as optional fields are added; what this protects is that a policy
        # written before any of them loads, and that every one it omits defaults to empty.
        self.assertEqual(len(rules), 5)
        self.assertEqual(rules[3], ())      # override_prefixes
        self.assertEqual(rules[4], ())      # recall_cpv_prefixes

    def test_a_three_field_policy_tuple_is_still_accepted(self):
        """`outside_scope` reads the tail defensively, as it already did for overrides."""
        legacy = (("alfa sistem",), (), ())
        self.assertFalse(gate.outside_scope(notice("Alfa sistemos darbai", "45331000"),
                                            legacy))
        self.assertTrue(gate.outside_scope(notice("Omega", OUT_CODE), legacy))

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
