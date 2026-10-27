import os
import shutil
import tempfile
import unittest

import normalize


class BracketedName(unittest.TestCase):
    """EIS names a file "<title> (<original file>)", so the name ends with a bracket."""

    def test_a_workbook_named_with_a_closing_bracket_is_read(self):
        import openpyxl
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = os.path.join(folder, "034_9.pielikums (9_pielikums_FP-veidne.xlsx)")
        wb = openpyxl.Workbook()
        wb.active.append(["Pozicija", "Daudzums"])
        wb.active.append(["Kabelis", 12])
        wb.save(path)
        self.assertEqual(normalize.sniff(path), ".xlsx")
        text = normalize.xlsx_to_md(path)
        self.assertIn("Kabelis", text if isinstance(text, str) else text[0])


if __name__ == "__main__":
    unittest.main()
