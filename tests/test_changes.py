#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The diff itself, with no drive and no pack anywhere near it.

`test_deliver_index` holds what a second delivery COSTS. This file holds what it SAYS, and
particularly the four ways a diff of this shape can lie:

  * calling a pipeline upgrade a change, when a new extractor rewrote identical text;
  * calling a change in how the tender was found a change in the tender;
  * collapsing two hundred archive members into one document because they share a digest;
  * missing a file swapped inside a record whose publish date never moved.

Each of those would train a reader to stop believing the file, which costs more than the
delivery ever saved.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import changes


def manifest(records):
    return {"documents": records, "withheld_records": []}


def record(rid="r1", publish="2026-08-01", files=(), **kw):
    body = {"id": rid, "section": "actual", "title": "Nolikums", "type_code": "PRCDOC",
            "publish_date": publish,
            "files": [{"filename": n, "original_name": n, "sha256": d, "size": 1}
                      for n, d in files]}
    body.update(kw)
    return body


def normalized(*docs):
    """docs are (source, original_sha256, chars)."""
    return {"documents": [{"source": s, "original_sha256": d, "markdown_path": "x/document.md",
                           "markdown_chars": c, "section": "actual", "record_id": "r1",
                           "original_file": s.rsplit("/", 1)[-1]}
                          for s, d, c in docs],
            "unreadable_files": []}


def state(facts=None, records=(), docs=(), tool="v1"):
    return changes.fingerprint("111", dict({"title": "T", "deadline": "2026-09-01"},
                                           **(facts or {})),
                               manifest(list(records)), normalized(*docs), tool=tool)


class DocumentIdentity(unittest.TestCase):
    def test_archive_members_do_not_collapse_into_one_document(self):
        # `normalize` stamps the CARRIER's digest on every entry it produced, so all two
        # hundred members of one archive share `original_sha256`. Keyed on that alone they
        # would be one document, and a delivery would send one file of the two hundred.
        keys = {changes.document_key("aa" * 32, "actual/zip/%d.pdf" % i) for i in range(5)}
        self.assertEqual(len(keys), 5)

    def test_the_same_file_in_the_same_place_keeps_its_address(self):
        self.assertEqual(changes.document_key("aa" * 32, "actual/n.pdf"),
                         changes.document_key("aa" * 32, "actual/n.pdf"))

    def test_different_bytes_under_one_name_are_different_documents(self):
        self.assertNotEqual(changes.document_key("aa" * 32, "actual/n.pdf"),
                            changes.document_key("bb" * 32, "actual/n.pdf"))


class NotAChange(unittest.TestCase):
    def test_an_identical_tender_is_unchanged(self):
        before = state(records=[record(files=[("n.pdf", "aa")])], docs=[("actual/n.pdf", "aa", 9)])
        after = state(records=[record(files=[("n.pdf", "aa")])], docs=[("actual/n.pdf", "aa", 9)])
        move = changes.diff(before, after)
        self.assertEqual(move["status"], "unchanged")
        self.assertEqual(move["carried_over"], 1)

    def test_a_new_extractor_is_not_the_buyer_doing_something(self):
        # The one that would be reported as an amendment by anything comparing the Markdown.
        before = state(docs=[("actual/n.pdf", "aa", 9)], tool="v1")
        after = state(docs=[("actual/n.pdf", "aa", 9)], tool="v2")
        move = changes.diff(before, after)
        self.assertEqual(move["status"], "unchanged")
        self.assertTrue(move["reextracted"])

    def test_how_the_tender_was_found_is_not_a_fact_about_the_tender(self):
        # A tender found through the register on Monday and fetched by id on Tuesday has not
        # changed; only the route to it has. Reporting that would train a reader to ignore
        # the whole file, because the caller varies how it asks all the time.
        before = state(facts={"register_check": "discovery", "eis_only": False})
        after = state(facts={"register_check": "unverified", "eis_only": True})
        self.assertEqual(changes.diff(before, after)["status"], "unchanged")

    def test_the_raw_field_map_is_not_diffed_against_the_values_read_out_of_it(self):
        before = state(facts={"fields": {"#Name": "T", "noise": "1"}})
        after = state(facts={"fields": {"#Name": "T", "noise": "2"}})
        self.assertEqual(changes.diff(before, after)["status"], "unchanged")


class TheSameTenderInAnotherLanguage(unittest.TestCase):
    """EIS serves the same procurement in Latvian or in English and the caller cannot choose.

    Measured on one tender fetched four times in one morning: the downloader asked for
    Latvian first every time and was answered in English twice. Every field whose value is a
    display string moves together when that happens, and reporting ten amendments that did
    not happen is how a reader learns to stop reading the file.
    """

    LV = {"fields": {"Iepirkuma statuss": "Izsludināts"},
          "status": "Izsludināts", "work_kind": "Būvdarbi", "lots": "Nē",
          "deadline": "28.08.2026 09:00 (plānots)",
          "title": "Pamatskolas piebūves būvniecība", "ref": "RNP 2026/80",
          "profile": "PIL_Atklāts_konkurss", "cpv_main": "45000000-7"}
    EN = dict(LV, fields={"Procurement status": "Announced"},
              status="Announced", work_kind="Construction works", lots="No",
              deadline="28.08.2026 09:00 (scheduled)")

    def state(self, facts, **kw):
        return changes.fingerprint("111", facts, manifest([record()]), normalized(), **kw)

    def test_the_language_is_read_off_the_labels_the_page_carried(self):
        self.assertEqual(changes.page_language(self.LV), "lv")
        self.assertEqual(changes.page_language(self.EN), "en")
        self.assertIsNone(changes.page_language({"fields": {}}))

    def test_a_translated_page_is_not_an_amended_tender(self):
        move = changes.diff(self.state(self.LV), self.state(self.EN))
        self.assertEqual(move["status"], "unchanged")
        self.assertEqual(move["facts"], [])

    def test_the_flip_is_reported_rather_than_swallowed(self):
        move = changes.diff(self.state(self.LV), self.state(self.EN))
        self.assertEqual(move["language"], {"from": "lv", "to": "en"})
        self.assertIn("status", move["facts_not_compared"])
        self.assertIn("deadline", move["facts_not_compared"])

    def test_what_survives_translation_is_still_compared_across_one(self):
        # Dropping every field on a flip would let a real amendment hide behind one.
        moved = dict(self.EN, ref="RNP 2026/81")
        move = changes.diff(self.state(self.LV), self.state(moved))
        self.assertEqual(move["status"], "changed")
        self.assertEqual([f["field"] for f in move["facts"]], ["ref"])

    def test_within_one_language_everything_is_compared_as_before(self):
        moved = dict(self.LV, status="Pārtraukts")
        move = changes.diff(self.state(self.LV), self.state(moved))
        self.assertEqual(move["status"], "changed")
        self.assertEqual([f["field"] for f in move["facts"]], ["status"])
        self.assertIsNone(move["language"])
        self.assertEqual(move["facts_not_compared"], [])


class WhatMoved(unittest.TestCase):
    def test_a_moved_deadline_is_named_with_both_values(self):
        move = changes.diff(state(), state(facts={"deadline": "2026-09-15"}))
        self.assertEqual(move["status"], "changed")
        self.assertEqual(move["facts"],
                         [{"field": "deadline", "from": "2026-09-01", "to": "2026-09-15"}])

    def test_a_new_record_is_listed_with_the_title_a_person_reads(self):
        after = state(records=[record(), record("r2", "2026-08-12", title="Grozījumi Nr. 1")])
        move = changes.diff(state(records=[record()]), after)
        self.assertEqual([r["title"] for r in move["records_added"]], ["Grozījumi Nr. 1"])
        self.assertEqual(move["records_removed"], [])

    def test_a_record_the_portal_published_without_a_download_still_counts(self):
        # It is a record, and its arrival is news of exactly the same kind. Listed with no
        # files rather than omitted, so it cannot later look like a record that vanished.
        after = changes.fingerprint("111", {}, {"documents": [], "withheld_records": [
            {"id": "r9", "title": "Nolikums", "publish_date": "2026-08-12"}]}, normalized())
        move = changes.diff(changes.fingerprint("111", {}, manifest([]), normalized()), after)
        self.assertEqual([r["id"] for r in move["records_added"]], ["r9"])
        self.assertTrue(move["records_added"][0]["withheld"])

    def test_a_replaced_document_is_an_addition_and_a_removal(self):
        before = state(records=[record(files=[("n.pdf", "aa")])], docs=[("actual/n.pdf", "aa", 9)])
        after = state(records=[record(files=[("n.pdf", "bb")])], docs=[("actual/n.pdf", "bb", 9)])
        move = changes.diff(before, after)
        self.assertEqual([d["name"] for d in move["documents_added"]], ["n.pdf"])
        self.assertEqual([d["name"] for d in move["documents_removed"]], ["n.pdf"])
        self.assertEqual(move["carried_over"], 0)

    def test_a_file_swapped_without_the_date_moving_is_called_out(self):
        # The one update the page's own metadata does not announce, and the reason this
        # compares digests rather than trusting publish dates. Skipping a re-download on the
        # strength of an unchanged date would miss exactly this, which is why the fetch still
        # takes everything and only the delivery is a delta.
        before = state(records=[record(files=[("n.pdf", "aa")])])
        after = state(records=[record(files=[("n.pdf", "bb")])])
        move = changes.diff(before, after)
        self.assertEqual([r["id"] for r in move["records_changed"]], ["r1"])
        self.assertTrue(move["records_changed"][0]["silent"])
        self.assertEqual(changes.summary(move)["silent_records"], 1)

    def test_a_file_added_with_a_new_date_is_not_called_silent(self):
        before = state(records=[record(files=[("n.pdf", "aa")])])
        after = state(records=[record(publish="2026-08-12",
                                      files=[("n.pdf", "aa"), ("b.pdf", "bb")])])
        move = changes.diff(before, after)
        self.assertEqual(move["records_changed"][0]["files_added"], ["b.pdf"])
        self.assertFalse(move["records_changed"][0]["silent"])

    def test_a_scan_that_appeared_is_reported_even_though_nobody_could_read_it(self):
        # A file no decoder could read is not an absent file. If it stopped showing up in the
        # diff, the only trace of "a drawing arrived" would be a count nobody compares.
        before = changes.fingerprint("111", {}, manifest([]), normalized())
        after = changes.fingerprint("111", {}, manifest([]),
                                    {"documents": [], "unreadable_files": [
                                        {"file": "actual/plan.dwg", "sha256": "cc",
                                         "reason": "no text layer", "bytes": 12}]})
        move = changes.diff(before, after)
        self.assertEqual(move["status"], "changed")
        self.assertEqual([g["file"] for g in move["unreadable_added"]], ["actual/plan.dwg"])


class FirstSighting(unittest.TestCase):
    def test_nothing_is_enumerated_because_everything_is_being_sent(self):
        move = changes.diff(None, state(docs=[("actual/n.pdf", "aa", 9)]), date="2026-08-11")
        self.assertEqual(move["status"], "new")
        self.assertEqual(move["counts"]["documents"], 1)
        self.assertEqual(move["first_seen"], "2026-08-11")
        self.assertNotIn("documents_added", move)

    def test_everything_is_sent(self):
        current = state(docs=[("actual/a.pdf", "aa", 9), ("actual/b.pdf", "bb", 9)])
        move = changes.diff(None, current)
        self.assertEqual(changes.documents_to_send(move, current), sorted(current["documents"]))


class WhatTravels(unittest.TestCase):
    def test_only_the_additions_travel(self):
        before = state(docs=[("actual/a.pdf", "aa", 9)])
        current = state(docs=[("actual/a.pdf", "aa", 9), ("actual/b.pdf", "bb", 9)])
        move = changes.diff(before, current)
        added = changes.document_key("bb", "actual/b.pdf")
        self.assertEqual(changes.documents_to_send(move, current), [added])

    def test_nothing_travels_when_nothing_moved(self):
        before = state(docs=[("actual/a.pdf", "aa", 9)])
        current = state(docs=[("actual/a.pdf", "aa", 9)])
        self.assertEqual(changes.documents_to_send(changes.diff(before, current), current), [])

    def test_everything_travels_again_when_the_extractor_moved(self):
        # The tender did not change, but the Markdown on the drive came from an older
        # extractor. Leaving it there would make "same bytes in, same text out" true of the
        # pipeline and false of the delivery.
        before = state(docs=[("actual/a.pdf", "aa", 9)], tool="v1")
        current = state(docs=[("actual/a.pdf", "aa", 9)], tool="v2")
        move = changes.diff(before, current)
        self.assertEqual(changes.documents_to_send(move, current), sorted(current["documents"]))


class PipelineVersion(unittest.TestCase):
    """What the extractor version tracks, and what it must not.

    A tender whose version moved has every document re-uploaded, so a value that moves for
    reasons unrelated to the text turns an unrelated edit into a re-delivery of the whole
    corpus. This is the guard on that.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="eis_pipe_")
        self.addCleanup(shutil.rmtree, self.root, True)
        for name in changes.PIPELINE_FILES:
            self.write(name, "original\n")

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_the_same_extraction_path_gives_the_same_version(self):
        self.assertEqual(changes.pipeline_version(self.root),
                         changes.pipeline_version(self.root))

    def test_changing_the_extractor_moves_it(self):
        before = changes.pipeline_version(self.root)
        self.write("normalize.py", "different\n")
        self.assertNotEqual(changes.pipeline_version(self.root), before)

    def test_changing_the_pinned_libraries_moves_it(self):
        before = changes.pipeline_version(self.root)
        self.write("requirements.txt", "pymupdf==1.0.0\n")
        self.assertNotEqual(changes.pipeline_version(self.root), before)

    def test_a_file_that_cannot_change_the_text_does_not_move_it(self):
        # The delivery, the diff and the tests are edited constantly and none of them can
        # change a character of the Markdown. Tracking them would re-upload every document of
        # every tender ever fetched for a corrected comment.
        before = changes.pipeline_version(self.root)
        self.write("deliver_graph.py", "rewritten\n")
        self.write("changes.py", "rewritten\n")
        self.assertEqual(changes.pipeline_version(self.root), before)

    def test_an_unreadable_path_is_unknown_rather_than_guessed(self):
        # Not evidence that the pipeline is the same, so not a value that says it is.
        os.remove(os.path.join(self.root, "normalize.py"))
        self.assertIsNone(changes.pipeline_version(self.root))


class ANewParserIsNotAnAmendment(unittest.TestCase):
    """The facts are read by `eis_page`, and improving it must not accuse every buyer at once.

    A spelling added to a label map, a field that used to come back null — one such edit
    changes facts across the whole corpus in a single day. Compared blind, that is an
    amendment reported for every tender in the register, from our own side. It is the page
    language all over again, and it gets the same treatment: not compared, and said out loud.
    """

    def state(self, parser, facts=None):
        return changes.fingerprint("111", dict({"title": "T", "status": "Izsludināts"},
                                               **(facts or {})),
                                   manifest([record()]), normalized(), tool="v1", parser=parser)

    def test_it_is_not_the_extraction_version(self):
        # Sharing one value would re-upload every document of every tender for an edit that
        # cannot change a character of Markdown.
        self.assertNotEqual(changes.PARSER_FILES, changes.PIPELINE_FILES)
        self.assertNotIn("eis_page.py", changes.PIPELINE_FILES)
        self.assertNotIn("normalize.py", changes.PARSER_FILES)

    def test_facts_are_not_compared_across_a_parser_change(self):
        move = changes.diff(self.state("p1"), self.state("p2", {"status": "Announced"}))
        self.assertEqual(move["status"], "unchanged")
        self.assertEqual(move["facts"], [])
        self.assertTrue(move["reparsed"])
        self.assertEqual(move["facts_not_compared"], sorted(changes.FACTS))

    def test_the_home_is_still_refreshed_so_it_does_not_repeat_for_ever(self):
        # Nothing else moved, so the tender is unchanged — but the fingerprint must be
        # rewritten anyway, or the next run finds the old parser recorded and says the same
        # thing again, and again.
        move = changes.diff(self.state("p1"), self.state("p2"))
        self.assertEqual(move["status"], "unchanged")
        self.assertTrue(changes.refreshed(move))

    def test_but_no_document_travels_for_it(self):
        current = self.state("p2")
        move = changes.diff(self.state("p1"), current)
        self.assertEqual(changes.documents_to_send(move, current), [])

    def test_within_one_parser_facts_are_compared_as_before(self):
        move = changes.diff(self.state("p1"), self.state("p1", {"status": "Pārtraukts"}))
        self.assertEqual(move["status"], "changed")
        self.assertEqual([f["field"] for f in move["facts"]], ["status"])
        self.assertFalse(move["reparsed"])


class Determinism(unittest.TestCase):
    def test_the_same_inputs_give_the_same_answer_twice(self):
        before = state(records=[record(files=[("n.pdf", "aa")])], docs=[("actual/n.pdf", "aa", 9)])
        after = state(records=[record(files=[("n.pdf", "bb")])], docs=[("actual/n.pdf", "bb", 9)])
        self.assertEqual(changes.diff(before, after), changes.diff(before, after))

    def test_every_list_comes_out_sorted_rather_than_in_dict_order(self):
        before = changes.fingerprint("111", {}, manifest([]), normalized())
        after = state(records=[record("r3"), record("r1"), record("r2")],
                      docs=[("actual/c.pdf", "cc", 1), ("actual/a.pdf", "aa", 1)])
        move = changes.diff(before, after)
        self.assertEqual([r["id"] for r in move["records_added"]], ["r1", "r2", "r3"])
        keys = [d["key"] for d in move["documents_added"]]
        self.assertEqual(keys, sorted(keys))


if __name__ == "__main__":
    unittest.main()
