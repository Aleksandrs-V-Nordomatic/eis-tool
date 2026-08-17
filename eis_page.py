#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
What the procurement page says about the procurement — title, buyer, deadline, value, CPV.

The downloader already fetches this page; it once read the anti-forgery token and the
document arrays out of it and threw the rest away. That was a real loss: a procurement
below the publication duty carries no register notice, and for those this page is the only
place the metadata exists at all. The register cannot supply what it never received.

    from eis_page import parse_notice, resolve_eis_id
    notice = parse_notice(html, "178345")       # no network — html comes from the caller
    pid    = resolve_eis_id(iub_notice_html)    # IUB's API never returns the EIS URL

EVERY PARSER HERE IS A PURE FUNCTION. The transport lives in eis_fetch.py and is passed in
where it is needed, so the parsing can be tested against saved HTML without a runner, an
address draw, or a live portal — which is also the only way these rules can be regression-
tested at all, given that a third of runners cannot reach EIS.

THE CONTACT PERSON NEVER ENTERS THE PARSER. Unlike the IUB search API, this page names a
human being with a phone number and an e-mail. Any label matching `_PERSONAL` is dropped
before the value is even read, so the no-personal-data property is held by construction
rather than by a downstream filter someone can forget. This is about a named individual's
data, not about the documents: those are published by the state to anonymous visitors and
are copied verbatim.
"""

import html as _html
import json
import re

BASE = "https://www.eis.gov.lv"
PAGE = BASE + "/EKEIS/Supplier/Procurement/%s"
IUB_NOTICE = "https://eformsb.pvs.iub.gov.lv/show/%s"

# WHAT PROVES A PAGE IS A PUBLISHED PROCUREMENT — and what merely used to.
#
# The old rule was the Latvian heading `Iepirkuma pamatdati`. It is wrong twice over:
#
#   * EIS serves some real, complete tender pages IN ENGLISH, carrying the token and the
#     document arrays while the Latvian heading is simply absent. `Accept-Language: lv`
#     does not change it — the portal ignores the header. Under the old rule such a tender
#     reads as unpublished.
#   * An id that really is not published answers with a small redirect stub, which looks
#     nothing like a full page missing one heading.
#
# It also matters beyond a parser detail: a large page without the Latvian heading was once
# recorded as evidence of throttling, when it was the English rendering of the tender. That
# difference decides whether the address or the portal gets blamed.
#
# So publication is proved structurally, in a way no translation can move: the page must
# carry the anti-forgery token and the document-array declarations the platform emits on
# every procurement page. The localized headings are kept as an additional accepted signal,
# never as the only one.
PUBLISHED_MARKERS = ("Iepirkuma pamatdati", "Basic data")
_STRUCTURAL = ("__RequestVerificationToken", "ActualDocuments_items")


def is_published(page):
    """Is this HTML a published procurement page, in any language EIS chooses to serve?"""
    if not page:
        return False
    if all(mark in page for mark in _STRUCTURAL):
        return True
    return any(mark in page for mark in PUBLISHED_MARKERS)

# Attribute-aware on purpose. The obvious `<label[^>]*for="([^"]*)"[^>]*>` breaks the moment
# an attribute value contains a `>` — a tooltip written as `title="<div>help</div>"` ends the
# tag early, and the "label" then captured is the help text. That failure is silent: the field
# lookup below simply misses, and a tender arrives with no deadline rather than with an error.
_LABEL = re.compile(r'<label\b((?:[^>"]|"[^"]*")*)>(.*?)</label>', re.S)
_FOR = re.compile(r'\bfor="([^"]*)"')
# The value is the whole field-block, not just its field-text span: some values sit in a
# bare text node beside the span (`<div class="field-block">63100.00 <span>EUR</span>`).
_FIELD = re.compile(r'class="[^"]*field-block[^"]*"[^>]*>(.*?)</div>', re.S)
_MONEY = re.compile(r"(\d[\d\s]*[.,]?\d*)\s*(EUR|USD)?")
_PERSONAL = re.compile(r"kontakt|e-?past|t[āa]lru|person", re.I)
_TAG = re.compile(r"<[^>]+>")
# The register publishes two shapes, under two hosts, and only one of them is a notice
# with a uuid. A market consultation is a planning publication:
#
#   competition   https://eformsb.pvs.iub.gov.lv/show/<uuid>
#   planning      https://eforms.pvs.iub.gov.lv/planning-publications/view/<kind>/<id>
#
# Matching only the first read a printed planning link as no link at all. Note the hosts
# differ by one letter — `eformsb` against `eforms` — which is why this is two patterns
# and not one with an alternation in the path.
_IUB = re.compile(r"https://eformsb\.pvs\.iub\.gov\.lv/show/([0-9a-f\-]{36})")
_IUB_PLANNING = re.compile(
    r"https://eforms\.pvs\.iub\.gov\.lv/planning-publications/view/[a-z0-9\-]+/(\d+)")
# The EIS link as it appears on an IUB notice page. IUB's own search API does not carry it,
# so the only route from a notice to its documents runs through the notice HTML.
_EIS_LINK = re.compile(r"eis\.gov\.lv/EKEIS/Supplier/Procurement/(\d+)")


def _text(fragment):
    """Visible text of an HTML fragment, tooltips and markup removed."""
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub(" ", fragment or ""))).strip()


def _label_text(fragment):
    """The label's own words, stopping before its help tooltip.

    A label often carries a tooltip as a child element, and tag-stripping the whole thing
    bleeds the help text into the label. The label's own words are always the leading text
    node, so cut at the first tag and keep what came before it.

    Entities are unescaped after the cut, because a page is free to write `Organiz&#257;cijas`
    instead of `Organizācijas` and the lookup keys below are spelled in Latvian letters. That
    mismatch, too, fails silently — the field is simply never found.
    """
    lead = (fragment or "").split("<")[0]
    return re.sub(r"\s+", " ", _html.unescape(lead)).strip().rstrip(":").strip()


class MalformedArray(ValueError):
    """A payload the page declares but this reader cannot parse."""


def embedded_array(page, name, strict=False):
    """The JS array assigned to `name`, parsed properly.

    `raw_decode` rather than a regex: these payloads carry nested objects, escaped quotes
    and Latvian text, and a non-greedy `\\[.*?\\]` truncates at the first inner bracket —
    which is exactly how an earlier reader silently lost document rows. The decoder
    consumes one complete JSON value and reports where it ended.

    `strict` picks which failure is worse, and both answers are right somewhere. A DOWNLOAD
    must stop: an unparsable document array means the tender may have documents we never
    saw, and a pack that looks complete and is not is the failure this project refuses. A
    WALK must not: one broken page among hundreds is not a reason to abandon discovery.
    """
    marker = re.search(r"\b%s\s*=\s*" % re.escape(name), page)
    if not marker:
        return []
    try:
        value, _ = json.JSONDecoder().raw_decode(page[marker.end():])
    except ValueError as exc:
        if strict:
            raise MalformedArray("could not parse %s: %s" % (name, exc))
        return []
    return value if isinstance(value, list) else []


def parse_fields(page):
    """Every labelled field on the page, keyed by its Latvian label and by its ASP.NET id.

    Two keys for one value on purpose: the label is what a human recognises and what the
    field map below reads, the `#id` is what survives a Latvian wording change.
    """
    marks = []
    for m in _LABEL.finditer(page):
        attr = _FOR.search(m.group(1))
        marks.append((m.start(), attr.group(1) if attr else "", _label_text(m.group(2))))

    fields = {}
    for i, (pos, fid, label) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(page)
        if not label or _PERSONAL.search(label):
            continue                                  # the contact person stops here
        value = _FIELD.search(page, pos, end)
        if value:
            text = _text(value.group(1))
            fields.setdefault(label, text)
            fields.setdefault("#" + fid, text)
    return fields


def parse_money(raw):
    """`63100.00 EUR` → 63100.0. None when the buyer did not state a value."""
    money = _MONEY.match(raw or "")
    if not money or not money.group(1).strip():
        return None
    try:
        return float(money.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def parse_documents(page):
    """The document catalogue, both sections, in page order.

    Two sections exist and missing the second is easy: `Dokumenti (aktuālie)` and
    `Dokumenti (arhīvs)`. A superseded specification lives in the archive — and so does
    the ORIGINAL when a buyer re-uploads a corrected version, so reading only the current
    section silently loses both sides of a correction.
    """
    docs, seen = [], set()
    for section, var in (("current", "ActualDocuments_items"),
                         ("archive", "HistoricalDocuments_items")):
        for row in embedded_array(page, var):
            if row.get("Id") in seen:
                continue
            seen.add(row.get("Id"))
            title = row.get("DocumentTitle") or {}
            docs.append({
                "doc_id": row.get("Id"),
                "title": (row.get("Title") or title.get("Title") or "").strip(),
                "kind": row.get("TypeTitle"),
                "published": row.get("PublishDate"),
                "section": section,
                "link_type": row.get("DocumentLinkTypeCode") or "PRCDOC",
            })
    return docs


# Every field this tool promises, and the three ways to find it: the platform's own field
# id first, then the Latvian label, then the English one.
#
# THE ID COMES FIRST AND THAT IS THE WHOLE POINT. It is the same string whichever language
# EIS decides to serve, and it survives a rewording of the visible label. The labels stay as
# a fallback for the day the platform renames an id — losing a deadline silently is the
# failure this ordering exists to prevent. Both label spellings were read off live pages.
FIELDS = {
    "ref":         ("IdentificationNumber", "Iepirkuma identifikācijas numurs",
                    "Identification number"),
    "title":       ("Name", "Iepirkuma nosaukums", "Name"),
    "status":      ("StatusInfo", "Iepirkuma statuss", "Procurement status"),
    "published":   ("FirstAnnouncementDate", "Izsludināts", "Announced"),
    "procedure":   ("ProcurementRegulationId", "Procedūras tips", "Procedure / procurement"),
    "profile":     ("ProfileId", "Iepirkuma profils", "Procurement profile"),
    "legal_basis": ("RegulatoryActId", "Procedūras juridiskais pamats", "Regulatory LA"),
    "work_kind":   ("SubjectDescription_ProcurementObjectTypeId", "Pamatveids", "Object type"),
    "deadline":    ("stage_ProposalSubmissionDate",
                    "Pieteikumu/piedāvājumu iesniegšanas termiņš",
                    "Applications / proposals submission deadline"),
    "opening":     ("stage_ProposalOpeningDate", "Pieteikumu/piedāvājumu atvēršanas laiks",
                    "Applications / proposals opening"),
    # No English page in the sample carried this one, so its English label stays unknown
    # rather than guessed — a wrong label is indistinguishable from a silent page.
    "docs_until":  ("stage_DocumentationAvailabilityDate",
                    "Dokumentācijas izsniegšanas termiņš", None),
    # A market consultation ("Apspriede ar piegādātājiem") has no proposal deadline,
    # because no proposals are being taken yet — it has a consultation date instead, in a
    # different field. Such a procurement comes back with a null deadline and a real date
    # nobody would have seen. A date you must answer by is a date, whatever the procedure
    # is called, so it gets its own field rather than being folded into deadline.
    "consultation_until": ("stage_SuppliersGathering", "Apspriedes ar piegādātājiem termiņš",
                           "Deadline for consultation with suppliers"),
    "cpv_main":    ("SubjectDescription_CpvMainId", "CPV galvenais kods", "CPV main code"),
    "place":       ("SubjectDescription_ShipmentAddress", "Līguma izpildes vieta",
                    "Execution place"),
    "lots":        ("SubjectDescription_ProcurementHasParts", "Iepirkums ir sadalīts daļās",
                    "Procurement is split into lots"),
    "framework":   ("SubjectDescription_IsGeneralAgreement", "Paredzēta vispārīgā vienošanās",
                    "Framework agreement intended"),
    "contract_duration": ("SubjectDescription_ContractDuration", "Līguma darbības termiņš",
                          "Performance of the contract"),
    "award_criteria": ("ProposalSelectionMethodId", "Izvēles metode", "Award criteria"),
    "_buyer":      ("OrganizerId", "Organizācijas nosaukums", "Contracting authority"),
    "_value":      ("SubjectDescription_EstimatedContractValue", "Paredzamā vērtība",
                    "Expected contract price"),
}


def field(fields, key):
    """One field, by platform id first and visible label second. None when truly absent."""
    fid, *labels = FIELDS[key]
    if fid and fields.get("#" + fid) is not None:
        return fields["#" + fid]
    for label in labels:
        if label and fields.get(label) is not None:
            return fields[label]
    return None


def parse_notice(page, pid):
    """Everything the public page carries about one procurement. None if it is not published."""
    if not is_published(page):
        return None

    fields = parse_fields(page)
    iub = _IUB.search(page)
    planning = _IUB_PLANNING.search(page)
    # The registration number is appended to the buyer in Latvian even on the English page.
    buyer = field(fields, "_buyer") or ""
    buyer_name, _, buyer_reg = buyer.partition(", reģ. numurs:")

    notice = {
        "eis_id": str(pid),
        "value": parse_money(field(fields, "_value")),
        "currency": "EUR",
        "buyer": buyer_name.strip() or None,
        "buyer_reg": buyer_reg.strip() or None,
        "iub_uuid": iub.group(1) if iub else None,
        # WHAT THIS FLAG SAYS: this page printed no register hyperlink of either shape.
        #
        # WHAT IT DOES NOT SAY, THOUGH IT USED TO CLAIM IT: that the register lacks the
        # procurement, and therefore that it sits below the publication duty and is small.
        # It cannot say that. Discovery reaches this page BY searching the register
        # (`eis_tool.discover`), so everything the pipeline collects is in the register by
        # construction, and no flagged procurement is one the register misses.
        #
        # Measured over 169 collected pages: 43 come back flagged. Fetching a sample of
        # them live settles what the flag is reading — five of six carried NO iub.gov.lv
        # URL anywhere in the HTML, and the sixth carried a planning link. So for almost
        # all of them the page simply does not publish the connection, on a procurement the
        # register does carry. Widening the pattern cannot fix that: the information is not
        # on the page to be matched.
        #
        # Whether a procurement is genuinely EIS-only is therefore not answerable here, and
        # is not answered by this corpus either — one absent from the register is never
        # discovered, so none is present to look at. Enumerating those means walking EIS ids
        # (`walk_ids`, present and unused). A caller that already knows the notice it came
        # from should trust that over this flag.
        "eis_only": iub is None and planning is None,
        "link": PAGE % pid,
        "source": "EIS",
        "fields": fields,
    }
    # Everything else comes from the map above, so adding a field is one line there and
    # nothing here. Private keys (`_buyer`, `_value`) are inputs to the two composed
    # values and do not appear in the output.
    for key in FIELDS:
        if not key.startswith("_"):
            notice[key] = field(fields, key)
    return notice


def resolve_eis_id(notice_page):
    """The EIS procurement id an IUB notice page points at, or None.

    IUB's search API returns the notice, never the platform link, so this hop cannot be
    skipped: notice uuid → notice HTML → EIS id → documents.
    """
    found = _EIS_LINK.search(notice_page or "")
    return found.group(1) if found else None


def walk_ids(start_id, fetch_page, stop_after_misses=40, limit=None):
    """Walk procurement ids upward from `start_id`, yielding published notices.

    `fetch_page(pid)` returns page HTML or None; it is passed in so this can be walked over
    a fixture in tests and over the portal in production.

    EIS ids are assigned before publication, so the public space is sparse and gappy — an
    unpublished id today can publish tomorrow. The walk therefore does not stop at the first
    miss; it stops after a long run of consecutive misses, and the caller keeps the frontier
    so lower ids are re-probed on later runs.
    """
    pid, misses, seen = int(start_id), 0, 0
    while misses < stop_after_misses and (limit is None or seen < limit):
        notice = parse_notice(fetch_page(pid), pid)
        if notice is None:
            misses += 1
        else:
            misses = 0
            seen += 1
            yield notice
        pid += 1
