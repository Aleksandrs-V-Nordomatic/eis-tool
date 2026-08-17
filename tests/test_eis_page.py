#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The page parser, tested without a portal.

Every rule here was paid for once — by a wrong conclusion, a hand-collected deadline, or a
silently truncated document list. The fixture reproduces the shapes that caused them: a
tooltip inside a label attribute, a value in a bare text node beside its span, a nested
object inside the document array, a contact person, and the same document listed in both
sections.

A third of runners cannot reach EIS, so a test that needed the portal would be a test that
fails for reasons unrelated to the code. These need nothing.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eis_page


PAGE = """
<html><body>
<h1>Iepirkuma pamatdati</h1>

<label for="Ref">Iepirkuma identifikācijas numurs:</label>
<div class="field-block"><span class="field-text">RN 2026/21</span></div>

<label for="Title" title="<div>Palīdzība<br>par nosaukumu</div>">Iepirkuma nosaukums:</label>
<div class="field-block"><span class="field-text">Skolas p&#257;rb&#363;ve</span></div>

<label for="Org">Organiz&#257;cijas nosaukums:</label>
<div class="field-block"><span class="field-text">R&#299;gas dome, re&#291;. numurs: 90011524360</span></div>

<label for="Val">Paredzam&#257; v&#275;rt&#299;ba:</label>
<div class="field-block">63100.00 <span>EUR</span></div>

<label for="Dl">Pieteikumu/pied&#257;v&#257;jumu iesnieg&#353;anas termi&#326;&#353;:<span
   class="hint" title="pal&#299;gs">Kad j&#257;iesniedz</span></label>
<div class="field-block"><span class="field-text">2026-09-01 10:00</span></div>

<label for="Cpv">CPV galvenais kods:</label>
<div class="field-block"><span class="field-text">45000000-7</span></div>

<label for="SubjectDescription_CpvAdditionalIdList">CPV papildkods:</label>
<div class="field-block"><span class="field-text">var SubjectDescription_CpvAdditionals_items
 = [{"Title":"45200000-9 Izb&#363;ves darbi [1. k&#257;rta]","IsSelected":false},
    {"Title":"71000000-8 Arhitektu pakalpojumi","IsSelected":false},
    {"Title":"45200000-9 Izb&#363;ves darbi [1. k&#257;rta]","IsSelected":false}];
function SubjectDescription_CpvAdditionalsRefresh(data) { return null; }</span></div>

<label for="Kind">Pamatveids:</label>
<div class="field-block"><span class="field-text">B&#363;vdarbi</span></div>

<label for="Pub">Izsludin&#257;ts:</label>
<div class="field-block"><span class="field-text">2026-08-01</span></div>

<label for="Dur">L&#299;guma darb&#299;bas termi&#326;&#353;:</label>
<div class="field-block"><span class="field-text">17 M&#275;ne&#353;i</span></div>

<label for="Sel">Izv&#275;les metode:</label>
<div class="field-block"><span class="field-text">Tikai zem&#257;k&#257;s cenas v&#275;rt&#275;&#353;ana</span></div>

<label for="Doc">Dokument&#257;cijas izsnieg&#353;anas termi&#326;&#353;:</label>
<div class="field-block"><span class="field-text">2026-08-25 17:00</span></div>

<label for="Person">Kontaktpersona:</label>
<div class="field-block"><span class="field-text">Jānis Bērziņš, janis@riga.lv, +371 20000000</span></div>

<label for="Mail">E-pasts:</label>
<div class="field-block"><span class="field-text">iepirkumi@riga.lv</span></div>

<a href="https://eformsb.pvs.iub.gov.lv/show/1b4e28ba-2fa1-11d2-883f-0016d3cca427">IUB</a>

<script>
var ActualDocuments_items = [
  {"Id": 111, "Title": "Nolikums", "TypeTitle": "Nolikums", "PublishDate": "2026-08-01",
   "DocumentLinkTypeCode": "PRCDOC",
   "DocumentTitle": {"Title": null, "ShowDownloadIcon": true, "Nested": [1, 2, 3]}},
  {"Id": 112, "Title": "Tehnisk\\u0101 specifik\\u0101cija", "TypeTitle": "Pielikums",
   "PublishDate": "2026-08-01", "DocumentTitle": {"Title": null}}
];
var HistoricalDocuments_items = [
  {"Id": 111, "Title": "Nolikums", "TypeTitle": "Nolikums"},
  {"Id": 99, "Title": "Nolikums (veca redakcija)", "TypeTitle": "Nolikums"}
];
</script>
</body></html>
"""


# The same tender as EIS actually served it on 2026-08-06: English labels, and the platform's
# own field ids. Reproduced by hand from a live page rather than saved from it — the real page
# names a contact person, and a fixture is not a reason to republish somebody's phone number.
ENGLISH_PAGE = """
<html><head><title>EPS - Procurements - AAA 2099/1 - Basic data</title></head><body>
<input name="__RequestVerificationToken" type="hidden" value="tok123" />

<label for="IdentificationNumber">Identification number:</label>
<div class="field-block"><span class="field-text">AAA 2099/1</span></div>

<label for="Name">Name:</label>
<div class="field-block"><span class="field-text">Skolas piebūves būvniecība</span></div>

<label for="OrganizerId">Contracting authority:</label>
<div class="field-block"><span class="field-text">Ropažu novada pašvaldība, reģ. numurs: 90000067986</span></div>

<label for="stage_ProposalSubmissionDate">Applications / proposals submission deadline:</label>
<div class="field-block"><span class="field-text">27.08.2026 10:00 (scheduled)</span></div>

<label for="SubjectDescription_CpvMainId">CPV main code:</label>
<div class="field-block"><span class="field-text">45000000-7 Celtniecības darbi.</span></div>

<label for="SubjectDescription_ProcurementObjectTypeId">Object type:</label>
<div class="field-block"><span class="field-text">Construction works</span></div>

<label for="SubjectDescription_EstimatedContractValue">Expected contract price:</label>
<div class="field-block"><span class="field-text">5445650.00 EUR</span></div>

<label for="SubjectDescription_ContractDuration">Performance of the contract:</label>
<div class="field-block"><span class="field-text">17 Months</span></div>

<label for="ProposalSelectionMethodId">Award criteria:</label>
<div class="field-block"><span class="field-text">Tender with lowest price</span></div>

<script>var ActualDocuments_items = [{"Id": 8362070, "Title": "Nolikums"}];</script>
</body></html>
"""


class EnglishPage(unittest.TestCase):
    """EIS serves this tender in English, and `Accept-Language: lv` does not change it.

    The old rule — the page must contain `Iepirkuma pamatdati` — calls this tender
    unpublished. It is not: it carries the token, the document array and every field.
    """

    def setUp(self):
        self.notice = eis_page.parse_notice(ENGLISH_PAGE, "178345")

    def test_an_english_page_is_still_a_published_procurement(self):
        self.assertTrue(eis_page.is_published(ENGLISH_PAGE))
        self.assertIsNotNone(self.notice)

    def test_the_fields_are_found_by_platform_id_not_by_language(self):
        self.assertEqual(self.notice["ref"], "AAA 2099/1")
        self.assertEqual(self.notice["title"], "Skolas piebūves būvniecība")
        self.assertEqual(self.notice["deadline"], "27.08.2026 10:00 (scheduled)")
        self.assertEqual(self.notice["cpv_main"], "45000000-7 Celtniecības darbi.")
        self.assertEqual(self.notice["work_kind"], "Construction works")
        self.assertEqual(self.notice["contract_duration"], "17 Months")
        self.assertEqual(self.notice["award_criteria"], "Tender with lowest price")

    def test_the_estimated_value_survives_an_english_page(self):
        # It did not. `_value` was keyed on `EstimatedValue`, an id EIS never emits, so the
        # lookup fell through to the labels — where the English one was guessed as
        # "Estimated value" and the page says "Expected contract price". Both routes missed
        # and the tender came back worth nothing, with no error anywhere. Measured over 169
        # collected pages: 14 of the 35 English ones published a value and lost all of it,
        # the largest 5,445,650 EUR. The Latvian label happened to be right, which is why
        # only half the corpus showed the hole.
        self.assertEqual(self.notice["value"], 5445650.0)
        self.assertEqual(self.notice["currency"], "EUR")

    def test_every_field_this_page_carries_resolves_from_the_ids_alone(self):
        # FIELDS puts the id first precisely so a reworded label cannot lose a field. That
        # promise only holds if the id is the one EIS emits — and when it is not, the entry
        # quietly rides on its labels and looks fine until the portal serves the other
        # language. Stripping every visible label is the cheapest way to say so out loud.
        ids_only = {k: v for k, v in self.notice["fields"].items() if k.startswith("#")}
        for key in eis_page.FIELDS:
            whole = eis_page.field(self.notice["fields"], key)
            if whole is None:
                continue
            self.assertEqual(eis_page.field(ids_only, key), whole,
                             "%s resolves only through a label — its id is wrong" % key)

    def test_the_buyer_splits_on_the_latvian_suffix_even_here(self):
        # `reģ. numurs:` stays Latvian on the English page.
        self.assertEqual(self.notice["buyer"], "Ropažu novada pašvaldība")
        self.assertEqual(self.notice["buyer_reg"], "90000067986")

    def test_a_redirect_stub_is_not_a_procurement(self):
        # What an unpublished id really answers: 302 to a 244-byte "Object moved" page.
        self.assertFalse(eis_page.is_published(
            '<html><head><title>Object moved</title></head>'
            '<body><h2>Object moved to <a href="/">here</a>.</h2></body></html>'))

    def test_structure_alone_is_enough_when_no_heading_matches(self):
        # Neither localized heading appears; the token and the document array do.
        self.assertNotIn("Iepirkuma pamatdati", ENGLISH_PAGE)
        self.assertTrue(eis_page.is_published(ENGLISH_PAGE))


class ParseNotice(unittest.TestCase):
    def setUp(self):
        self.notice = eis_page.parse_notice(PAGE, "178345")

    def test_unpublished_page_is_none_not_empty(self):
        # A page without the marker is a throttle, a stub or a wrong id — never a tender
        # with no data. Returning {} here is what once produced "not published" about a
        # tender that plainly was.
        self.assertIsNone(eis_page.parse_notice("<html>anything else</html>", "1"))
        self.assertIsNone(eis_page.parse_notice(None, "1"))

    def test_the_fields_a_card_cannot_be_written_without(self):
        self.assertEqual(self.notice["ref"], "RN 2026/21")
        self.assertEqual(self.notice["title"], "Skolas pārbūve")
        self.assertEqual(self.notice["deadline"], "2026-09-01 10:00")
        self.assertEqual(self.notice["cpv_main"], "45000000-7")
        self.assertEqual(self.notice["work_kind"], "Būvdarbi")
        self.assertEqual(self.notice["eis_id"], "178345")

    def test_buyer_is_split_from_its_registration_number(self):
        self.assertEqual(self.notice["buyer"], "Rīgas dome")
        self.assertEqual(self.notice["buyer_reg"], "90011524360")

    def test_value_reads_from_a_bare_text_node_beside_the_span(self):
        # `<div class="field-block">63100.00 <span>EUR</span>` — reading only the
        # field-text span returns nothing at all here.
        self.assertEqual(self.notice["value"], 63100.0)
        self.assertEqual(self.notice["currency"], "EUR")

    def test_the_contact_person_never_enters_the_parser(self):
        joined = json.dumps(self.notice, ensure_ascii=False)
        self.assertNotIn("Bērziņš", joined)
        self.assertNotIn("+371 20000000", joined)
        self.assertNotIn("janis@riga.lv", joined)
        self.assertNotIn("iepirkumi@riga.lv", joined)

    def test_a_tooltip_does_not_bleed_into_the_label(self):
        # Both shapes the portal can serve. A child element is the common one; an
        # attribute carrying raw `<div>` markup additionally ends the tag early for any
        # naive `[^>]*` regex, and then the "label" is the help text itself.
        keys = " ".join(self.notice["fields"])
        self.assertIn("Iepirkuma nosaukums", self.notice["fields"])          # attribute form
        self.assertIn("Pieteikumu/piedāvājumu iesniegšanas termiņš",
                      self.notice["fields"])                                 # child form
        self.assertNotIn("Palīdzība", keys)
        self.assertNotIn("Kad jāiesniedz", keys)

    def test_labels_written_as_entities_still_match(self):
        # `Organiz&#257;cijas nosaukums` and `Organizācijas nosaukums` are the same label;
        # a lookup that misses returns None and the tender loses a field without an error.
        self.assertIn("Organizācijas nosaukums", self.notice["fields"])
        self.assertIn("Paredzamā vērtība", self.notice["fields"])

    def test_the_latvian_labels_are_the_ones_the_portal_prints(self):
        # This fixture's `for=` attributes are deliberately not platform ids, so every
        # lookup here lands on the Latvian label — which makes it the only place a stale
        # spelling shows up. Three were stale: the map asked for "Līguma izpildes termiņš",
        # "Piedāvājuma izvēles kritērijs" and "Piegādātāju sanāksme" while EIS prints
        # "Līguma darbības termiņš", "Izvēles metode" and "Apspriedes ar piegādātājiem
        # termiņš". Their ids were right, so nothing was lost — the fallback was simply
        # dead, in the exact case it exists to cover.
        self.assertEqual(self.notice["contract_duration"], "17 Mēneši")
        self.assertEqual(self.notice["award_criteria"], "Tikai zemākās cenas vērtēšana")
        self.assertEqual(self.notice["docs_until"], "2026-08-25 17:00")

    def test_the_additional_cpv_codes_are_read_out_of_the_widget(self):
        # The page ships these as a repeater, so the "field" is kilobytes of JavaScript and
        # the codes were dropped with it. Measured over 169 collected pages, 74 carry at
        # least one — 172 codes in all, one tender holding 29. A tool that classifies by
        # CPV was discarding the codes on nearly half of what it fetched.
        self.assertEqual(self.notice["cpv_additional"],
                         ["45200000-9 Izbūves darbi [1. kārta]",
                          "71000000-8 Arhitektu pakalpojumi"])
        # The main code is not folded in: which one the buyer filed under is information.
        self.assertEqual(self.notice["cpv_main"], "45000000-7")

    def test_a_bracket_in_a_caption_does_not_truncate_the_list(self):
        # This is why the array is taken with `raw_decode` and not a `\\[.*?\\]` regex: the
        # first caption above closes a bracket, and a regex stopping there would return one
        # code, silently, with nothing to show a second ever existed.
        self.assertIn("[1. kārta]", self.notice["cpv_additional"][0])
        self.assertEqual(len(self.notice["cpv_additional"]), 2)

    def test_the_widget_javascript_never_reaches_the_output(self):
        # The slab was 65% of every delivered procurement.json — measured over 169 pages,
        # 1.62 MB of 2.50 MB — and every tender ships its procurement.json three times:
        # folder, archive, shards.zip. What stays under the widget's keys is what the page
        # shows a person, so `fields` still says the field exists and what it held.
        blob = json.dumps(self.notice, ensure_ascii=False)
        self.assertNotIn("CpvAdditionals_items", blob)
        self.assertNotIn("function", blob)
        self.assertEqual(
            self.notice["fields"]["CPV papildkods"],
            "45200000-9 Izbūves darbi [1. kārta]; 71000000-8 Arhitektu pakalpojumi")

    def test_a_procurement_with_no_extra_codes_gets_an_empty_list(self):
        # Absent is a list nobody has to special-case, not None and not a missing key.
        page = PAGE.replace("SubjectDescription_CpvAdditionals_items", "Unrelated_items")
        self.assertEqual(eis_page.parse_notice(page, "1")["cpv_additional"], [])

    def test_the_portal_prints_the_publication_date_under_several_captions(self):
        # `Izsludināts` on some procurements, `Izsludināts / publicēts` on others, and two
        # more spellings in English. The id carried all of them, so nothing was lost — but
        # the label fallback matched 111 of 134 Latvian pages and 23 of 35 English ones,
        # which is a fallback that would not have caught the id moving.
        for caption in ("Izsludin&#257;ts / public&#275;ts",
                        "Izsludin&#257;&#353;anas / public&#275;&#353;anas datums"):
            page = PAGE.replace("Izsludin&#257;ts:", caption + ":")
            fields = {k: v for k, v in eis_page.parse_fields(page).items()
                      if not k.startswith("#")}
            self.assertEqual(eis_page.field(fields, "published"), "2026-08-01", caption)

    def test_iub_link_decides_eis_only(self):
        self.assertEqual(self.notice["iub_uuid"], "1b4e28ba-2fa1-11d2-883f-0016d3cca427")
        self.assertFalse(self.notice["eis_only"])
        without = eis_page.parse_notice(PAGE.replace("eformsb.pvs.iub.gov.lv", "x.example"),
                                        "178345")
        self.assertTrue(without["eis_only"])

    def test_what_the_caller_knows_beats_what_the_page_shows(self):
        # The page below prints no register link at all — the ordinary case: of 43 flagged
        # pages, six were fetched live and five carried no iub.gov.lv URL of any shape.
        # When the caller reached it BY searching the register, that settles membership and
        # the page has no standing to contradict it.
        page = PAGE.replace("eformsb.pvs.iub.gov.lv", "x.example")
        blind = eis_page.parse_notice(page, "178345")
        self.assertTrue(blind["eis_only"])
        self.assertEqual(blind["register_check"], "unverified")
        self.assertIsNone(blind["iub_uuid"])

        known = eis_page.parse_notice(page, "178345",
                                      register_uuid="6f1c8a02-0000-4000-8000-00000000abcd")
        self.assertFalse(known["eis_only"])
        self.assertEqual(known["register_check"], "discovery")
        self.assertEqual(known["iub_uuid"], "6f1c8a02-0000-4000-8000-00000000abcd")

    def test_a_page_link_is_evidence_and_says_so(self):
        self.assertEqual(self.notice["register_check"], "page-link")
        self.assertFalse(self.notice["eis_only"])

    def test_a_planning_publication_is_a_register_link_too(self):
        # A market consultation is published as a planning publication, on a host that
        # differs from the notice host by one letter and under a path that carries a
        # numeric id instead of a uuid. Read off procurement 178056 live. Matching only the
        # notice shape called this page absent from the register while it printed the link.
        page = PAGE.replace(
            "https://eformsb.pvs.iub.gov.lv/show/1b4e28ba-2fa1-11d2-883f-0016d3cca427",
            "https://eforms.pvs.iub.gov.lv/planning-publications/view/pil-discussion/"
            "1078730/content")
        notice = eis_page.parse_notice(page, "178056")
        self.assertFalse(notice["eis_only"])
        self.assertEqual(notice["register_check"], "page-link")
        # There is no notice uuid in that URL, and inventing one would be worse than none:
        # a caller matching by uuid must miss rather than match the wrong thing.
        self.assertIsNone(notice["iub_uuid"])

    def test_a_walk_reports_unverified_rather_than_claiming_absence(self):
        # `walk_ids` reaches a page without having asked the register anything, so it is in
        # no position to say the register lacks the procurement. This is the case the flag
        # exists for and the one it must not overstate.
        page = PAGE.replace("eformsb.pvs.iub.gov.lv", "x.example")
        walked = list(eis_page.walk_ids(1, lambda pid: page if pid == 1 else None,
                                        stop_after_misses=1))
        self.assertEqual(len(walked), 1)
        self.assertEqual(walked[0]["register_check"], "unverified")


class ParseDocuments(unittest.TestCase):
    def test_both_sections_are_read_and_the_archive_is_labelled(self):
        docs = eis_page.parse_documents(PAGE)
        self.assertEqual([d["doc_id"] for d in docs], [111, 112, 99])
        self.assertEqual(docs[0]["section"], "current")
        self.assertEqual(docs[2]["section"], "archive")

    def test_a_document_listed_in_both_sections_is_one_document(self):
        docs = eis_page.parse_documents(PAGE)
        self.assertEqual(sum(1 for d in docs if d["doc_id"] == 111), 1)

    def test_nested_objects_do_not_truncate_the_array(self):
        # A non-greedy `\\[.*?\\]` stops at the first inner bracket — here the `Nested`
        # list inside document 111 — and silently loses every row after it.
        docs = eis_page.parse_documents(PAGE)
        self.assertEqual(len(docs), 3)
        self.assertEqual(docs[1]["title"], "Tehniskā specifikācija")

    def test_link_type_defaults_when_the_row_omits_it(self):
        docs = eis_page.parse_documents(PAGE)
        self.assertEqual(docs[1]["link_type"], "PRCDOC")

    def test_a_page_without_arrays_yields_no_documents(self):
        self.assertEqual(eis_page.parse_documents("<html>Iepirkuma pamatdati</html>"), [])


class Money(unittest.TestCase):
    def test_the_shapes_buyers_actually_write(self):
        self.assertEqual(eis_page.parse_money("63100.00 EUR"), 63100.0)
        self.assertEqual(eis_page.parse_money("1 250 000,50"), 1250000.5)
        self.assertEqual(eis_page.parse_money("42"), 42.0)

    def test_no_value_stated_is_none_not_zero(self):
        # Zero is a number a buyer could mean; "not stated" is not, and a display that
        # renders 0 EUR for an unstated value is a lie with a decimal point.
        self.assertIsNone(eis_page.parse_money(""))
        self.assertIsNone(eis_page.parse_money(None))
        self.assertIsNone(eis_page.parse_money("pēc vienošanās"))


class Resolve(unittest.TestCase):
    def test_finds_the_eis_id_on_an_iub_notice_page(self):
        html = ('<a href="https://www.eis.gov.lv/EKEIS/Supplier/Procurement/178345">'
                'Skatīt EIS</a>')
        self.assertEqual(eis_page.resolve_eis_id(html), "178345")

    def test_a_notice_with_no_eis_link_resolves_to_none(self):
        self.assertIsNone(eis_page.resolve_eis_id("<html>no link here</html>"))
        self.assertIsNone(eis_page.resolve_eis_id(None))


class Walk(unittest.TestCase):
    def _pages(self, published):
        def fetch(pid):
            return PAGE if pid in published else "<html>nothing</html>"
        return fetch

    def test_walks_over_gaps_instead_of_stopping_at_the_first_miss(self):
        # Ids are assigned before publication, so the public space is gappy. Stopping at
        # the first miss would end most walks immediately.
        found = list(eis_page.walk_ids(100, self._pages({100, 103, 104}),
                                       stop_after_misses=5))
        self.assertEqual(len(found), 3)

    def test_stops_after_a_long_run_of_misses(self):
        found = list(eis_page.walk_ids(100, self._pages(set()), stop_after_misses=3))
        self.assertEqual(found, [])

    def test_limit_caps_a_run(self):
        found = list(eis_page.walk_ids(100, self._pages(set(range(100, 200))),
                                       stop_after_misses=5, limit=4))
        self.assertEqual(len(found), 4)


if __name__ == "__main__":
    unittest.main()
