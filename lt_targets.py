"""Discovery for Lithuania: which procurements a window contains.

`batch.targets_from` does this for Latvia by walking the IUB register and resolving each
notice to its EIS page — real work, because the register's API never returns the platform
link. Lithuania needs none of that. EPPS searches its own publications by date, so a window
is one POST and the ids come back already addressed.

TWO POPULATIONS, ONE PASS. Tenders and market consultations are the same kind of resource
here, told apart by the `Pirkimo būdas` column. Consultations never appear in the portal's
"newest procurements" list — measured over 600 rows across nine working days, not one — so
a discovery that crawled that list would silently lose the population publishing DRAFT
technical specifications. The window search is a different query and carries them already,
which is why this asks once and reads the kind off the row: asking a second time with
`procedure=…pmc` returns the same records again, and a run that concatenated the two would
fetch every consultation twice and count the day wrong.

THE SEARCH IS STATEFUL, and getting that wrong looks like success. Criteria go in a POST
to `viewCFTSAction.do`, and the filtered results come back in that POST's own response.
A follow-up GET to the results page returns the UNFILTERED list — the same 200, the same
table, the wrong tenders. So every page is its own POST.

    python3 lt_targets.py 2026-08-20 2026-08-20          # one day, tenders
    python3 lt_targets.py 2026-08-20 2026-08-20 --consultations
"""
import argparse
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://viesiejipirkimai.lt/epps"
FORM = BASE + "/prepareAdvancedSearch.do?type=cftFTS"
SEARCH = BASE + "/viewCFTSAction.do"
PAGER = "d-3680175-p"          # displaytag's page parameter, and it travels in the POST

TENDERS = None                 # no procedure filter: every kind of tender in the window
CONSULTATIONS = "cft.procedure.type.pmc"
DPS = "cft.procedure.type.dps"
QS = "cft.procedure.type.qs"

UA = "Mozilla/5.0 (compatible; eis-tool)"
_ROW = re.compile(r"(?is)<tr[^>]*>(.*?)</tr>")
_CELL = re.compile(r"(?is)<td[^>]*>(.*?)</td>")
# The view a row links to depends on what it is: an ordinary tender, a market
# consultation, or a standing system. Missing one of them returns an empty list from a
# response that plainly had rows in it, which is how the doors read as "none open".
_ID = re.compile(r"(?:prepareViewCfTWS|prepareViewCfTDPSWS|viewPmc)\.do\?resourceId=(\d+)")
# Java's Date.toString(), which is what the table actually carries: the browser reformats it.
_STAMP = re.compile(r"^\w{3} (\w{3}) (\d{1,2}) (\d{2}:\d{2}:\d{2}) \w+ (\d{4})$")
_MONTH = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1)}


def _text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)
                  .replace("&nbsp;", " ").replace("&amp;", "&")
                  .replace("&#034;", '"').replace("&quot;", '"')).strip()


def as_iso(stamp):
    """`Thu Aug 21 10:20:02 EEST 2026` -> `2026-08-21T10:20:02`.

    The zone abbreviation is dropped rather than parsed: EEST is Vilnius, which is Riga,
    so the wall clock needs no conversion for a reader who thinks in Riga time. Keeping the
    raw string as well means nothing is lost if that ever stops being true.
    """
    m = _STAMP.match(stamp or "")
    if not m:
        return None
    month, day, clock, year = m.groups()
    if month not in _MONTH:
        return None
    return "%s-%02d-%02dT%s" % (year, _MONTH[month], int(day), clock)


class Session(object):
    """One cookie jar. EPPS hands an anonymous session to anyone who opens the form."""

    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [("User-Agent", UA)]
        self.opener.open(FORM, timeout=60).read()

    def page(self, criteria, number):
        body = dict(criteria, **{"mode": "advanced", "isFTS": "true", "type": "cftFTS",
                                 PAGER: str(number)})
        request = urllib.request.Request(
            SEARCH, data=urllib.parse.urlencode(body).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        return self.opener.open(request, timeout=90).read().decode("utf-8", "replace")


def parse_rows(html, kind):
    rows = []
    for fragment in _ROW.findall(html):
        found = _ID.search(fragment)
        if not found:
            continue
        cells = [_text(c) for c in _CELL.findall(fragment)]
        if len(cells) < 9:
            continue
        rows.append({
            "pid": found.group(1),
            "kind": kind,
            "title": cells[1],
            "buyer": cells[3],
            "published": as_iso(cells[5]),
            "published_raw": cells[5],
            "deadline": as_iso(cells[6]),
            "procedure": cells[7],
            "status": cells[8],
        })
    return rows


def targets_from(date_from, date_to, procedure=TENDERS, kind="tender",
                 max_pages=60, session=None):
    """Every resource EPPS published in the window, newest first.

    The window is inclusive on both sides and is the PUBLICATION date, which is the honest
    question for a nightly run: the portal's "newest" list carries only what is still open,
    so a notice published yesterday and withdrawn today is absent from it. Asked by window,
    20 August returns 120 records where the open list showed 92.
    """
    session = session or Session()
    criteria = {}
    if date_from:
        criteria["publicationFromDate"] = date_from
    if date_to:
        criteria["publicationUntilDate"] = date_to
    if procedure:
        criteria["procedure"] = procedure

    out, seen = [], set()
    for number in range(1, max_pages + 1):
        rows = parse_rows(session.page(criteria, number), kind)
        fresh = [r for r in rows if r["pid"] not in seen]
        if not rows:
            break
        for row in fresh:
            seen.add(row["pid"])
            out.append(row)
        if not fresh:                     # the pager ran past the end and repeated itself
            break
        time.sleep(0.15)
    return out


def day(date_from, date_to):
    """Both populations for one window, as one deduplicated list.

    ONE PASS, not two. The window search is not the portal's "newest procurements" list and
    does not behave like it: consultations come back in it already, labelled in the
    `Pirkimo būdas` column. Asking a second time with `procedure=…pmc` therefore returns the
    same records again, and a run that concatenated the two would fetch every consultation
    twice and count the day wrong. The population a record belongs to is read off the row.
    """
    found = targets_from(date_from, date_to, TENDERS, "tender")
    for row in found:
        if (row.get("procedure") or "").strip().casefold() == "rinkos konsultacija":
            row["kind"] = "consultation"
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description="What EPPS published in a window.")
    ap.add_argument("date_from", help="dd/mm/yyyy or yyyy-mm-dd")
    ap.add_argument("date_to", nargs="?")
    ap.add_argument("--consultations", action="store_true",
                    help="market consultations only")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    def lt(value):
        if value and re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            y, m, d = value.split("-")
            return "%s/%s/%s" % (d, m, y)
        return value

    a, b = lt(args.date_from), lt(args.date_to or args.date_from)
    rows = (targets_from(a, b, CONSULTATIONS, "consultation") if args.consultations
            else day(a, b))
    rows.sort(key=lambda r: (r["published"] or ""), reverse=True)
    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return rows
    by_kind = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    print("%s .. %s: %d record(s) %s" % (a, b, len(rows), by_kind))
    for r in rows[:15]:
        print("  %s  %-12s %s" % (r["pid"], r["kind"], (r["title"] or "")[:64]))
    return rows


if __name__ == "__main__":
    main()
