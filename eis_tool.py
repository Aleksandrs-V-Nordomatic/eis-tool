#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One tender, end to end: find it, download it, read it, say what could not be read.

    python3 eis_tool.py probe                                 # can this address reach EIS
    python3 eis_tool.py resolve 1b4e28ba-...-0016d3cca427     # IUB notice -> EIS URL
    python3 eis_tool.py fetch  https://www.eis.gov.lv/EKEIS/Supplier/Procurement/178345 --out out
    python3 eis_tool.py extract --pack out                    # deterministic text
    python3 eis_tool.py run    <notice-uuid|eis-url> --out out

WHY A SINGLE ENTRY POINT. The steps existed already but only as separate scripts glued
together inside one workflow file, which meant the sequence — and the standing-down rule
that protects it — lived in YAML, where it could not be tested and could not be run
anywhere else. A VPS, a laptop and a runner now execute the identical thing.

`probe` is the part worth naming. EIS refuses part of the cloud address space at the TCP
layer: runners dispatched in the same second, downloading nothing, are partly refused and
partly served in about a second, while the register answers all of them. A failed EIS fetch
is therefore evidence about the address we drew, never about the tender — so the run asks
first, cheaply, and stands down for the next draw instead of concluding anything about a
procurement.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

import eis_fetch
import eis_page
from console import utf8_streams

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def probe(url=eis_page.BASE, timeout=40):
    """(reachable, detail). One request, no downloads — the address is the only variable."""
    try:
        done = subprocess.run(
            ["curl", "-s", "-o", os.devnull, "--connect-timeout", "20",
             "--max-time", str(timeout), "-A", eis_fetch.UA,
             "-H", "Accept-Language: lv,en;q=0.8", "-w", "%{http_code}", url],
            capture_output=True, timeout=timeout + 30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "curl could not run: %s" % exc
    code = (done.stdout or b"").decode("utf-8", "replace").strip()
    if code in ("", "000"):
        return False, "no TCP connection — this address is refused, the tender is unknown"
    return True, "EIS answered %s" % code


def address():
    """The egress address this run drew, for the log. Never a reason to fail."""
    try:
        done = subprocess.run(["curl", "-s", "--max-time", "15", "https://api.ipify.org"],
                              capture_output=True, timeout=45)
        return (done.stdout or b"").decode("utf-8", "replace").strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def resolve(notice, timeout=45):
    """An IUB notice uuid or URL -> the EIS procurement URL, or None.

    The register's search API returns the notice and never the platform link, so this hop
    cannot be skipped: uuid -> notice HTML -> EIS id -> documents.
    """
    url = eis_page.IUB_NOTICE % notice if UUID.match(notice.strip()) else notice
    workdir = tempfile.mkdtemp(prefix="eis_resolve_")
    try:
        html = eis_fetch.Curl(workdir, url).get_text(url)
    except eis_fetch.Fail:
        return None
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    pid = eis_page.resolve_eis_id(html)
    return eis_page.PAGE % pid if pid else None


def discover(days=1, date_from=None, date_to=None, resolve_links=True):
    """Every biddable notice published in a window, with its EIS URL where one exists.

    ONE SOURCE, NAMED AS SUCH. This walks the register and resolves each notice to the
    platform, because the register's API returns the notice and never the platform link.

    What that leaves out, deliberately: a procurement below the publication duty never
    reaches the register, so it is not discovered here and no count below refers to it.
    Enumerating those means walking EIS ids instead (`eis_page.walk_ids`, present and
    unused). Until something calls it, "the window" means the register's window and
    nothing wider — say so rather than let a complete-looking day imply otherwise.

    HOW MUCH THAT IS, MEASURED. Walking ids 179550-179800 and putting each page to the
    register: 129 of 251 ids were live, and 25 of those 129 — a fifth — had no register
    notice at all. Mostly `Neregulēts iepirkums`, market consultations, and closed
    competitions inside a dynamic purchasing system. None of them can appear below, however
    complete the coverage proof looks, because coverage is proven against the register's
    own total and the register is exactly what they are missing from.

    The register's own cadence, over a fortnight: about 200 biddable notices a working day,
    published from 05:00 to shortly before midnight, and the API carries them within
    minutes — the newest notice was two minutes old when measured. There is nothing to wait
    for after a publication, but a day is not finished until its last notice, near midnight.

    A notice with no EIS link is not an error: it is a procurement conducted somewhere else,
    and it is reported as such rather than dropped.
    """
    import datetime as dt
    import harvest

    today = dt.date.today()
    end = dt.date.fromisoformat(date_to) if date_to else today
    start = (dt.date.fromisoformat(date_from) if date_from
             else end - dt.timedelta(days=max(int(days) - 1, 0)))

    raw, coverage = harvest.fetch_window(start, end)
    # Coverage is proven against the register's own total or the run stops. A partial
    # window that looks complete is the one failure discovery must never ship.
    if not coverage["proven"]:
        raise RuntimeError("register coverage not proven: %d unique of %d claimed"
                           % (coverage["unique"], coverage["expected"]))

    found = []
    for record in raw:
        if record.get("type") not in harvest.BIDDABLE:
            continue
        notice = harvest.normalise(record)
        url = resolve(notice["uuid"]) if resolve_links and notice["uuid"] else None
        found.append(dict(notice, eis_url=url))
    return {"from": start.isoformat(), "to": end.isoformat(), "coverage": coverage,
            "notices": found}


def extract(pack, with_images=False, keep_unpacked=False):
    """Deterministic text. Imported rather than shelled out, so a failure is a traceback."""
    import normalize
    argv = ["--in", pack, "--out", os.path.join(pack, "normalized")]
    if with_images:
        argv.append("--with-images")
    # The scan lane runs after this and can only read what still exists: a file that came
    # out of an archive has no downloaded original to fall back on.
    if keep_unpacked:
        argv.append("--keep-unpacked")
    return normalize.main(argv)


def read_scans(pack, model=None, limit=None, provider=None):
    """The fallback lane over files no decoder could read. Never fails the run.

    Defaults to local OCR, which needs no account and no key — so this step works out of
    the box on any machine that has Tesseract, and degrades to a printed note rather than
    an error on one that does not. A pack whose scans stay unread is exactly as complete
    as it was before this lane existed.
    """
    import assist as assist_mod
    provider = provider or os.environ.get("ASSIST_PROVIDER", assist_mod.DEFAULT_PROVIDER)
    _, needs_key, _, _ = assist_mod.PROVIDERS.get(
        provider, assist_mod.PROVIDERS[assist_mod.DEFAULT_PROVIDER])
    api_key = os.environ.get("%s_API_KEY" % provider.upper()) or \
        os.environ.get("ASSIST_API_KEY")
    if needs_key and not api_key:
        print("scan lane skipped — %s_API_KEY not set (the pack is complete without it)"
              % provider.upper())
        return 0
    # EVERY exception, not one class of them. This lane reads files the deterministic
    # extractor already listed as unreadable, and a pack whose scans stay unread is exactly
    # as complete as it was before the lane existed — so nothing it does may fail a tender.
    # Guarding only RuntimeError made that promise depend on which class a dependency
    # happened to raise: PyMuPDF raises its own hierarchy, so rasterising one oversized page
    # threw `code=5: Overly large image` straight past this handler and marked a tender with
    # 20 records, 50 files and 1.4 million extracted characters as a failure.
    try:
        doc = assist_mod.run(pack, model=model, api_key=api_key, provider=provider,
                             limit=limit)
    except Exception as exc:
        print("scan lane skipped — %s" % str(exc)[:200])
        return 0
    print("%s lane · %d read · %d deferred · %d skipped"
          % (doc["provider"], doc["read"], doc["deferred"], doc["skipped"]))
    return 0


def main(argv=None):
    utf8_streams()

    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="can this address reach EIS at all")
    p = sub.add_parser("resolve", help="IUB notice uuid or URL -> EIS URL")
    p.add_argument("notice")

    p = sub.add_parser("discover", help="notices published in a window, with EIS links")
    p.add_argument("--days", type=int, default=1, help="window length ending today")
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    p.add_argument("--urls-only", action="store_true",
                   help="print one EIS URL per line — the input eis-batch expects")
    p.add_argument("--out", default=None, help="also write the full JSON here")

    for name, help_text in (("fetch", "download one procurement's documents"),
                            ("run", "resolve if needed, then fetch, extract and assist")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("target", help="EIS URL, or an IUB notice uuid/URL for `run`")
        p.add_argument("--out", default="out")
        if name == "run":
            p.add_argument("--with-images", action="store_true")
            p.add_argument("--llm-max-files", type=int, default=None)
            p.add_argument("--model", default=None)

    p = sub.add_parser("extract", help="turn a downloaded pack into text")
    p.add_argument("--pack", required=True)
    p.add_argument("--with-images", action="store_true")

    args = ap.parse_args(argv)

    if args.command == "probe":
        reachable, detail = probe()
        print("address %s · %s" % (address(), detail))
        return 0 if reachable else 1

    if args.command == "resolve":
        url = resolve(args.notice)
        if not url:
            print("no EIS link on that notice — it may be IUB-only", file=sys.stderr)
            return 1
        print(url)
        return 0

    if args.command == "discover":
        try:
            found = discover(args.days, args.date_from, args.date_to)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 2
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(found, fh, ensure_ascii=False, indent=2)
        linked = [n for n in found["notices"] if n["eis_url"]]
        if args.urls_only:
            for notice in linked:
                print(notice["eis_url"])
        else:
            for notice in found["notices"]:
                print("%-11s %-58s %s" % (notice["published"], (notice["title"] or "")[:58],
                                          notice["eis_url"] or "— no EIS link"))
            print("\n%s..%s · %d biddable · %d with documents on EIS"
                  % (found["from"], found["to"], len(found["notices"]), len(linked)),
                  file=sys.stderr)
        return 0

    if args.command == "extract":
        return extract(os.path.abspath(args.pack), args.with_images)

    target, out = args.target, os.path.abspath(args.out)
    if args.command == "run" and not target.lower().startswith("http"):
        url = resolve(target)
        if not url:
            print("could not resolve %s to an EIS procurement" % target, file=sys.stderr)
            return 1
        print("resolved to %s" % url)
        target = url

    code = eis_fetch.main([target, "--out", out])
    if code or args.command == "fetch":
        return code

    code = extract(out, args.with_images, keep_unpacked=True)
    if code:
        return code
    read_scans(out, model=args.model, limit=args.llm_max_files)

    summary = os.path.join(out, "summary.json")
    if os.path.exists(summary):
        with open(summary, encoding="utf-8") as fh:
            print(json.dumps(json.load(fh), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
