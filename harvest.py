"""Notices from the Latvian procurement register (IUB) and the EIS page each one points at.

    fetch_window(date_from, date_to)  every notice published in the window, with a coverage
                                      check against the register's own total
    resolve(uuid)                     the EIS procurement URL of a notice, or None
"""

import re
import shutil
import tempfile
import time
import urllib.parse

import eis_fetch
import eis_page
import net

API = "https://infob.iub.gov.lv/api/search"
NOTICE_URL = "https://eformsb.pvs.iub.gov.lv/show/{uuid}"
UA = "eis-tool/1.0 (+public procurement monitoring)"
BIDDABLE = {"competition", "planning"}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

PAGE_LIMIT = 200
MAX_PAGES = 200
RETRIES = 5


def _get(url, tries=RETRIES):
    """GET with the shared retry policy. Returns (parsed_json, headers)."""
    return net.get_json(url, headers={"User-Agent": UA, "Accept": "application/json"},
                        timeout=60, tries=tries)


def fetch_window(date_from, date_to):
    """Every notice published in [date_from, date_to]. Returns (records, coverage)."""
    base = {
        "publishedFrom": date_from.strftime("%d/%m/%Y"),
        "publishedTo": date_to.strftime("%d/%m/%Y"),
        "limit": PAGE_LIMIT,
        "withInflections": "true",
        "searchPhrase": "true",
    }

    seen, records, total, page = set(), [], None, 1
    page_size = PAGE_LIMIT
    while page <= MAX_PAGES:
        params = dict(base, page=page)
        data, headers = _get(API + "?" + urllib.parse.urlencode(params))

        if total is None:
            raw_total = headers.get("x-total-count") or headers.get("X-Total-Count")
            if raw_total is None:
                raise RuntimeError("the register answered without x-total-count")
            total = int(raw_total)
            page_size = int(headers.get("x-limit") or headers.get("X-Limit") or len(data) or PAGE_LIMIT)

        if not data:
            break
        for rec in data:
            key = rec.get("identifier") or rec.get("externalId")
            if key not in seen:
                seen.add(key)
                records.append(rec)
        if len(data) < page_size or len(records) >= total:
            break
        page += 1
        time.sleep(0.25)

    coverage = {
        "expected": total,
        "unique": len(records),
        "pages": page,
        "drift": len(records) - (total or 0),
        "proven": total is not None and len(records) >= total,
    }
    return records, coverage


def normalise(rec):
    """A register record in the shape the rest of the tool uses."""
    uuid = rec.get("identifier") or ""
    value = rec.get("amount")
    try:
        value = float(value) if value not in (None, "", "0") else None
    except (TypeError, ValueError):
        value = None

    return {
        "id": str(rec.get("externalId") or ""),
        "uuid": uuid,
        "ref": rec.get("procurementIdentifier") or "",
        "title": (rec.get("name") or "").strip(),
        "buyer": (rec.get("organizationName") or "").strip(),
        "buyer_reg": rec.get("organizationIdentifier") or "",
        "published": (rec.get("publicationDate") or "")[:10],
        "deadline": rec.get("date"),
        "value": value,
        "currency": rec.get("currency") or "EUR",
        "cpv": [{"code": c.get("code"), "caption": c.get("caption")} for c in (rec.get("cpvCodes") or [])],
        "notice_type": rec.get("type"),
        "procedure": rec.get("procedureType"),
        "legal_basis": rec.get("procedureLegalBasis"),
        "eu_fund": bool(rec.get("euFund")),
        "link": NOTICE_URL.format(uuid=uuid),
        "source": "IUB",
    }


def resolve(notice, workdir=None):
    """The EIS procurement URL an IUB notice points at, or None when it names none.

    Raises `eis_fetch.Fail` when the register cannot be reached, so that an unreachable
    notice is never mistaken for one conducted outside EIS.
    """
    url = NOTICE_URL.format(uuid=notice) if UUID.match(notice.strip()) else notice
    folder = workdir or tempfile.mkdtemp(prefix="iub_")
    try:
        html = eis_fetch.Curl(folder, url).get_text(url)
    finally:
        if workdir is None:
            shutil.rmtree(folder, ignore_errors=True)
    pid = eis_page.resolve_eis_id(html)
    return eis_page.PAGE % pid if pid else None
