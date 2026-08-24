#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The plan workbooks: a template every buyer saves slightly differently.

The bug these tests hold the line against produced no error and no warning. Buyers save the
mandatory template with a varying number of blank rows above the table — row 4 in one plan,
row 6 in the next — and reading the column header at a fixed index takes the blanks for
column names, filters every row away, and reports a buyer with nothing planned. A buyer who
planned nothing looks exactly the same, so nobody would have gone looking.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import xlrd                                          # noqa: F401
    import xlwt
    HAVE_XLWT = True
except ImportError:
    HAVE_XLWT = False

import lt_plans

COLUMNS = ["Pirkimo pavadinimas", "Aprašymas", "Pirkimo tipas", "Direktyva",
           "Procedūros tipas", "BVPŽ kodas", "Numatoma pirkimo sutarties vertė",
           "Numatoma pirkimo pradžios data"]


def workbook(blank_rows, lines):
    """A plan as a buyer saves it, with `blank_rows` spacers above the table."""
    book = xlwt.Workbook()
    sheet = book.add_sheet("DUOMENYS")
    header = [("Pirkimo vykdytojas", "UAB Bandymas"),
              ("Planuojamo pirkimo finansiniai metai", "2026"),
              ("Paskutinio atnaujinimo data", "46258.5"),
              ("Komentarai", "")]
    for r, (label, value) in enumerate(header):
        sheet.write(r, 0, label)
        sheet.write(r, 1, value)
    row = len(header) + blank_rows
    for c, name in enumerate(COLUMNS):
        sheet.write(row, c, name)
    for i, line in enumerate(lines, start=1):
        for c, value in enumerate(line):
            sheet.write(row + i, c, value)
    import io
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@unittest.skipUnless(HAVE_XLWT, "xlwt is needed to author a fixture workbook")
class TheHeaderRowIsFound(unittest.TestCase):

    def lines(self):
        return [["Gaisrinės signalizacijos įrengimas", "", "Darbai", "Nacionalinis pagrindas",
                 "Skelbiama apklausa", "45312100", 50000, "2026-II ketv."]]

    def test_a_plan_with_no_spacers_reads(self):
        head, rows = lt_plans.rows(workbook(0, self.lines()))
        self.assertEqual(len(rows), 1)
        self.assertEqual(head["Planuojamo pirkimo finansiniai metai"], "2026")

    def test_a_plan_with_spacers_reads_the_same(self):
        """The failure that started this: two blank rows and the table vanished."""
        head, rows = lt_plans.rows(workbook(2, self.lines()))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["BVPŽ kodas"], "45312100")

    def test_the_number_of_spacers_never_changes_the_answer(self):
        counts = {n: len(lt_plans.rows(workbook(n, self.lines()))[1]) for n in range(0, 5)}
        self.assertEqual(set(counts.values()), {1}, counts)

    def test_a_workbook_with_no_header_row_is_refused_not_reported_empty(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet("DUOMENYS")
        sheet.write(0, 0, "Pirkimo vykdytojas")
        import io
        buffer = io.BytesIO()
        book.save(buffer)
        with self.assertRaises(ValueError):
            lt_plans.rows(buffer.getvalue())

    def test_a_spacer_row_inside_the_table_is_skipped_not_kept(self):
        lines = self.lines() + [["", "", "", "", "", "", "", ""]] + self.lines()
        _, rows = lt_plans.rows(workbook(1, lines))
        self.assertEqual(len(rows), 2)


@unittest.skipUnless(HAVE_XLWT, "xlwt is needed to author a fixture workbook")
class TheLineAReaderGets(unittest.TestCase):

    def line(self, code="45312100"):
        data = workbook(1, [["Gaisrinės signalizacijos įrengimas", "aprašymas", "Darbai",
                             "Nacionalinis pagrindas", "Skelbiama apklausa", code, 50000,
                             "2026-II ketv."]])
        head, rows = lt_plans.rows(data)
        return lt_plans.as_line(rows[0], head, "UAB Bandymas", "1")

    def test_the_code_is_digits_only_however_the_buyer_typed_it(self):
        self.assertEqual(self.line("45312100-9")["cpv_main"], "45312100")
        self.assertEqual(self.line("45312100")["cpv_main"], "45312100")

    def test_a_missing_code_is_none_and_an_empty_list(self):
        line = self.line("")
        self.assertIsNone(line["cpv_main"])
        self.assertEqual(line["cpv"], [])

    def test_the_gate_can_read_the_line_as_it_reads_a_notice(self):
        """`batch.cpv_codes` looks under `cpv`, so a plan line gates like a tender."""
        import batch
        self.assertEqual(batch.cpv_codes(self.line()), ["45312100", "45312100"])


if __name__ == "__main__":
    unittest.main()


class ProcedureIsNeverEmptyForAKindThatHasOne(unittest.TestCase):
    """`Iepirkuma veids` is also what says which of the three kinds a card is.

    The consultation view does not print `Pirkimo būdas` — it is a different view of a
    different thing — so the field parses empty and the column with it, and the card loses
    the one thing that distinguishes a consultation from a competition. Derived, not
    guessed: a resource served from the PMC view is a market consultation.
    """

    def notice(self, kind, page="Peržiūrėti rinkos konsultaciją\nPavadinimas:\nBandymas"):
        import lt_page
        return lt_page.parse_notice(page, "1", kind)

    def test_a_consultation_carries_its_procedure_without_the_page_saying_so(self):
        self.assertEqual(self.notice("consultation")["procedure"], "Rinkos konsultacija")

    def test_a_competition_is_left_alone(self):
        """Nothing is invented for a kind whose page does print the field."""
        import lt_page
        page = "Peržiūrėti pirkimo\nPavadinimas:\nBandymas"
        self.assertIsNone(lt_page.parse_notice(page, "1", "tender")["procedure"])

    def test_the_page_wins_when_it_says_anything(self):
        import lt_page
        page = ("Peržiūrėti rinkos konsultaciją\nPavadinimas:\nBandymas\n"
                "Pirkimo būdas:\nAtviras konkursas")
        self.assertEqual(lt_page.parse_notice(page, "1", "consultation")["procedure"],
                         "Atviras konkursas")
