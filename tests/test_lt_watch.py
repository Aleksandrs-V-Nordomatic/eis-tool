#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The watch list: the half of a night that is not about new procurements.

A radar has two jobs and only one of them is discovery. The other is answering, every
night, whether anything moved on the procurements somebody is still deciding about — and
those are exactly the ones a recall gate must not touch, because the gate decides what is
worth fetching for the FIRST time and these already carry a card a person made.

The failure this file exists to prevent is silent in the worst way: a watched procurement
dropped by the gate, or lost to a `--limit` meant for a trial, leaves the card sitting on
the board looking maintained while nothing re-reads it.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eis_tool
import lt_day


def notice(pid, title="Pastato valdymo sistema", cpv="45331000"):
    return {"pid": pid, "title": title, "buyer": "Vilniaus miestas",
            "deadline": "2026-09-15", "cpv_main": cpv, "cpv": [cpv],
            "published": "2026-08-20", "link": "https://viesiejipirkimai.lt/%s" % pid}


def fetched(pid, kind, **_):
    return {"pid": pid, "kind": kind, "home": "tenders/%s" % pid,
            "documents": 1, "catalogued": 1, "uncatalogued": 0, "bytes": 10,
            "index": {"documents": [{"amendment": "3"}]},
            "state": {"schema": "state/1", "pid": pid, "facts": {}, "records": {},
                      "documents": {}, "unreadable": {}},
            "title": "Pastato valdymo sistema", "buyer": "Vilniaus miestas",
            "deadline": "2026-09-15", "cpv_main": "45331000"}


class Stubs(object):
    """The portal, replaced by a dict. Records what was actually fetched."""

    def __init__(self, window, served, gate_drops=()):
        self.window = window
        self.served = served                 # pid -> the kinds whose view answers
        self.gate_drops = set(gate_drops)
        self.fetched = []

    def install(self, case):
        case.addCleanup(setattr, lt_day, "lt_targets", lt_day.lt_targets)
        case.addCleanup(setattr, lt_day, "lt_page", lt_day.lt_page)
        case.addCleanup(setattr, lt_day, "lt_fetch", lt_day.lt_fetch)
        case.addCleanup(setattr, lt_day, "batch_mod", lt_day.batch_mod)

        outer = self

        class Targets(object):
            @staticmethod
            def day(a, b):
                return [dict(t) for t in outer.window]

        class Page(object):
            @staticmethod
            def notice_only(pid, kind="tender"):
                if kind in outer.served.get(str(pid), ()):
                    return notice(str(pid))
                return None

        class Fetch(object):
            @staticmethod
            def fetch(pid, out_root, kind, notice=None):
                outer.fetched.append(str(pid))
                return fetched(str(pid), kind)

        class Batch(object):
            @staticmethod
            def load_policy(source):
                return ("valdym",), (), (), ()

            @staticmethod
            def outside_scope(n, rules):
                return str(n["pid"]) in outer.gate_drops

            @staticmethod
            def cpv_codes(n):
                return [n.get("cpv_main")]

        lt_day.lt_targets = Targets
        lt_day.lt_page = Page
        lt_day.lt_fetch = Fetch
        lt_day.batch_mod = Batch


class TheWatchListRidesWithTheWindow(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "LT")

    def go(self, stubs, watch=None, limit=None):
        stubs.install(self)
        return lt_day.run("2026-08-20", self.out, limit=limit, policy="{}", watch=watch)

    def test_a_watched_procurement_the_gate_would_drop_is_fetched_anyway(self):
        stubs = Stubs(window=[], served={"999": ("tender",)}, gate_drops={"999"})
        day, changes = self.go(stubs, watch=["999"])

        self.assertEqual(stubs.fetched, ["999"])
        self.assertEqual(changes["counts"]["gated"], 0)
        self.assertTrue(changes["tenders"][0]["watched"])

    def test_a_window_procurement_the_gate_drops_is_still_dropped(self):
        stubs = Stubs(window=[{"pid": "111", "kind": "tender", "title": "t",
                               "buyer": "b", "published": "2026-08-20"}],
                      served={"111": ("tender",)}, gate_drops={"111"})
        day, changes = self.go(stubs)

        self.assertEqual(stubs.fetched, [])
        self.assertEqual(changes["counts"]["gated"], 1)

    def test_a_watched_procurement_already_in_the_window_is_fetched_once(self):
        stubs = Stubs(window=[{"pid": "111", "kind": "tender", "title": "t",
                               "buyer": "b", "published": "2026-08-20"}],
                      served={"111": ("tender",)})
        self.go(stubs, watch=["111"])

        self.assertEqual(stubs.fetched, ["111"])

    def test_a_trial_limit_never_truncates_the_watch_list(self):
        window = [{"pid": str(i), "kind": "tender", "title": "t", "buyer": "b",
                   "published": "2026-08-20"} for i in range(10)]
        served = {str(i): ("tender",) for i in range(10)}
        served["999"] = ("tender",)
        stubs = Stubs(window=window, served=served)
        self.go(stubs, watch=["999"], limit=2)

        # Two of the day, and the watched one whatever the limit said.
        self.assertEqual(len(stubs.fetched), 3)
        self.assertIn("999", stubs.fetched)

    def test_a_watched_card_no_view_serves_is_named_not_silently_dropped(self):
        stubs = Stubs(window=[], served={})
        day, changes = self.go(stubs, watch=["404"])

        self.assertEqual(stubs.fetched, [])
        lost = day["lost"]
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["pid"], "404")
        self.assertTrue(lost[0]["watched"])
        self.assertFalse(day["complete"])

    def test_the_kind_is_asked_of_the_portal_not_of_the_card(self):
        # A consultation, whose id looks exactly like a competition's.
        stubs = Stubs(window=[], served={"777": ("consultation",)})
        day, changes = self.go(stubs, watch=["777"])

        self.assertEqual(changes["tenders"][0]["kind"], "consultation")

    def test_the_day_counts_the_watched_separately(self):
        window = [{"pid": "111", "kind": "tender", "title": "t", "buyer": "b",
                   "published": "2026-08-20"}]
        stubs = Stubs(window=window, served={"111": ("tender",), "999": ("tender",)})
        day, changes = self.go(stubs, watch=["999"])

        self.assertEqual(day["counts"]["watched"], 1)
        self.assertEqual(day["counts"]["tenders"], 2)


class TheListItselfIsReadForgivingly(unittest.TestCase):
    """A board spells a key `EPPS:<id>`; a workflow writes a textarea to a file; a person
    types two ids and a comma. All three are the same list."""

    def test_a_board_key_is_reduced_to_the_id_the_portal_answers_to(self):
        self.assertEqual(eis_tool.read_targets("EPPS:9320336"), ["9320336"])

    def test_commas_newlines_and_spaces_all_separate(self):
        self.assertEqual(eis_tool.read_targets("1, 2\n3  4"), ["1", "2", "3", "4"])

    def test_the_same_procurement_named_twice_is_fetched_once(self):
        self.assertEqual(eis_tool.read_targets("EPPS:5\n5\n 5 "), ["5"])

    def test_an_empty_list_is_empty_and_not_a_list_holding_nothing(self):
        self.assertEqual(eis_tool.read_targets(""), [])
        self.assertEqual(eis_tool.read_targets(None), [])
        self.assertEqual(eis_tool.read_targets("\n\n  \n"), [])

    def test_a_file_of_them_reads_the_same_as_the_ids_themselves(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as fh:
            fh.write("EPPS:11\nEPPS:22\n")
            path = fh.name
        try:
            self.assertEqual(eis_tool.read_targets(path), ["11", "22"])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
