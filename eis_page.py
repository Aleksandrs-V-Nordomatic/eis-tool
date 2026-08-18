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


# A THIRD ANSWER, NOT A SLOWER PATH TO ONE OF THE OTHER TWO.
#
# `is_published` distinguishes "this is the tender" from "this is not, yet" — and the caller
# reasonably retries the second, because a draft becomes visible and a throttle passes. EIS
# answers some ids with neither: a fixed page at its own path, `/EKEIS/Error.html`, saying
# the procurement has no stage a guest may see at all. That is not a draft warming up and
# not a throttle cooling down — the message is the platform's own explanation, chosen and
# served instantly, not a timeout. Six more attempts spend five minutes proving what the
# first answer already said.
#
# The heading is bilingual on one line, `Pieeja liegta / Access Denied`, which is EIS's own
# fixed template for this state rather than a phrase this project chose — the same
# language-proof reasoning `is_published` already relies on. Measured on EIS 179817 and
# 179872, 2026-08-18: both notices, both answering instantly with this page on every draw,
# never once returning a procurement.
ACCESS_DENIED_MARKERS = ("Pieeja liegta / Access Denied",)


def is_access_denied(page):
    """EIS's own 'no displayable stage' page — permanent, and not this tool's failure."""
    return bool(page) and any(mark in page for mark in ACCESS_DENIED_MARKERS)

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
    # Four spellings, all read off live pages: the portal prints a combined caption on some
    # procurements and a bare one on others. `field` walks every label given, so extra
    # spellings cost a tuple entry — and a missing one costs a date whenever the id moves.
    "published":   ("FirstAnnouncementDate", "Izsludināts", "Announced",
                    "Izsludināts / publicēts", "Izsludināšanas / publicēšanas datums",
                    "Announced / Published", "Announcement / Publication date"),
    "procedure":   ("ProcurementRegulationId", "Procedūras tips", "Procedure / procurement"),
    "profile":     ("ProfileId", "Iepirkuma profils", "Procurement profile"),
    "legal_basis": ("RegulatoryActId", "Procedūras juridiskais pamats", "Regulatory LA"),
    "work_kind":   ("SubjectDescription_ProcurementObjectTypeId", "Pamatveids", "Object type"),
    "deadline":    ("stage_ProposalSubmissionDate",
                    "Pieteikumu/piedāvājumu iesniegšanas termiņš",
                    "Applications / proposals submission deadline"),
    "opening":     ("stage_ProposalOpeningDate", "Pieteikumu/piedāvājumu atvēršanas laiks",
                    "Applications / proposals opening"),
    "docs_until":  ("stage_DocumentationAvailabilityDate",
                    "Dokumentācijas izsniegšanas termiņš",
                    "Documentation issuance deadline"),
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
    # The page renders the additional CPV codes as a repeater widget, so this "field" comes
    # back as several kilobytes of JavaScript rather than a value. The codes are inside it.
    "_cpv_extra":  ("SubjectDescription_CpvAdditionalIdList", "CPV papildkods",
                    "CPV additional code"),
    "_value":      ("SubjectDescription_EstimatedContractValue", "Paredzamā vērtība",
                    "Expected contract price"),
}


_CPV_ITEMS = re.compile(r"CpvAdditionals_items\s*=\s*")


def cpv_additional(fields):
    """Every CPV code beyond the main one, as `code caption`, in page order.

    A procurement is classified by its CPV codes, and the main one is not all of them:
    measured over 169 collected pages, 70 carry at least one more — road works under a
    street-rebuild, design services under construction, laboratory reagents under a
    supplies notice. They were parsed and thrown away, because the page ships them as a
    repeater widget and the field the parser grabbed is the widget's JavaScript.

    The codes sit in `CpvAdditionals_items`, a JSON array, each entry's `Title` being the
    code and its caption. `raw_decode` is used rather than a bracket regex: a caption may
    contain a bracket, and a regex that stops at the first one truncates the list silently,
    which is the failure this whole file keeps refusing.
    """
    slab = field(fields, "_cpv_extra")
    if not slab:
        return []
    found = _CPV_ITEMS.search(str(slab))
    if not found:
        return []
    try:
        items, _end = json.JSONDecoder().raw_decode(str(slab)[found.end():])
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        title = (item or {}).get("Title") if isinstance(item, dict) else None
        title = _text(title) if title else ""
        if title and title not in out:
            out.append(title)
    return out


def field(fields, key):
    """One field, by platform id first and visible label second. None when truly absent."""
    fid, *labels = FIELDS[key]
    if fid and fields.get("#" + fid) is not None:
        return fields["#" + fid]
    for label in labels:
        if label and fields.get(label) is not None:
            return fields[label]
    return None


def parse_notice(page, pid, register_uuid=None):
    """Everything the public page carries about one procurement. None if it is not published.

    `register_uuid` is what the CALLER already knows: the register notice this page was
    reached from. Pass it whenever the target came out of `discover`, because register
    membership is not a fact this page reliably carries — see `register_check` below — and
    the caller's knowledge beats a guess made from HTML every time.
    """
    if not is_published(page):
        return None

    fields = parse_fields(page)
    iub = _IUB.search(page)
    planning = _IUB_PLANNING.search(page)
    # The registration number is appended to the buyer in Latvian even on the English page.
    buyer = field(fields, "_buyer") or ""
    buyer_name, _, buyer_reg = buyer.partition(", reģ. numurs:")

    # The additional CPV codes, extracted BEFORE the widget that carried them is cut out of
    # `fields`. The page renders them as a repeater, so the raw "value" under those keys is
    # kilobytes of the widget's JavaScript — 65% of every procurement.json, measured over
    # 169 collected pages, and shipped three times per tender: folder, archive, shards.zip.
    # While the script was the only carrier of the codes it had to stay. Now that they are
    # first-class it carries nothing, so what stays under the keys is what the page shows a
    # person — the codes themselves. `fields` still records that the field exists.
    cpv_extra = cpv_additional(fields)
    _fid, *_cpv_labels = FIELDS["_cpv_extra"]
    for k in ["#" + _fid] + [l for l in _cpv_labels if l]:
        if k in fields:
            fields[k] = "; ".join(cpv_extra)

    notice = {
        "eis_id": str(pid),
        "value": parse_money(field(fields, "_value")),
        "currency": "EUR",
        "buyer": buyer_name.strip() or None,
        "buyer_reg": buyer_reg.strip() or None,
        "iub_uuid": register_uuid or (iub.group(1) if iub else None),
        # HOW REGISTER MEMBERSHIP WAS ESTABLISHED, AND WHETHER IT WAS ESTABLISHED AT ALL.
        #
        #   "discovery"  the caller found this procurement BY searching the register, so it
        #                is in the register and there is nothing to infer
        #   "page-link"  the page printed a register link — a notice or a planning
        #                publication — and that link is the evidence
        #   "unverified" the page printed neither, and nobody asked the register
        #
        # This field exists because the page cannot answer the question and used to be made
        # to. Measured over 169 collected pages, 43 carried no register link; six were then
        # fetched live and five had no iub.gov.lv URL of any shape anywhere in the HTML —
        # on procurements the register demonstrably carries, since discovery is what found
        # them. So a missing link is ordinary, and reading it as absence from the register
        # was wrong in every one of those cases.
        "register_check": ("discovery" if register_uuid
                           else "page-link" if (iub or planning)
                           else "unverified"),
        # SO THIS MEANS "NOT CONFIRMED TO BE IN THE REGISTER", NOT "ABSENT FROM IT" — and
        # the difference has now been measured rather than argued. Walking ids 179550-179800
        # and putting every page to the register itself, by identification number and by
        # buyer:
        #
        #   129 published of 251 ids walked   the id space is about half live
        #    66 page-link    ->  66 in the register,  0 not. The link never lied.
        #    63 unverified   ->  38 in the register after all, 25 genuinely absent
        #
        # A fifth of what EIS publishes has no register notice — and the page alone would
        # have accused twice that many. What the 25 are: 12 `Neregulēts iepirkums`, outside
        # the publication duty, which is the premise this flag used to assert without
        # evidence; 7 market consultations; 4 closed competitions under a dynamic purchasing
        # system whose parent was published once; 2 carrying 2025 references, whose register
        # entry simply predates the window checked.
        #
        # Only 5 of the 25 publish a value: 9,999 / 15,000 / 23,500 / 30,000 / 90,909 EUR.
        # Small, as "below the publication duty" would predict — the old premise turns out
        # to hold for the population it was actually about, and not for the one this flag
        # was selecting.
        "eis_only": register_uuid is None and iub is None and planning is None,
        "link": PAGE % pid,
        "source": "EIS",
        # Beside `cpv_main`, never folded into it: the main code is what the buyer filed the
        # procurement under, and the rest are what else it touches. A consumer deciding by
        # code needs to be able to tell which is which.
        "cpv_additional": cpv_extra,
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
