#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One tender, end to end: find it, download it, read it, say what could not be read.

    python3 eis_tool.py probe                                 # can this address reach register + EIS
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

It asks about all three hosts, not one. A run reaches the register twice — the search API,
then one notice page per hit — before it ever addresses EIS, and a gate that consulted only
EIS passed runners whose very first request was then refused by a host nobody had asked.

WHERE THE DAY IS. Not here. A Latvian day is four runners drawing four addresses at a portal
that refuses about a third of them, so it is `batch.py` under `eis-batch.yml`, with
`collect_day.py` reconciling the shard indexes afterwards. This file is the single tender
and the pieces a day is made of.

ONE COUNTRY. This tool reads Latvia and nothing else. Everything after the read — the pack,
the digests, the index, the change comparison, the recall gate, the retry policy — is
deliberately generic, so the shape a reader sees does not depend on which portal it came
from. `--country LV` is still not optional and still has no default: see country.py.
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
import net
from console import utf8_streams

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _reach(url, timeout=40):
    """(reachable, detail) for one host. One request, no downloads.

    REACHABILITY, NOT HEALTH, AND THE DIFFERENCE MATTERS. The question is whether EIS and
    the register will open a TCP connection to the address this runner drew — they refuse
    some outright, at the transport layer, before any HTTP happens. So any status line at
    all is a pass, including the 406, 500 and 404 these three endpoints answer a bare probe
    with. Reading a status code here instead would turn a reachable host into a stand-down
    and spend a draw on nothing.
    """
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
        return False, "no TCP connection — this address is refused"
    return True, "reachable (HTTP %s)" % code


# Every host a discovery run opens, in the order it opens them. The first two are the
# register; EIS is third and used to be the only one asked about. That mattered: a runner
# stood down or went ahead on EIS's answer and then made its first request to
# infob.iub.gov.lv — a different service, which had its own opinion and was never consulted.
# The gate said the address was good, and it was, about a host the run had not reached yet.
DOORS = (("register search", "https://infob.iub.gov.lv/api/search?limit=1&page=1"),
         ("register notices", "https://eformsb.pvs.iub.gov.lv/"),
         ("EIS", eis_page.BASE))


def probe(url=None, timeout=40):
    """(reachable, detail). Every door this run has to walk through, not just the last one.

    EIS refuses part of the cloud address space at the TCP layer, and that is what this
    check was built for. It is not the only thing that can refuse us, and a gate that
    passes a runner which then cannot make its first request is worse than no gate: it
    converts a stand-down, which costs one draw, into a crash, which costs the shard.
    """
    if url is not None:
        return _reach(url, timeout)
    detail = []
    for name, door in DOORS:
        ok, why = _reach(door, timeout)
        detail.append("%s %s" % (name, why))
        if not ok:
            return False, "; ".join(detail) + " — standing down, the tender is unknown"
    return True, "; ".join(detail)


def address():
    """The egress address this run drew, for the log. Never a reason to fail."""
    try:
        done = subprocess.run(["curl", "-s", "--max-time", "15", "https://api.ipify.org"],
                              capture_output=True, timeout=45)
        return (done.stdout or b"").decode("utf-8", "replace").strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def resolve(notice, timeout=45, strict=False):
    """An IUB notice uuid or URL -> the EIS procurement URL, or None.

    The register's search API returns the notice and never the platform link, so this hop
    cannot be skipped: uuid -> notice HTML -> EIS id -> documents.

    TWO ANSWERS THAT ARE NOT THE SAME, AND USED TO BE. `None` meant both "the register
    served the notice and it names no EIS procurement" — a fact about a purchase conducted
    somewhere else — and "we never reached the register". Discovery skips a notice with no
    link, silently and by design, so the second answer was being filed as the first: a
    connection reset during resolution shrank the day, and nothing downstream could tell.
    Coverage is proven against the register's own total *before* this step, so the proof did
    not cover it either. `strict=True` separates them, and discovery asks for it.
    """
    url = eis_page.IUB_NOTICE % notice if UUID.match(notice.strip()) else notice
    workdir = tempfile.mkdtemp(prefix="eis_resolve_")
    try:
        html = eis_fetch.Curl(workdir, url).get_text(url)
    except eis_fetch.Fail as exc:
        # curl has already spent its own --retry budget by the time this raises.
        if strict:
            raise net.Unreachable("could not reach the register for %s: %s" % (notice, exc))
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
    Enumerating those means walking EIS ids instead, which `idwalk.py` now does as a second
    source beside this one. It does not widen this function: "the window" here still means
    the register's window and nothing more, and the counts below still describe only what
    the register carries. A caller reading a complete-looking day gets the truth from both
    or from neither.

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
    and it is reported as such rather than dropped. A notice we could not ask about is a
    different thing entirely and is kept apart from it — see the resolution block below,
    which is proven the way coverage is and for the same reason.
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

    biddable = [harvest.normalise(r) for r in raw if r.get("type") in harvest.BIDDABLE]

    # RESOLUTION IS PROVEN THE SAME WAY COVERAGE IS, AND PROPORTIONATELY. A notice whose
    # link could not be asked for is held apart from one that answered "no link", because
    # those two are indistinguishable once they reach the caller and only one of them is a
    # fact. Unreachable notices get one more pass; the register recovers on the scale of
    # seconds and this costs nothing on the ordinary day, where the list is empty.
    #
    # What happens to the survivors depends on what they say about the register, and the
    # boundary is a property rather than a threshold:
    #
    #   nothing resolved at all  the register is not answering this runner. Refuse the
    #                            window: standing down costs one draw, and shipping a day
    #                            of nothing but gaps costs the day.
    #   some resolved, some not  the register is up and these particular notices are the
    #                            problem. Ship, and carry them down by uuid so the run
    #                            asks once more at fetch time and names them in failed.txt
    #                            if it still cannot. A short day is still a day, and it
    #                            says which tenders it is short of.
    found, pending = [], []
    for notice in biddable:
        if not (resolve_links and notice["uuid"]):
            found.append(dict(notice, eis_url=None))
            continue
        try:
            found.append(dict(notice, eis_url=resolve(notice["uuid"], strict=True)))
        except net.Unreachable:
            pending.append(notice)

    unreachable = []
    if pending:
        print("resolution: %d notice(s) unreachable, asking once more" % len(pending),
              file=sys.stderr)
        for notice in pending:
            try:
                found.append(dict(notice, eis_url=resolve(notice["uuid"], strict=True)))
            except net.Unreachable as exc:
                unreachable.append(notice["uuid"])
                found.append(dict(notice, eis_url=None, unreachable=str(exc)[:200]))

    linked = sum(1 for n in found if n["eis_url"])
    if unreachable and not linked:
        raise RuntimeError(
            "register resolution not proven: none of %d biddable notice(s) could be asked "
            "for its EIS link — this runner is not reaching the register, and a window of "
            "nothing but gaps is not a window. Standing down for a fresh draw." % len(biddable))
    if unreachable:
        print("resolution: %d of %d notice(s) still unreachable — carried down by uuid, to "
              "be asked again at fetch time and named if they still cannot be reached"
              % (len(unreachable), len(biddable)), file=sys.stderr)

    resolution = {"biddable": len(biddable),
                  "linked": linked,
                  "unlinked": sum(1 for n in found if not n["eis_url"] and not n.get("unreachable")),
                  "unreachable": len(unreachable),
                  "retried": len(pending),
                  "proven": not unreachable}
    return {"from": start.isoformat(), "to": end.isoformat(), "coverage": coverage,
            "resolution": resolution, "notices": found}


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

    sub.add_parser("probe", help="can this address reach the register and EIS at all")
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
