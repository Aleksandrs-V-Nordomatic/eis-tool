"""A listed EIS file is placed under a name one folder can hold.

On 9 November 2026 six Latvian tenders were lost in one run: their technical specifications
are listed by EIS as "<document title> (<file>)", the title held a slash, and the per-file
route used that text as a path into a folder that did not exist.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eis_fetch  # noqa: E402


class DiskNameTest(unittest.TestCase):
    def test_a_slash_in_the_title_becomes_part_of_the_name(self):
        name = eis_fetch.disk_name(
            "Tehniskā specifikācija / Projektēšanas uzdevums (P7_Tehniska specifikacija.doc)")
        self.assertNotIn("/", name)
        self.assertTrue(name.endswith("(P7_Tehniska specifikacija.doc)"))

    def test_backslashes_and_control_characters_go(self):
        self.assertEqual(eis_fetch.disk_name("a\\b\x01c.pdf"), "a_b_c.pdf")

    def test_a_long_name_keeps_its_extension_within_the_limit(self):
        name = eis_fetch.disk_name("ā" * 300 + ".pdf")
        self.assertLessEqual(len(name.encode("utf-8")), eis_fetch.NAME_BYTES)
        self.assertTrue(name.endswith(".pdf"))

    def test_an_empty_name_is_still_a_file(self):
        self.assertEqual(eis_fetch.disk_name(""), "file")
        self.assertEqual(eis_fetch.disk_name(" . "), "file")

    def test_an_ordinary_name_is_unchanged(self):
        self.assertEqual(eis_fetch.disk_name("Nolikums.docx"), "Nolikums.docx")


class PlaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_file_whose_title_holds_a_slash_lands_in_the_folder(self):
        src = os.path.join(self.tmp, "download")
        with open(src, "wb") as fh:
            fh.write(b"spec")
        dest = os.path.join(self.tmp, "documents", "actual")
        target, _, dup = eis_fetch.place(
            src, dest, "Tehniskā specifikācija / tehniskais piedāvājums (P2_TS.xlsx)", {})
        self.assertFalse(dup)
        self.assertEqual(os.path.dirname(target), dest)
        self.assertTrue(os.path.isfile(target))


if __name__ == "__main__":
    unittest.main()
