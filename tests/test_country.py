#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One run is one country, and the failure these tests exist to prevent succeeds quietly.

A run that reads one country and writes under another's folder uploads cleanly, produces a
valid index, and hands a reader the wrong country's tenders with nothing anywhere saying so.
There is no error to notice. So the rule is structural rather than checked after the fact: the source
and the destination are both derived from one resolved code, and every way of expressing a
mismatch is refused at the point it is expressed.

THIS REPOSITORY FETCHES ONE COUNTRY, AND THAT MAKES THE CHECK MORE IMPORTANT, NOT LESS.
Every code but `LV` is one this tool has no source for, and each is refused by the same
line. That is the whole guarantee: this tool cannot be pointed at another portal, and it
cannot be pointed at another folder.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import changes
import country

# A code this repository deliberately has no source for — which is every code but `LV`, and
# the shape of what a stale command line or a copied secret names.
ELSEWHERE = "EE"


class Resolve(unittest.TestCase):
    def test_the_flag_wins_and_is_normalised(self):
        self.assertEqual(country.resolve("lv"), "LV")
        self.assertEqual(country.resolve(" LV "), "LV")

    def test_the_environment_answers_when_the_flag_does_not(self):
        self.assertEqual(country.resolve(None, {"EIS_COUNTRY": "LV"}), "LV")

    def test_there_is_no_default_even_with_one_country(self):
        """A tool that assumes its own country cannot say when it was pointed elsewhere."""
        with self.assertRaises(country.Mismatch) as raised:
            country.resolve(None, {})
        self.assertIn("no country", str(raised.exception))

    def test_a_code_that_is_not_two_letters_is_refused(self):
        for bad in ("L", "LVA", "l_v", "../EE", "1V"):
            with self.subTest(bad=bad), self.assertRaises(country.Mismatch):
                country.resolve(bad)

    def test_a_country_with_no_source_is_refused_not_guessed(self):
        for code in (ELSEWHERE, "EE"):
            with self.subTest(code=code), self.assertRaises(country.Mismatch) as raised:
                country.resolve(code)
            self.assertIn("no source", str(raised.exception))

    def test_the_refusal_names_what_this_repository_does_have(self):
        # So a reader who ran the wrong tool learns which one they wanted, in one line.
        with self.assertRaises(country.Mismatch) as raised:
            country.resolve(ELSEWHERE)
        self.assertIn("LV", str(raised.exception))


class Destination(unittest.TestCase):
    def test_the_country_folder_is_appended_to_the_runtime_root(self):
        self.assertEqual(country.destination("Shared/project/work", "LV"),
                         "Shared/project/work/LV")

    def test_stray_slashes_do_not_double(self):
        self.assertEqual(country.destination("/base/work/", "LV"), "base/work/LV")

    def test_a_root_that_already_names_a_country_is_refused(self):
        """This is how `work/LV/LV` gets created, and then quietly filled."""
        with self.assertRaises(country.Mismatch) as raised:
            country.destination("project/work/LV", "LV")
        self.assertIn("already ends in a country code", str(raised.exception))

    def test_a_root_naming_the_country_this_tool_is_not_is_refused_too(self):
        # The likeliest misconfiguration of all: GRAPH_DEST_ROOT copied across from
        # somewhere else, still ending in that country's folder.
        with self.assertRaises(country.Mismatch):
            country.destination("project/work/%s" % ELSEWHERE, "LV")

    def test_an_empty_root_is_refused(self):
        with self.assertRaises(country.Mismatch):
            country.destination("", "LV")

    def test_the_folder_is_this_countrys_and_no_other(self):
        self.assertTrue(country.destination("project/work", "LV").endswith("/LV"))


class Source(unittest.TestCase):
    def test_the_reader_answers_the_questions_downstream_asks(self):
        """The whole point of the seam: downstream never learns which country it has."""
        for code in sorted(country.SOURCES):
            page, fetch = country.source(code)
            with self.subTest(code=code):
                for name in ("parse_notice", "parse_documents", "is_published"):
                    self.assertTrue(callable(getattr(page, name, None)),
                                    "%s.%s is missing" % (code, name))
                self.assertIsNotNone(fetch)

    def test_a_country_this_repository_does_not_fetch_has_no_source(self):
        for code in (ELSEWHERE, "EE"):
            with self.subTest(code=code), self.assertRaises(country.Mismatch):
                country.source(code)


class Describe(unittest.TestCase):
    def test_a_report_can_say_which_portal_it_read(self):
        self.assertEqual(country.describe("LV")["portal"], "EIS")

    def test_every_country_names_a_timezone(self):
        for code in sorted(country.SOURCES):
            self.assertTrue(country.describe(code)["timezone"])


class TheParserFollowsTheCountry(unittest.TestCase):
    """A page reader improving is not a buyer amending, and the two must not be confused.

    The version stamped in a fingerprint decides that. Stamped with another country's digest,
    an `eis_page` improvement changes facts across the whole corpus in one day and every one
    of them reaches a card as an amendment.
    """

    def test_this_country_names_its_own_page_reader(self):
        self.assertEqual(country.parser_files("LV"), ("eis_page.py",))

    def test_a_country_this_tool_does_not_fetch_names_no_reader_at_all(self):
        # Rather than falling back to some default reader, which is how one country's digest
        # would come to be stamped on another's fingerprint.
        with self.assertRaises(country.Mismatch):
            country.parser_files(ELSEWHERE)

    def test_the_default_is_this_repositorys_reader(self):
        # Every caller written before the country flag existed meant "this tool's parser",
        # and in a single-country repository that is the only thing it can mean.
        self.assertEqual(changes.parser_version(),
                         changes.parser_version(files=country.parser_files("LV")))


if __name__ == "__main__":
    unittest.main()
