#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One run is one country, and the failure these tests exist to prevent succeeds quietly.

A run that reads Lithuania and writes under `work/LV/` uploads cleanly, produces a valid
index, and hands a Latvian reader Lithuanian tenders with nothing anywhere saying so. There
is no error to notice. So the rule is structural rather than checked after the fact: the
source and the destination are both derived from one resolved code, and every way of
expressing a mismatch is refused at the point it is expressed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import country


class Resolve(unittest.TestCase):
    def test_the_flag_wins_and_is_normalised(self):
        self.assertEqual(country.resolve("lt"), "LT")
        self.assertEqual(country.resolve(" LV "), "LV")

    def test_the_environment_answers_when_the_flag_does_not(self):
        self.assertEqual(country.resolve(None, {"EIS_COUNTRY": "LT"}), "LT")

    def test_there_is_no_default(self):
        """A default would send the first Lithuanian run at Latvia in silence."""
        with self.assertRaises(country.Mismatch) as raised:
            country.resolve(None, {})
        self.assertIn("no country", str(raised.exception))

    def test_a_code_that_is_not_two_letters_is_refused(self):
        for bad in ("L", "LTU", "l_t", "../LV", "1T"):
            with self.subTest(bad=bad), self.assertRaises(country.Mismatch):
                country.resolve(bad)

    def test_a_country_with_no_source_is_refused_not_guessed(self):
        with self.assertRaises(country.Mismatch) as raised:
            country.resolve("EE")
        self.assertIn("no source", str(raised.exception))


class Destination(unittest.TestCase):
    def test_the_country_folder_is_appended_to_the_runtime_root(self):
        self.assertEqual(country.destination("Shared/02 tender-radar/work", "LT"),
                         "Shared/02 tender-radar/work/LT")

    def test_stray_slashes_do_not_double(self):
        self.assertEqual(country.destination("/base/work/", "LV"), "base/work/LV")

    def test_a_root_that_already_names_a_country_is_refused(self):
        """This is how `work/LV/LV` gets created, and then quietly filled."""
        with self.assertRaises(country.Mismatch) as raised:
            country.destination("project/work/LV", "LV")
        self.assertIn("already ends in a country code", str(raised.exception))

    def test_it_is_refused_even_when_the_two_codes_disagree(self):
        with self.assertRaises(country.Mismatch):
            country.destination("project/work/LV", "LT")

    def test_an_empty_root_is_refused(self):
        with self.assertRaises(country.Mismatch):
            country.destination("", "LV")

    def test_two_countries_cannot_land_in_one_folder(self):
        base = "project/work"
        self.assertNotEqual(country.destination(base, "LV"),
                            country.destination(base, "LT"))


class Source(unittest.TestCase):
    def test_each_country_gets_its_own_reader(self):
        lv_page, lv_fetch = country.source("LV")
        lt_page, lt_fetch = country.source("LT")
        self.assertIsNot(lv_page, lt_page)
        self.assertIsNot(lv_fetch, lt_fetch)

    def test_the_readers_answer_the_same_questions(self):
        """The whole point of the seam: downstream never learns which country it has."""
        for code in sorted(country.SOURCES):
            page, _ = country.source(code)
            with self.subTest(code=code):
                for name in ("parse_notice", "parse_documents", "is_published"):
                    self.assertTrue(callable(getattr(page, name, None)),
                                    "%s.%s is missing" % (code, name))

    def test_an_unknown_country_has_no_source(self):
        with self.assertRaises(country.Mismatch):
            country.source("EE")


class Describe(unittest.TestCase):
    def test_a_report_can_say_which_portal_it_read(self):
        self.assertEqual(country.describe("LT")["portal"], "EPPS")
        self.assertEqual(country.describe("LV")["portal"], "EIS")

    def test_every_country_names_a_timezone(self):
        for code in sorted(country.SOURCES):
            self.assertTrue(country.describe(code)["timezone"])


if __name__ == "__main__":
    unittest.main()
