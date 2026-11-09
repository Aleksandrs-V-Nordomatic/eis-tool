#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Step 1 of 4 — DISCOVERY. Deterministic. No model, no browser, no credentials.

Pulls every procurement notice published in a window from the Latvian regulator's search
API and writes them as normalised JSONL, plus a meta file carrying the coverage proof.

    python harvest.py --days 3 --out ../work/raw/2026-08-04.jsonl

Fails loudly rather than shipping a partial window. Three refusals, each on purpose:
  * fetched rows must equal the API's own x-total-count, or the run stops;
  * an empty result with a non-empty window is a fail, not a quiet day;
  * notices older than the freshness ceiling mean the source regressed, and the run stops.

Standard library only, so this runs unchanged on a laptop, in a scheduled cloud session,
and in a container.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse

import net

API = "https://infob.iub.gov.lv/api/search"
NOTICE_URL = "https://eformsb.pvs.iub.gov.lv/show/{uuid}"
# A tool name and a purpose, and nothing about who is running it. A User-Agent is sent on
# every request, so anything put here is disclosed to the register itself — not merely to a
# reader of this repository. Naming the operator is the one thing this string must not do.
UA = "eis-tool/1.0 (+public procurement monitoring)"

# Notice types that can still be bid on or positioned for. Everything else — results,
# contract modifications, execution reports — is history and never reaches the pipeline.
BIDDABLE = {"competition", "planning"}

PAGE_LIMIT = 200
MAX_PAGES = 200
RETRIES = 5
FRESHNESS_CEILING_HOURS = 36


def _get(url, tries=RETRIES):
    """GET with backoff. Returns (parsed_json, headers).

    The policy lives in `net` and not here. It used to live here, written as a tuple of
    urllib exception types, and it was the wrong tuple: the register answers a run it does
    not want by resetting the connection, which arrives as `http.client.RemoteDisconnected`
    — an OSError and an HTTPException, and neither a `URLError` nor a `TimeoutError`. Four
    attempts were budgeted and none was ever spent. See net.py for the hierarchy in full.
    """
    return net.get_json(url, headers={"User-Agent": UA, "Accept": "application/json"},
                        timeout=60, tries=tries, log=lambda line: print(line, file=sys.stderr))


def fetch_window(date_from, date_to):
    """Every notice published in [date_from, date_to]. Returns (records, coverage)."""
    base = {
        "publishedFrom": date_from.strftime("%d/%m/%Y"),
        "publishedTo": date_to.strftime("%d/%m/%Y"),
        "limit": PAGE_LIMIT,
        "withInflections": "true",
        "searchPhrase": "true",
    }

    # Notices publish while we walk, which shifts pagination under us. An insert makes a row
    # appear on two consecutive pages; a withdrawal makes one row slip through the gap. So the
    # honest proof is: dedup by notice UUID, then require at least as many unique rows as the
    # register claimed. Fewer means we lost something. More means the register grew mid-walk,
    # which is expected and recorded as drift rather than hidden.
    seen, records, total, page = set(), [], None, 1
    page_size = PAGE_LIMIT
    while page <= MAX_PAGES:
        params = dict(base, page=page)
        data, headers = _get(API + "?" + urllib.parse.urlencode(params))

        if total is None:
            raw_total = headers.get("x-total-count") or headers.get("X-Total-Count")
            if raw_total is None:
                raise RuntimeError("no x-total-count header — the search API contract changed")
            total = int(raw_total)
            # The server silently caps page size below whatever we ask for. Believe its
            # x-limit header, never our own request, or the walk stops after one page.
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
        time.sleep(0.25)  # politeness; the register is a public service

    coverage = {
        "expected": total,
        "unique": len(records),
        "pages": page,
        "drift": len(records) - (total or 0),
        "proven": total is not None and len(records) >= total,
    }
    return records, coverage


def normalise(rec):
    """The one record shape the rest of the pipeline knows. Nothing else crosses this line."""
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


def freshest(records):
    """Most recent publicationDate in the pull, as an aware datetime, or None."""
    best = None
    for rec in records:
        stamp = rec.get("publicationDate")
        if not stamp:
            continue
        try:
            parsed = dt.datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if best is None or parsed > best:
            best = parsed
    return best


def main(argv=None):
    ap = argparse.ArgumentParser(description="Harvest Latvian procurement notices (deterministic).")
    ap.add_argument("--days", type=int, default=3,
                    help="window length ending today; overlapping windows catch late publication")
    ap.add_argument("--from", dest="date_from", help="window start, DD.MM.YYYY (overrides --days)")
    ap.add_argument("--to", dest="date_to", help="window end, DD.MM.YYYY")
    ap.add_argument("--out", required=True, help="output .jsonl path; a .meta.json is written beside it")
    ap.add_argument("--allow-empty", action="store_true",
                    help="treat an empty window as success (only for backfilling known-quiet days)")
    args = ap.parse_args(argv)

    today = dt.date.today()
    date_to = dt.datetime.strptime(args.date_to, "%d.%m.%Y").date() if args.date_to else today
    if args.date_from:
        date_from = dt.datetime.strptime(args.date_from, "%d.%m.%Y").date()
    else:
        date_from = date_to - dt.timedelta(days=max(args.days - 1, 0))

    started = dt.datetime.now().astimezone()
    raw, coverage = fetch_window(date_from, date_to)

    if not coverage["proven"]:
        print("COVERAGE FAILED: %d unique of %d claimed — refusing to ship a partial window"
              % (coverage["unique"], coverage["expected"]), file=sys.stderr)
        return 2

    if not raw and not args.allow_empty:
        print("EMPTY WINDOW %s..%s — the register returned nothing at all; treating as a source failure"
              % (date_from, date_to), file=sys.stderr)
        return 3

    newest = freshest(raw)
    lag_hours = None
    if newest is not None:
        lag_hours = round((started - newest).total_seconds() / 3600.0, 1)
        if date_to >= today and lag_hours > FRESHNESS_CEILING_HOURS:
            print("FRESHNESS FAILED: newest notice is %.1f h old (ceiling %d h)"
                  % (lag_hours, FRESHNESS_CEILING_HOURS), file=sys.stderr)
            return 4

    biddable = [normalise(r) for r in raw if r.get("type") in BIDDABLE]

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for rec in biddable:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_type = {}
    for rec in raw:
        key = rec.get("type") or "unknown"
        by_type[key] = by_type.get(key, 0) + 1

    meta = {
        "step": "harvest",
        "window": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "started": started.isoformat(),
        "coverage": coverage,
        "by_notice_type": by_type,
        "biddable": len(biddable),
        "freshness_hours": lag_hours,
        "out": os.path.basename(args.out),
    }
    with open(args.out.replace(".jsonl", "") + ".meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    print("harvest %s..%s | %d notices, coverage proven (drift %+d) | %d biddable | freshness %s h"
          % (date_from, date_to, coverage["unique"], coverage["drift"], len(biddable), lag_hours))
    return 0


if __name__ == "__main__":
    sys.exit(main())
