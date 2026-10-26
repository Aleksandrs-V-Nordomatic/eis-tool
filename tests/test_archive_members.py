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


if __name__ == "__main__":
    unittest.main()
