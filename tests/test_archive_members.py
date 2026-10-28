import os
import shutil
import subprocess
import tempfile
import unittest

import archives

LISTING_7Z = """\
Path = 1.2. pielikums Objektu detalizets apraksts
Size = 0
Attributes = RD dr-xr-xr-x

Path = 1.2. pielikums Objektu detalizets apraksts\\Aizkraukles PII
Size = 0
Attributes = D

Path = 1.2. pielikums Objektu detalizets apraksts\\Aizkraukles PII\\apraksts.pdf
Size = 1200
Attributes = A -rw-r--r--
"""

LISTING_RAR = """\
Path = Projekts
Folder = +
Attributes = D

Path = Projekts\\Rasejumi\\AR-01.pdf
Folder = -
Attributes = A
"""


class Listing(unittest.TestCase):
    def test_a_7z_folder_is_known_by_its_attributes(self):
        self.assertEqual(archives.listed_files(LISTING_7Z),
                         ["1.2. pielikums Objektu detalizets apraksts/Aizkraukles PII/apraksts.pdf"])

    def test_a_rar_folder_is_known_by_its_folder_line(self):
        self.assertEqual(archives.listed_files(LISTING_RAR), ["Projekts/Rasejumi/AR-01.pdf"])


@unittest.skipUnless(archives.seven_zip(), "7-Zip is not installed")
class SolidArchive(unittest.TestCase):
    """A solid 7z archive, where every member sits in one compressed block."""

    MEMBERS = {"Eleja/Dokumenti/apraksts.pdf": b"eleja", "Skriveri/Dokumenti/apraksts.pdf": b"skriveri",
               "Skriveri/Atteli/foto.jpg": b"photo"}

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        source = os.path.join(self.root, "source")
        for name, data in self.MEMBERS.items():
            path = os.path.join(source, *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
        self.files = os.path.join(self.root, "files")
        os.makedirs(self.files)
        self.archive = os.path.join(self.files, "003_apraksts.7z")
        subprocess.run([archives.seven_zip(), "a", "-ms=on", self.archive, "Eleja", "Skriveri"],
                       cwd=source, capture_output=True, check=True)
        self.picks = [("Eleja/Dokumenti/apraksts.pdf", os.path.join(self.files, "003.000_apraksts.pdf")),
                      ("Skriveri/Dokumenti/apraksts.pdf", os.path.join(self.files, "003.001_apraksts.pdf"))]

    def test_the_chosen_members_land_whole_and_the_rest_stay_packed(self):
        archives.extract_many(self.archive, self.picks)
        with open(self.picks[0][1], "rb") as fh:
            self.assertEqual(fh.read(), b"eleja")
        with open(self.picks[1][1], "rb") as fh:
            self.assertEqual(fh.read(), b"skriveri")
        self.assertEqual(sorted(os.listdir(self.files)),
                         ["003.000_apraksts.pdf", "003.001_apraksts.pdf", "003_apraksts.7z"])

    def test_the_archive_is_read_once_for_all_its_members(self):
        calls, real = [], archives.subprocess.run

        def counted(args, **kw):
            calls.append(args[1])
            return real(args, **kw)

        archives.subprocess.run = counted
        self.addCleanup(setattr, archives.subprocess, "run", real)
        archives.extract_many(self.archive, self.picks)
        self.assertEqual(calls, ["x"])

    def test_a_member_the_archive_does_not_hold_leaves_nothing_that_looks_finished(self):
        picks = self.picks + [("Kalna/nav.pdf", os.path.join(self.files, "003.009_nav.pdf"))]
        with self.assertRaises(archives.Unreadable):
            archives.extract_many(self.archive, picks)
        self.assertFalse(os.path.exists(picks[2][1]))
        self.assertFalse([n for n in os.listdir(self.files) if n.startswith(".unpack")])


if __name__ == "__main__":
    unittest.main()
