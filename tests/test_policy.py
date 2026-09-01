#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The recall gate, which is the same rule in every country.

It used to live in `batch.py` and be tested there, which made it look like a property of
this portal. It is not: the terms come from the caller's environment and CPV is European.
These tests moved with the code, so the gate and the proof of it stay together.

The fixture is deliberately unrelated to anything anyone hunts for — the repository must
disclose the SHAPE of the filter and never its subject.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import policy


class DownloadFilter(unittest.TestCase):
    """The one filter allowed before a document exists.

    The policy under test is a FIXTURE, not a deployment's. What is being proved is the
    mechanism — which way each signal fails, and which rule wins when two disagree — and
    that is independent of anybody's terms. Committing real terms here would publish the
    policy that `EIS_POLICY` exists to keep out of the repository.
    """

    FIXTURE = json.dumps({
        # One opaque token and one inflected root, because roots are matched as substrings.
        "recall_title_terms": ["alfa", "sarkan"],
        "hard_exclude_prefixes": ["99999"],
        "hard_exclude_title_terms": ["omega"],
    })

    def setUp(self):
        self.policy = policy.load_policy(self.FIXTURE)
        self.assertIsNotNone(self.policy, "the fixture policy must load")

    def test_a_code_alone_never_vetoes_when_the_title_is_missing(self):
        # Missing title is missing evidence, whatever the codes happen to say.
        self.assertFalse(policy.outside_scope(
            {"cpv": [{"code": "33140000-3"}, {"code": "33141000-0"}]}, self.policy))

    def test_a_notice_with_no_code_at_all_is_never_dropped(self):
        # Silence is not a classification.
        self.assertFalse(policy.outside_scope({}, self.policy))
        self.assertFalse(policy.outside_scope({"cpv": []}, self.policy))

    def test_a_matching_title_is_kept(self):
        self.assertFalse(policy.outside_scope({"title": "Alfa piegāde"}, self.policy))

    def test_a_root_matches_an_inflected_form(self):
        # The whole reason roots are substrings: the language inflects the ending.
        for title in ("Sarkanā korpusa remonts", "Sarkanu detaļu piegāde"):
            self.assertFalse(policy.outside_scope({"title": title}, self.policy), title)

    def test_a_nonmatching_title_is_not_downloaded(self):
        self.assertTrue(policy.outside_scope({
            "title": "Kaut kas pavisam cits",
            "cpv": [{"code": "45000000-7"}],
        }, self.policy))

    def test_an_excluded_title_term_beats_a_matching_root(self):
        # Exclusion wins even when a recall root is present in the same title.
        self.assertTrue(policy.outside_scope(
            {"title": "Alfa un omega", "cpv": [{"code": "71320000-7"}]}, self.policy))

    def test_an_excluded_title_term_works_without_any_code(self):
        self.assertTrue(policy.outside_scope({"title": "Omega izbūve"}, self.policy))

    def test_an_all_excluded_code_set_is_dropped(self):
        self.assertTrue(policy.outside_scope({"cpv": [{"code": "99999000-1"}]}, self.policy))

    def test_one_excluded_code_among_others_is_not_enough(self):
        # `all`, not `any`: a notice carrying an excluded code beside an unrelated one is
        # not settled by the excluded one, and a missing title still fails open.
        self.assertFalse(policy.outside_scope(
            {"cpv": [{"code": "99999000-1"}, {"code": "45000000-7"}]}, self.policy))

    def test_no_policy_means_fetch_everything(self):
        self.assertFalse(policy.outside_scope({"cpv": [{"code": "33140000-3"}]}, None))
        self.assertFalse(policy.outside_scope({"title": "Omega"}, None))


class PolicySource(unittest.TestCase):
    """Where the policy comes from, and every way of not having one fails open."""

    def tearDown(self):
        os.environ.pop(policy.POLICY_ENV, None)

    def test_the_environment_supplies_it(self):
        os.environ[policy.POLICY_ENV] = json.dumps({"recall_title_terms": ["alfa"]})
        self.assertTrue(policy.outside_scope({"title": "beta"}, policy.load_policy()))
        self.assertFalse(policy.outside_scope({"title": "alfa"}, policy.load_policy()))

    def test_a_path_is_accepted_too(self):
        directory = tempfile.mkdtemp(prefix="eis_pol_")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "policy.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"recall_title_terms": ["alfa"]}))
        self.assertIsNotNone(policy.load_policy(path))

    def test_nothing_configured_means_no_filter(self):
        self.assertIsNone(policy.load_policy())
        self.assertIsNone(policy.load_policy(""))

    def test_a_broken_policy_fails_open_rather_than_dropping_everything(self):
        # The failure that must never happen quietly: a policy that will not parse must not
        # be read as "nothing matches".
        self.assertIsNone(policy.load_policy("{not json"))
        self.assertIsNone(policy.load_policy("/no/such/file.json"))
        self.assertIsNone(policy.load_policy(json.dumps({"recall_title_terms": []})))


class OverriddenCodes(unittest.TestCase):
    """A code that survives its own excluded division.

    The exclusions ask what the buyer classified a purchase as, and 62% of live procurements
    carry one code only — so a purchase filed under a service division has its whole code set
    inside an excluded one and is dropped before a byte moves, however exactly its title says
    what it is. Whether such a tender is wanted is a later question; the gate's job is only to
    stop deciding it in advance.

    The fixtures are invented, like every other fixture here. What any deployment actually
    looks for arrives in a secret and is named nowhere in this repository.
    """

    def policy(self, **extra):
        return policy.load_policy(json.dumps(dict(
            {"recall_title_terms": ["alfa", "beta"],
             "hard_exclude_prefixes": ["72", "79"]}, **extra)))

    def notice(self, title, *codes):
        return {"title": title, "cpv": list(codes)}

    def test_without_an_override_an_excluded_division_drops_it(self):
        self.assertTrue(policy.outside_scope(
            self.notice("Alfa priežiūros paslaugos", "72250000"), self.policy()))

    def test_an_override_keeps_it_despite_the_division(self):
        self.assertFalse(policy.outside_scope(
            self.notice("Alfa priežiūros paslaugos", "72250000"),
            self.policy(override_prefixes=["72250"])))

    def test_one_overridden_code_is_enough_among_several(self):
        self.assertFalse(policy.outside_scope(
            self.notice("Beta stebėjimas", "79711000", "79999000"),
            self.policy(override_prefixes=["79711"])))

    def test_an_override_does_not_rescue_an_excluded_title(self):
        """The title veto is the buyer's own words and outranks a code."""
        self.assertTrue(policy.outside_scope(
            self.notice("omega alfa", "72250000"),
            self.policy(override_prefixes=["72250"],
                        hard_exclude_title_terms=["omega"])))

    def test_an_override_does_not_rescue_a_title_with_nothing_we_do_in_it(self):
        """It undoes the code veto, not the recall test — otherwise every overridden
        division would arrive whole."""
        self.assertTrue(policy.outside_scope(
            self.notice("gamma prekės", "72250000"),
            self.policy(override_prefixes=["72250"])))

    def test_a_policy_written_before_overrides_existed_still_loads(self):
        rules = self.policy()
        # The tuple grows as optional fields are added; what this protects is that a policy
        # written before any of them loads, and that every one it omits defaults to empty.
        self.assertEqual(len(rules), 5)
        self.assertEqual(rules[3], ())      # override_prefixes
        self.assertEqual(rules[4], ())      # recall_cpv_prefixes


if __name__ == "__main__":
    unittest.main()
