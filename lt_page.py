"""The Lithuanian source: one procurement on EPPS, read off its public pages.

`eis_page` does this for Latvia. This is the same two questions asked of a different
portal, and the answers are shaped identically on purpose — `parse_notice` and
`parse_documents` return what their Latvian namesakes return, so everything downstream
(normalisation, digests, index, change detection, delivery) stays country-agnostic.

WHAT IS ACTUALLY DIFFERENT, and it is more than a base URL. Latvia's EIS is a bespoke
ASP.NET application; Lithuania's CVP IS is European Dynamics EPPS, a Java application whose
pages are label/value pairs rather than embedded JavaScript arrays. So there is no
`embedded_array` here and no field ids to prefer over labels — the label IS the identifier,
which makes the language the page was served in load-bearing. EPPS answers in Lithuanian or
English and the caller does not choose, exactly as EIS does, so every label below is given
in both and `SKELBIMO KALBA` is carried through so a reader can tell which one arrived.

THREE THINGS EPPS GIVES THAT EIS DOES NOT, and they are why this file is short:

  * the whole tender downloads as one archive from a single address, built by the portal
    rather than by us — `downloadCftResourceItems.do?resourceId=…&resourceType=…`;
  * every document row carries `Papildymo ID`, the number of the amendment that placed it
    there. A buyer replacing a specification says so in the catalogue, so a change is read
    rather than inferred — the sha256 comparison stays the floor, but the amendment number
    is what lets an update say WHICH document moved and under which amendment;
  * market consultations (`rinkos konsultacija`) are ordinary resources here, served from
    `pmc/viewPmc.do` and carrying documents through the same two endpoints. A consultation
    publishes the DRAFT technical specification, months before the tender exists.

ONE TRAP, and it is silent. EPPS returns its login form under HTTP 200 for a resourceId
that does not exist or is not public. A status code is therefore not a validity signal and
`is_published` below reads the body, exactly as its Latvian namesake has to.

    python3 lt_page.py <resourceId>        # the notice and its documents, as JSON
"""
import json
import re
import sys

import net

BASE = "https://viesiejipirkimai.lt/epps"
PAGE = BASE + "/cft/prepareViewCfTWS.do?resourceId=%s"
PMC = BASE + "/pmc/viewPmc.do?resourceId=%s"
# A standing system — a DPS, or a utilities KVS — is linked from the results table to its
# own view, and THAT VIEW IS NOT SERVED: it answers 500 anonymously and 500 inside the
# session that ran the search, tried on several ids. Recorded rather than removed,
# because the next reader will find the link in the table and try exactly this.
#
# So a door has no readable card, and therefore no CPV. What it does have is documents:
# `listContractDocuments` and the archive answer for a door id exactly as they do for a
# tender. Its facts come from the results row instead, which carries title, buyer,
# publication, procedure and status — everything but the codes.
DOOR = BASE + "/cft/prepareViewCfTDPSWS.do?resourceId=%s"
DOCS = BASE + "/cft/listContractDocuments.do?resourceId=%s"
# The whole tender in one request. `resourceType` is the portal's own word for the
# catalogue being asked for, and ContractDocument is the published one.
ARCHIVE = BASE + "/cft/downloadCftResourceItems.do?resourceId=%s&resourceType=ContractDocument"
# A single document is a two-step: this address answers with an interstitial page, not the
# file. It is recorded because the card shows it to a person, who gets the document; a
# fetcher should take the archive instead.
DOWNLOAD = BASE + "/cft/prepareAnonymousDownload.do?resourceId=%s&documentId=%s"

UA = "Mozilla/5.0 (compatible; eis-tool)"

# Every field this file promises, Lithuanian label first, English second. There are no field
# ids to prefer: EPPS renders a definition list, so the label is the only handle there is.
FIELDS = {
    "title":          ("PAVADINIMAS", "TITLE"),
    "buyer":          ("PIRKIMO VYKDYTOJO PAVADINIMAS", "CONTRACTING AUTHORITY NAME"),
    "description":    ("APRAŠYMAS", "DESCRIPTION"),
    "procedure":      ("PIRKIMO BŪDAS", "PROCEDURE TYPE"),
    "work_kind":      ("PIRKIMO OBJEKTO TIPAS", "CONTRACT TYPE"),
    "legal_basis":    ("DIREKTYVA", "DIRECTIVE"),
    "cpv_main":       ("BVPŽ KODAI", "CPV CODES"),
    "value":          ("NUMATOMA VERTĖ (EUR)", "ESTIMATED VALUE (EUR)"),
    # Two spellings, because a consultation is served from a different view with a shorter
    # label for the same thing. The long one first: on a tender page BOTH appear, and the
    # short one there belongs to a lot rather than to the procurement.
    "deadline":       ("PASIŪLYMŲ ARBA PARAIŠKŲ DALYVAUTI PIRKIME PATEIKIMO TERMINAS",
                       "TIME LIMIT FOR RECEIPT OF TENDERS OR REQUESTS TO PARTICIPATE",
                       "PASIŪLYMŲ PATEIKIMO TERMINAS", "SUBMISSION DEADLINE"),
    "status":         ("STATUSAS", "STATUS"),
    "docs_until":     ("PRAŠYMŲ PATEIKTI PAAIŠKINIMUS TERMINO PABAIGA",
                       "TIME LIMIT FOR REQUESTS FOR CLARIFICATION"),
    "opening":        ("SUSIPAŽINIMO SU PASIŪLYMAIS DATA", "TENDER OPENING DATE"),
    "published":      ("PASKELBIMO IR (ARBA) KVIETIMO DATA", "PUBLICATION AND/OR INVITATION DATE"),
    "threshold":      ("VIRŠ ARBA ŽEMIAU TARPTAUTINIO PIRKIMO VERTĖS RIBOS",
                       "ABOVE OR BELOW THE INTERNATIONAL THRESHOLD"),
    "lots":           ("PIRKIMAS SKAIDOMAS Į DALIS(-IŲ) (DPS - Į KATEGORIJAS)",
                       "CONTRACT DIVIDED INTO LOTS"),
    "contract_duration": ("SUTARTIES TRUKMĖ MĖNESIAIS ARBA METAIS, IŠSKYRUS PRATĘSIMUS",
                          "DURATION OF THE CONTRACT"),
    "award_criteria": ("PASIŪLYMŲ VERTINIMO KRITERIJAI (PASIRINKTAS VERTINIMO KRITERIJUS "
                       "BUS TAIKOMAS VISOMS PIRKIMO DALIMS/KATEGORIJOMS)",
                       "AWARD CRITERIA"),
    "language":       ("SKELBIMO KALBA", "NOTICE LANGUAGE"),
    # Lithuania-only, and worth carrying: the buyer's annual procurement plan this purchase
    # was announced under. It is the thread back to the plan register, where the same object
    # appeared months earlier as a line in a spreadsheet.
    "plan_ref":       ("PIRKIMŲ SUVESTINĖS NUORODA", "PROCUREMENT PLAN REFERENCE"),
}

_LOGIN = re.compile(r"(?:Prisijungimo vardas|/epps/authenticate/login\b.*?name=\"password\")",
                    re.S)
_TAG = re.compile(r"(?is)<script.*?</script>|<style.*?</style>")


def fetch(url):
    """One EPPS page, under the shared retry policy.

    It had no retry at all, which is the same defect the Latvian lane was carrying in a
    more elaborate disguise: there, a retry loop was written against the wrong exception
    types; here, there was nothing to get wrong because there was nothing. A single reset
    from EPPS ended the night either way.
    """
    return net.get_text(url, headers={"User-Agent": UA}, timeout=90,
                        log=lambda line: print(line, file=sys.stderr))


def is_published(page):
    """False for a resourceId EPPS will not show anonymously.

    The portal answers 200 with its login form in that case, so this reads the body. A page
    that is genuinely a procurement always carries the view's own heading.
    """
    headings = ("prepareViewCfTWS", "prepareViewCfTDPSWS", "Peržiūrėti pirkimo",
                "View contract", "Peržiūrėti rinkos konsultaciją",
                "Peržiūrėti dinaminę", "Peržiūrėti kvalifikacijos")
    if not any(heading in page for heading in headings):
        return False
    return not bool(_LOGIN.search(page))


def _lines(page):
    text = re.sub(r"<[^>]+>", "\n", _TAG.sub(" ", page))
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&#034;", '"').replace("&quot;", '"'))
    return [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n") if ln.strip()]


def parse_fields(page):
    """Label -> value, over a page rendered as a definition list.

    EPPS prints the label on its own line, ending in a colon, and the value on the next
    non-empty one. A field the buyer left empty is followed straight by the next label, so
    the value is recorded as None rather than as the label after it — which is the mistake
    that silently gives one tender another tender's deadline.
    """
    lines = _lines(page)
    labels = {_key(lbl) for pair in FIELDS.values() for lbl in pair}
    fields = {}
    for i, line in enumerate(lines):
        if not line.rstrip().endswith(":"):
            continue
        key = _key(line)
        if key not in labels:
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        empty = not nxt or nxt.rstrip().endswith(":") or _key(nxt) in labels
        fields[key] = None if empty else nxt
    return fields


def _key(label):
    """One spelling for a label however EPPS punctuated it.

    Some labels are printed `TERMINAS :` and some `TERMINAS:`. Matching on the raw string
    loses the deadline on exactly the tenders whose label carries the space — which is the
    field it can least afford to lose, and it fails silently.
    """
    return re.sub(r"\s*:\s*$", "", label).strip().upper()


def field(fields, name):
    for label in FIELDS[name]:
        value = fields.get(_key(label))
        if value:
            return value
    return None


def parse_money(raw):
    """`16528,93.` and `0.0` both occur. Returns a float, or None when nothing was published."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d,.\-]", "", raw).rstrip(".")
    if not cleaned:
        return None
    # A comma is the decimal mark here; a dot may be either, so the last separator wins.
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "") if cleaned.rfind(",") > cleaned.rfind(".") \
            else cleaned.replace(",", "")
    cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value or None


def parse_cpv(raw):
    """`45312100-Gaisrinės signalizacijos sistemų montavimo darbai` — code, hyphen, words.

    Note what is NOT here: the check digit. EPPS prints the eight digits and then the label,
    so a reader expecting `45312100-1` finds nothing and reports a tender with no CPV.
    """
    return sorted(set(re.findall(r"\b(\d{8})\b", raw or "")))


def parse_documents(page, resource_id):
    """The catalogue, in page order, each row carrying the amendment that placed it.

    `Papildymo ID` is `N/A` on a document published with the tender and a number on one
    added later. That number is the portal saying which amendment a document belongs to —
    the thing Latvia has to work out by comparing bytes.
    """
    docs = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page):
        ids = re.findall(r"downloadDocForAnonymous\('(\d+)'\)", row)
        if not ids:
            continue
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)
                        .replace("&nbsp;", " ").replace("&#034;", '"')).strip()
                 for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
        if len(cells) < 4:
            continue
        amendment = cells[0] if cells[0] and cells[0] != "N/A" else None
        docs.append({
            "doc_id": ids[0],
            "title": cells[1],
            # The file as the buyer named it. `title` is the catalogue entry, and the two
            # differ often enough that quoting the wrong one names a document nobody can find.
            "filename": cells[2] or cells[1],
            "note": cells[3] or None,
            "language": cells[4] if len(cells) > 4 else None,
            "amendment": amendment,
            "section": "current",
            "download": DOWNLOAD % (resource_id, ids[0]),
        })
    return docs


def parse_notice(page, pid, kind="tender"):
    """Everything the public page carries about one procurement. None if it is not published.

    `kind` is the population this resource belongs to — `tender` or `consultation` — because
    Lithuania publishes both through the same machinery and a consultation is not something
    anybody can bid on. The caller knows which list it came from; the page does not say.
    """
    if not is_published(page):
        return None
    fields = parse_fields(page)
    cpv = parse_cpv(field(fields, "cpv_main"))
    return {
        "eis_id": str(pid),           # the contract's name for it, kept so readers do not fork
        "source": "EPPS",
        "country": "LT",
        "kind": kind,
        "link": view_for(kind) % pid,
        "archive": ARCHIVE % pid,
        "title": field(fields, "title"),
        "buyer": field(fields, "buyer"),
        "buyer_reg": None,            # EPPS does not print it on this page
        "value": parse_money(field(fields, "value")),
        "currency": "EUR",
        "deadline": field(fields, "deadline"),
        "opening": field(fields, "opening"),
        "docs_until": field(fields, "docs_until"),
        "published": field(fields, "published"),
        "status": field(fields, "status"),
        "procedure": field(fields, "procedure") or KIND_PROCEDURE.get(kind),
        "work_kind": field(fields, "work_kind"),
        "legal_basis": field(fields, "legal_basis"),
        "threshold": field(fields, "threshold"),
        "lots": field(fields, "lots"),
        "contract_duration": field(fields, "contract_duration"),
        "award_criteria": field(fields, "award_criteria"),
        # Which language EPPS happened to answer in. Ten fields moving at once is that flag
        # flipping, not the buyer amending anything, and a reader needs to be able to tell.
        "page_language": field(fields, "language"),
        "plan_ref": field(fields, "plan_ref"),
        "cpv_main": cpv[0] if cpv else None,
        "cpv_additional": cpv[1:],
        # `batch.cpv_codes` looks under `cpv` for the whole set. Named here so the gate that
        # already works for Latvia works unchanged, rather than growing a country branch.
        "cpv": cpv,
        "fields": fields,
    }


# What the portal calls a resource that is not an ordinary competition. The consultation view
# does not print `Pirkimo būdas` at all — it is a different view of a different thing — so the
# field comes back empty and the card's `Iepirkuma veids` with it. That column is also what says
# which of the three kinds a card is, so an empty one loses the distinction entirely. This is
# derived, not guessed: a resource served from the PMC view IS a market consultation, and that is
# the portal's own name for it.
KIND_PROCEDURE = {"consultation": "Rinkos konsultacija"}


def view_for(kind):
    """Which of the three views carries this kind of resource."""
    return {"consultation": PMC, "door": DOOR}.get(kind, PAGE)


def notice_only(pid, kind="tender"):
    """The card, without the document catalogue — one ~30 KB page.

    THE GATE HAS TO FIRE BEFORE A BYTE MOVES, and an archive is megabytes. So a run reads
    this first, decides, and only then asks for the tender. Fetching and then discarding
    would cost the portal and us the whole day's bytes to learn nothing.
    """
    return parse_notice(fetch(view_for(kind) % pid), pid, kind)


def collect(pid, kind="tender"):
    """The notice and its catalogue, from the two pages that carry them."""
    notice = notice_only(pid, kind)
    if notice is None:
        return None
    notice["documents"] = parse_documents(fetch(DOCS % pid), pid)
    return notice


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        raise SystemExit("lt_page: name a resourceId — python3 lt_page.py <id> [consultation]")
    kind = "consultation" if len(argv) > 1 and argv[1].startswith("cons") else "tender"
    out = collect(argv[0], kind)
    if out is None:
        raise SystemExit("lt_page: %s is not published, or EPPS answered with its login form"
                         % argv[0])
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
