import os
import unittest

import selection

RULES = selection.Rules.load(os.path.join(os.path.dirname(__file__), "fixtures",
                                          "document_rules.json"))


class Kind(unittest.TestCase):
    def test_the_extension_decides_first(self):
        self.assertEqual(RULES.kind_of("Nolikums.dwg"), "CAD files")
        self.assertEqual(RULES.kind_of("IMG_001.JPG"), "Photos and scans")

    def test_a_name_is_folded_before_it_is_matched(self):
        self.assertEqual(RULES.kind_of("Tehniskā_specifikācija.docx"), "Technical specification")
        self.assertEqual(RULES.kind_of("ATBILDES UZ JAUTĀJUMIEM.pdf"),
                         "Answers to bidders' questions")

    def test_eis_names_its_own_form_and_the_record_says_what_it_is(self):
        self.assertEqual(RULES.kind_of("184282_PD.ANY_2026_10_01.pdf",
                                       record_title="Iepirkuma priekšmeta prasības"),
                         "EIS requirement form")

    def test_a_section_code_is_matched_on_the_original_spelling(self):
        self.assertEqual(RULES.kind_of("VAS-01.pdf"), "Drawing sheets")
        self.assertEqual(RULES.kind_of("ar ko strādāt.pdf"), "Unknown")

    def test_the_folder_speaks_when_the_name_says_nothing(self):
        self.assertEqual(RULES.kind_of("01.pdf", folders=["Rasejumi"]), "Drawing sheets")

    def test_then_the_archive_then_the_record(self):
        self.assertEqual(RULES.kind_of("01.pdf", archive="Nolikums.zip"), "Tender regulations")
        self.assertEqual(RULES.kind_of("01.pdf", record_title="Darba uzdevums"),
                         "Technical specification")

    def test_a_weak_name_waits_for_the_context(self):
        self.assertEqual(RULES.kind_of("Grozījumi.pdf", record_title="Nolikums"),
                         "Tender regulations")
        self.assertEqual(RULES.kind_of("Grozījumi.pdf"), "Amendments")

    def test_the_generic_requirements_record_is_not_a_context(self):
        self.assertEqual(RULES.kind_of("x.pdf", record_title="Iepirkuma priekšmeta prasības"),
                         "Unknown")

    def test_nothing_fits_is_unknown(self):
        self.assertEqual(RULES.kind_of("x.pdf"), "Unknown")

    def test_an_archive_name_that_states_an_unread_kind_decides_first(self):
        self.assertEqual(RULES.kind_of("TS_Annex_6_Photos of the object.zip"), "Photos and scans")
        self.assertEqual(RULES.kind_of("Nolikums rasējumi.7z"), "Drawing sheets")

    def test_an_archive_name_that_also_names_a_read_kind_is_opened(self):
        self.assertEqual(RULES.kind_of("Tehniskā specifikācija un foto.zip"),
                         "Technical specification")
        self.assertEqual(RULES.kind_of("Nolikums.zip"), "Tender regulations")

    def test_the_archive_name_rule_is_for_archives_only(self):
        self.assertEqual(RULES.kind_of("Tehniskā specifikācija foto.pdf"), "Technical specification")


class Decision(unittest.TestCase):
    def test_a_skipped_kind_keeps_only_its_name(self):
        self.assertEqual(RULES.decide("Drawing sheets")[0], selection.SKIP)

    def test_unknown_is_downloaded(self):
        self.assertEqual(RULES.decide("Unknown")[0], selection.DOWNLOAD)

    def test_the_archive_section_is_never_downloaded(self):
        self.assertEqual(RULES.decide("Tender regulations", "archive")[0], selection.SKIP)

    def test_archives_are_recognised_by_extension(self):
        self.assertTrue(RULES.is_archive("Projekts.RAR"))
        self.assertFalse(RULES.is_archive("Projekts.pdf"))


if __name__ == "__main__":
    unittest.main()
