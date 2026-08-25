"""The Lithuanian procurement plans: what buyers say they are going to buy.

Not a stream and not a tender. Every contracting authority must publish its annual plan in
CVP IS and republish it within five working days of amending it, so the register is a stock
that keeps being refreshed rather than a day's worth of news. What it buys us is lead time:
an object appears here as a line in a spreadsheet months before it appears anywhere as a
notice.

WHERE IT LIVES, and this is the part that costs a day if taken from the obvious place. The
plan register on `cvpp.eviesiejipirkimai.lt` is FROZEN — the newest publication date
anywhere in it is 2 December 2024, plan year 2025 holds three rows and 2026 none. The whole
CVPP portal stopped receiving data on that date. The live register is in EPPS, at
`app/viewPublication.do`, and on 24 August 2026 it held 1,424 plans for the 2026 cycle with
submission timestamps from that same morning.

ONE FILE PER BUYER, ONE SHAPE FOR ALL OF THEM. Each plan is an `.xls` on a mandatory VPT
template: sheets `DUOMENYS` and `TYPES`, a four-line header naming the buyer and the
financial year, then a row per planned procurement. Measured over ten buyers: 3,688 lines,
one schema, and `BVPŽ kodas` filled on 91% of them — so the same CPV gate that runs on
notices runs here.

    python3 lt_plans.py --out work/LT --policy rules.json
"""
import argparse
import json
import os
import re
import time
import urllib.request

import lt_page

try:
    import batch as batch_mod
except Exception:
    batch_mod = None

REGISTER = lt_page.BASE + "/app/viewPublication.do"
PLAN = lt_page.BASE + "/app/downloadPlanFile.do?submissionId=%s"

_ROW = re.compile(r"(?is)<tr[^>]*>(.*?)</tr>")
_CELL = re.compile(r"(?is)<td[^>]*>(.*?)</td>")
_SUBMISSION = re.compile(r"downloadPlanFile\.do\?submissionId=(\d+)")
# The header block above the table: label in column A, value in column B.
HEADER_ROWS = 4
# The column header is FOUND, never counted to. Buyers save the template with a varying
# number of blank rows above the table — row 4 in one plan, row 6 in the next — and a fixed
# index reads the blanks as column names, filters every row away and reports a buyer with
# no planned procurements. Zero lines is exactly what a buyer who planned nothing looks
# like, so the bug would have been invisible.
FIRST_COLUMN = "pirkimo pavadinimas"


def _text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)
                  .replace("&nbsp;", " ").replace("&amp;", "&")
                  .replace("&#034;", '"')).strip()


def register():
    """Every published plan: the buyer, when they last submitted, and how to get it."""
    page = lt_page.fetch(REGISTER)
    out = []
    for fragment in _ROW.findall(page):
        found = _SUBMISSION.search(fragment.replace("&amp;", "&"))
        if not found:
            continue
        cells = [_text(c) for c in _CELL.findall(fragment)]
        if len(cells) < 2:
            continue
        out.append({"submission_id": found.group(1),
                    "buyer": cells[0],
                    # The portal's own wall clock, kept as it is printed: it is the only
                    # thing that says a buyer has amended a plan since we last read it.
                    "submitted": cells[1]})
    return out


def plan_file(submission_id, timeout=180):
    request = urllib.request.Request(PLAN % submission_id,
                                     headers={"User-Agent": lt_page.UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
    if not data[:4] in (b"\xd0\xcf\x11\xe0", b"PK\x03\x04"):
        # OLE2 for the .xls the template is, PK for a buyer who saved it as .xlsx. Anything
        # else is EPPS answering with a page, which it does under 200.
        raise RuntimeError("plan %s did not answer with a workbook (%d bytes)"
                           % (submission_id, len(data)))
    return data


def rows(data):
    """One dict per planned procurement, plus what the header block says about the plan."""
    import xlrd                                   # only the plans need it
    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_index(0)
    head = {}
    for r in range(min(HEADER_ROWS, sheet.nrows)):
        label = str(sheet.cell_value(r, 0)).strip().rstrip(":")
        value = str(sheet.cell_value(r, 1)).strip() if sheet.ncols > 1 else ""
        if label:
            head[label] = value
    header_row = None
    for r in range(sheet.nrows):
        if str(sheet.cell_value(r, 0)).strip().casefold() == FIRST_COLUMN:
            header_row = r
            break
    if header_row is None:
        # Said out loud rather than returned as an empty plan: a template we cannot read is
        # a gap in coverage, and it must not look like a buyer with nothing to buy.
        raise ValueError("no column header row: %r not found in column A" % FIRST_COLUMN)
    columns = [str(sheet.cell_value(header_row, c)).strip() for c in range(sheet.ncols)]
    out = []
    for r in range(header_row + 1, sheet.nrows):
        row = {}
        for c, name in enumerate(columns):
            if not name:
                continue
            value = sheet.cell_value(r, c)
            row[name] = str(value).strip() if not isinstance(value, float) else value
        if not str(row.get("Pirkimo pavadinimas") or "").strip():
            continue                              # a spacer row, of which the template has many
        out.append(row)
    return head, out


def as_line(row, head, buyer, submission_id):
    """The shape a reader gets, whatever the buyer called their columns."""
    code = re.sub(r"\D", "", str(row.get("BVPŽ kodas") or ""))[:8]
    return {
        "buyer": buyer,
        "submission_id": submission_id,
        "year": head.get("Planuojamo pirkimo finansiniai metai") or None,
        "title": row.get("Pirkimo pavadinimas"),
        "description": row.get("Aprašymas") or None,
        "object_type": row.get("Pirkimo tipas") or None,
        "procedure": row.get("Procedūros tipas") or None,
        "directive": row.get("Direktyva") or None,
        "cpv_main": code or None,
        "cpv": [code] if code else [],
        "starts": row.get("Numatoma pirkimo pradžios data") or None,
        "value": row.get("Numatoma pirkimo sutarties vertė") or None,
    }


def harvest(out_root, policy=None, limit=None, previous=None):
    """The whole register into `plans/`, gated, with only the amended buyers re-read."""
    rules = batch_mod.load_policy(policy) if batch_mod is not None else None
    seen = previous or {}
    published = register()
    if limit:
        published = published[:limit]

    base = os.path.join(out_root, "plans")
    raw_dir = os.path.join(base, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    kept, buyers, skipped, failed, unchanged = [], 0, 0, [], 0
    for entry in published:
        sid = entry["submission_id"]
        # A buyer who has not resubmitted since the last harvest has the same plan, and the
        # register's timestamp is the portal saying so. Re-downloading 1,424 workbooks to
        # learn that is the kind of politeness a nightly job cannot afford.
        if seen.get(sid) == entry["submitted"]:
            unchanged += 1
            continue
        try:
            data = plan_file(sid)
        except Exception as exc:
            failed.append({"submission_id": sid, "buyer": entry["buyer"],
                           "reason": str(exc)[:160]})
            continue
        with open(os.path.join(raw_dir, "%s.xls" % sid), "wb") as fh:
            fh.write(data)
        try:
            head, table = rows(data)
        except Exception as exc:
            failed.append({"submission_id": sid, "buyer": entry["buyer"],
                           "reason": "unreadable workbook: %s" % str(exc)[:140]})
            continue
        buyers += 1
        seen[sid] = entry["submitted"]
        for row in table:
            line = as_line(row, head, entry["buyer"], sid)
            if rules is not None and batch_mod.outside_scope(line, rules):
                skipped += 1
                continue
            kept.append(line)

    lines_path = os.path.join(base, "lines.jsonl")
    with open(lines_path, "w", encoding="utf-8") as fh:
        for line in kept:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    index = {
        "schema": "plans/1", "country": "LT", "source": "EPPS",
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "register": REGISTER,
        "published_plans": len(published),
        "buyers_read": buyers,
        "buyers_unchanged": unchanged,
        "lines_kept": len(kept),
        "lines_gated": skipped,
        "failed": failed,
        "seen": seen,
        "lines_path": "plans/lines.jsonl",
    }
    with open(os.path.join(base, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    return index, kept


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lithuanian annual procurement plans.")
    ap.add_argument("--out", default="work/LT")
    ap.add_argument("--policy", default=None)
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after this many buyers, for a trial")
    args = ap.parse_args(argv)

    prior_path = os.path.join(args.out, "plans", "index.json")
    previous = {}
    if os.path.exists(prior_path):
        with open(prior_path, encoding="utf-8") as fh:
            previous = json.load(fh).get("seen") or {}

    index, kept = harvest(args.out, args.policy, args.limit, previous)
    print("plans: %d published, %d buyer(s) read, %d unchanged, %d line(s) kept, %d gated"
          % (index["published_plans"], index["buyers_read"], index["buyers_unchanged"],
             index["lines_kept"], index["lines_gated"]))
    for line in kept[:12]:
        print("  %-10s %-34s %s" % (line["cpv_main"] or "—", (line["buyer"] or "")[:34],
                                    (line["title"] or "")[:58]))
    if index["failed"]:
        print("%d plan(s) could not be read, every one named:" % len(index["failed"]))
        for gap in index["failed"][:10]:
            print("   %s  %s" % (gap["submission_id"], gap["reason"][:70]))
    return index


if __name__ == "__main__":
    main()
