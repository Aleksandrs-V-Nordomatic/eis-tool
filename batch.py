#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Many tenders, one polite download stream, and the reading done in the gaps.

    python3 batch.py --targets targets.txt --out packs
    python3 batch.py --days 1 --out packs                        # discover the window first
    python3 batch.py --from 2026-08-17 --to 2026-08-17 \
                     --targets watching.txt --out packs          # a day, and a named few

WHERE THE TIME GOES. A record costs a little work and then a pause several times longer —
the pause that keeps EIS answering us. Most of a tender's wall clock is therefore spent
waiting, while its own post-processing (extract, OCR) sits behind it. Run strictly
sequentially, a day of tenders takes hours.

So the shape here is a producer and a consumer:

    main thread   download tender 1 ─ pause ─ download tender 2 ─ pause ─ ...
    worker thread              └─ extract 1, read scans ─┘

DOWNLOADING STAYS SINGLE-FILE AND PACED, AND THAT IS NOT NEGOTIABLE. Exactly one thread
ever speaks to EIS. The portal sees the same one polite client it saw before — same order,
same five seconds — because the thing we are hiding is our own CPU and somebody else's API,
not our manners. Widening the download stream would trade a measured courtesy for seconds
we can get for free.

ONE WORKER, DELIBERATELY. `normalize` carries module-level settings, so two extractions in
one process would race over them; and one worker is already enough to keep up with a stream
that pauses five seconds per record. If the queue ever falls behind it simply drains at the
end — later, never lost.

A TENDER THAT FAILS IS ONE TENDER. It lands in `failed.txt` and the run goes on, because
the address that works is the scarce thing and one withdrawn procurement must not cost the
other fifty.
"""

import argparse
import json
import os
import hashlib
import queue as queue_mod
import re
import sys
import threading
import time
import traceback

from console import say, utf8_streams

import eis_fetch
import eis_tool
import net


# WHAT A TENDER WILL WEIGH, GUESSED FROM WHAT THE REGISTER ALREADY SAYS.
#
# Looking for a signal available BEFORE downloading, only one of the cheap ones carries any:
#
#   * record count is nearly useless — a tender's heaviest documents are not spread evenly
#     across its records, so a tender with many records is routinely small and vice versa.
#   * the procurement's OWN CLASS does carry signal. Construction works and the engineering
#     design around them are where the large document sets live; everything else is small.
#
# So this is a class prior, not an estimate. It cannot say WHICH construction tender is the
# large one — the variance inside the class dwarfs the difference between classes.
#
# WHICH BOUNDS WHAT THIS CAN ACHIEVE, and it is worth being plain about: no arrangement of
# whole tenders finishes faster than the largest single tender takes. Balancing removes the
# pathological case — every heavy tender landing on one runner — and nothing more. Beating
# the floor would mean splitting one tender across shards, which is a different change.
#
# These two are a size heuristic and nothing else: they steer which runner does the work,
# never whether a tender is fetched. That decision is EIS_POLICY's, and it is not here.
HEAVY_CPV = ("45", "71")        # construction works, and the engineering design around it
HEAVY_KINDS = ("būvdarbi", "construction")


# THE ONE FILTER ALLOWED BEFORE A DOCUMENT EXISTS.
#
# A title is kept when it contains one of the caller's recall roots. Roots are matched as
# substrings rather than as whole words because the language this runs against inflects
# heavily; precision belongs to the later document-reading step, not to a title.
#
# Two guards matter, and both fail toward fetching:
#   * no title means no evidence, so the notice is fetched;
#   * a classification code never vetoes a matching title, because the code is assigned by
#     the buyer and an imperfect one must not silently drop a notice whose title matches.
#
# Exclusions win over recall, and exclusion by code prefix covers notices whose title is
# absent or unhelpful.
#
# WHAT THIS KNOWS ABOUT THE CALLER'S INTEREST: NOTHING — the same rule deliver_graph.py
# keeps about its destination. The terms arrive in the environment, so this file names no
# industry, no trade and no target, and a reader of this repository learns the shape of the
# filter without learning what anyone points it at. An absent or unreadable policy means
# fetch everything, which is the only safe direction for a filter that failed to load:
# fetching too much costs time, and dropping silently costs a tender.
POLICY_ENV = "EIS_POLICY"


def load_policy(source=None):
    """The caller's recall policy, or None. None means no filter — fetch everything.

    `source` is JSON text, a path to a JSON file, or None to read `EIS_POLICY` from the
    environment. Tests pass a fixture through it; production passes nothing and the
    environment answers, so no deployment's terms are ever committed here.
    """
    raw = source if source is not None else os.environ.get(POLICY_ENV)
    if not raw or not raw.strip():
        return None
    text = raw
    if not raw.lstrip().startswith("{"):              # not JSON, so treat it as a path
        try:
            with open(raw, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return None
    try:
        policy = json.loads(text)
    except ValueError:
        return None                       # an unreadable policy must fail open, never drop all
    recall = tuple(t.casefold() for t in (policy.get("recall_title_terms") or ()))
    if not recall:
        return None                       # incomplete policy must fail open, never drop all
    return (recall,
            tuple(policy.get("hard_exclude_prefixes") or ()),
            tuple(t.casefold() for t in (policy.get("hard_exclude_title_terms") or ())),
            # CODES THAT SURVIVE THEIR OWN DIVISION. A control-system contract can carry a
            # main code inside an excluded division and nothing else — `72250000` software
            # services on a SCADA maintenance contract, `79711000` on an alarm-monitoring
            # one — and 62% of live procurements carry one code only. Without an override
            # such a notice is dropped before a byte moves, which is the one failure the
            # exclusions are least allowed to cause.
            tuple(policy.get("override_prefixes") or ()),
            # CODES THAT RECALL ON THEIR OWN, because a title is not always the better
            # signal. Recall was title-only, and a code could exclude or rescue from an
            # exclusion but never bring anything in — so a procurement whose title is vague
            # and whose code is exact was dropped before a byte moved. Measured on the
            # Lithuanian day of 24 Aug 2026: `Stebejimo sistema` under 32323500, which is
            # literally "video-surveillance system", and `LoRaWAN ... objektu parametru
            # kontrolei` under 32440000, telemetry. Both ours, both invisible, because a
            # buyer wrote a short title. Absent, this changes nothing.
            tuple(policy.get("recall_cpv_prefixes") or ()))

def cpv_codes(notice):
    """Every CPV code a notice carries, however the source spelled them."""
    codes = []
    raw = notice.get("cpv")
    if isinstance(raw, (list, tuple)):
        codes = [str(c.get("code", "")) if isinstance(c, dict) else str(c) for c in raw]
    elif raw:
        codes = [str(raw)]
    if notice.get("cpv_main"):
        codes.append(str(notice["cpv_main"]))
    return [c.strip() for c in codes if c and c.strip()]


def outside_scope(notice, policy):
    """Should this notice be excluded before any documents are fetched?"""
    if not policy:
        return False
    # Older policies carry three fields; the override list is the fourth and optional.
    recall_terms, exclude_prefixes, exclude_title_terms = policy[:3]
    override_prefixes = policy[3] if len(policy) > 3 else ()
    recall_prefixes = policy[4] if len(policy) > 4 else ()

    title = str(notice.get("title") or notice.get("name") or "").casefold()
    if title and any(term in title for term in exclude_title_terms):
        return True

    codes = cpv_codes(notice)
    # An override is read anyway, wherever its division sits. The gate asks what the buyer
    # classified this as; whether the work is ours is a later and different question.
    overridden = bool(override_prefixes) and any(c.startswith(override_prefixes)
                                                 for c in codes)
    if (codes and exclude_prefixes and not overridden
            and all(c.startswith(exclude_prefixes) for c in codes)):
        return True

    # A CODE CAN RECALL, AND IT IS ASKED BEFORE THE TITLE. The exclusions above still
    # bind — an excluded title term or an all-excluded code set has already returned — so
    # this widens what is fetched and can never drop anything the old gate kept.
    if recall_prefixes and any(c.startswith(recall_prefixes) for c in codes):
        return False

    if not title:
        return False                      # missing signal fails open
    return not any(term in title for term in recall_terms)

def weigh(notice):
    """A relative cost for planning. Not megabytes — a class, expressed as a number.

    The register hands CPV over as a LIST of {code, caption}, and `work_kind` is not in its
    record at all — it lives on the EIS page, which planning has not fetched yet. So the
    list is the signal that actually exists at this moment, and reading it has to work.
    An earlier version stringified the whole list before testing it, which quietly made
    every tender weigh the same; only the test that fed it a real register shape caught it.
    """
    codes = []
    raw = notice.get("cpv")
    if isinstance(raw, (list, tuple)):
        codes = [str(c.get("code", "")) if isinstance(c, dict) else str(c) for c in raw]
    elif raw:
        codes = [str(raw)]
    if notice.get("cpv_main"):                       # present once the page has been read
        codes.append(str(notice["cpv_main"]))

    kind = str(notice.get("work_kind") or "").lower()
    heavy = (any(c.strip().startswith(HEAVY_CPV) for c in codes)
             or any(k in kind for k in HEAVY_KINDS))
    return 10.0 if heavy else 1.0


def shard_of(target, of):
    """Which runner owns this target, 1..of, decided from the target and nothing else.

    THE PARTITION USED TO BE A PROPERTY OF THE LIST, AND THAT IS WHAT BROKE IT. Each shard
    walks the register for itself and then split the result by longest-processing-time-first
    bin-packing, on the premise that the same input gives every shard the same plan. The
    premise fails the moment one shard's answer differs by a single notice — and the failure
    is not proportional, because greedy packing cascades: one item changing weight moves it
    in the sort order and reshuffles everything packed after it.

    Measured on a four-shard run over three days of publications. All four agreed there were
    93 targets; one weighed a single notice differently; the slices came to 21+23+23+23 —
    ninety assignments covering sixty-eight distinct tenders. Twenty-two fetched twice, about
    two dozen fetched by nobody, and the day called itself complete because every shard had
    delivered something.

    A digest of the target's own identity cannot cascade. Two shards that disagree about the
    list still agree about every tender in it, so the only way to lose one is for its owner
    not to have seen it — a straight miss rather than a reshuffle, and `coverage` names it.

    WHAT THIS GIVES UP, PLAINLY: the weighted balance. It bought less than it looked like it
    did — the class prior is a prior, and the variance inside the class dwarfs the difference
    between classes, so no arrangement of whole tenders finishes faster than the largest one
    takes. The same run that lost two dozen tenders spent 46.8 minutes on one shard against
    7.4 on another, balanced. A digest spreads heavy and light alike in expectation, which is
    as much as a class prior can honestly promise.
    """
    key = identity(target) or str(target)
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % of + 1


def take_shard(targets, shard, of):
    """This runner's slice, or nothing at all if this runner was not asked for.

    THE GUARD IS THE POINT. The workflow matrix is fixed at four shards while `--of` comes
    from an input, so asking for two shards still starts four jobs. Without this, shard 3
    wrapped onto shard 1's slice and shard 4 onto shard 2's, and both pairs downloaded the
    same tenders — the same duplicate pull the `needs:` ordering was added to stop, arriving
    through a different door. A shard outside the requested count has no work, and says so.
    """
    if of and shard > of:
        return []
    if not of or of < 2:
        return list(targets)
    return [t for t in targets if shard_of(t, of) == shard]


_EIS_URL_ID = re.compile(r"/EKEIS/Supplier/Procurement/(\d+)", re.I)


def identity(target):
    """A comparable identity for a target, worked out without asking the network anything.

    Deduplication has to happen BEFORE the list is split across shards, and it has to be
    cheap: two shards handed the same tender both download it, both deliver it, and both
    write its state — which is the one arrangement that can lose an update, because the
    second writer's fingerprint overwrites the first's without having seen its documents.

    An EIS URL and a bare id are the same tender and reduce to the same key here. A notice
    uuid cannot be reduced without resolving it, which is a request; it keys as itself, and
    the case that matters — a caller naming by uuid what discovery already found — is caught
    below against the uuids discovery hands back.
    """
    t = (target or "").strip()
    if not t:
        return None
    if t.isdigit():
        return "eis:%s" % t
    found = _EIS_URL_ID.search(t)
    return "eis:%s" % found.group(1) if found else "iub:%s" % t.casefold()


def named_targets(path):
    """The caller's own list, comments and blanks removed."""
    # `utf-8-sig`, because a list typed on Windows arrives with a byte-order mark and
    # the first id then is not a number. `as_url` sends it to `resolve`, which answers
    # None, and the run reports "no EIS procurement behind it" for a target that is
    # plainly there — a diagnosis that sends the reader to the portal instead of to
    # their text editor.
    with open(path, encoding="utf-8-sig") as fh:
        lines = [l.split("#")[0].strip() for l in fh]
    return [l for l in lines if l]


def targets_from(path=None, days=None, date_from=None, date_to=None, no_gate=False):
    """(targets, weights, uuids) for the window, the named list, or BOTH.

    A caller that wants yesterday's publications *and* a handful of tenders it is already
    watching asks for both in one run and gets one list. That is not a convenience: fetching
    them as two runs means two draws against a portal that refuses a third of runner
    addresses, two deliveries into the same day, and two `day.json` files where the second
    silently describes less than the first.

    A hand-written entry carries no register data, so it weighs 1 — and, having come from
    nobody knows where, proves nothing about the register either, so it contributes no uuid
    and its packs come back `register_check: unverified`. A tender in both halves keeps the
    discovered version, which is the one that knows its notice.
    """
    targets, weights, uuids, seen, dropped, unreachable = [], {}, {}, set(), 0, 0

    if days is not None or date_from or date_to:
        found = eis_tool.discover(days=days or 1, date_from=date_from, date_to=date_to)
        policy = None if no_gate else load_policy()
        for n in found["notices"]:
            if not n["eis_url"]:
                # A notice discovery could not ask about is not a notice without a link, and
                # dropping it here is how a day used to come up short in silence. It goes
                # down the ordinary path under its uuid: `as_url` asks once more at fetch
                # time — minutes later, which is usually long enough — and names it in
                # failed.txt if it still cannot. A notice that genuinely has no EIS
                # procurement behind it is skipped exactly as it always was.
                if n.get("unreachable") and not outside_scope(n, policy):
                    targets.append(n["uuid"])
                    seen.add("iub:%s" % n["uuid"].casefold())
                    unreachable += 1
                continue
            if outside_scope(n, policy):
                dropped += 1
                continue
            targets.append(n["eis_url"])
            weights[n["eis_url"]] = weigh(n)
            seen.add(identity(n["eis_url"]))
            # The register notice this target came from. Carried the whole way down so the
            # pack records that membership was established by discovery, rather than
            # re-deriving it from a page that usually does not say.
            if n.get("uuid"):
                uuids[n["eis_url"]] = n["uuid"]
                seen.add("iub:%s" % n["uuid"].casefold())
        if dropped:
            say("pre-download filter: %d of %d notice(s) matched no recall term - not fetched"
                % (dropped, dropped + len(targets)))
        if unreachable:
            say("discovery could not reach the register for %d notice(s) - carried down by "
                "uuid and asked again below" % unreachable)

    if path:
        asked = named_targets(path)
        window = set(seen)
        for t in asked:
            key = identity(t)
            if key in seen:
                continue
            seen.add(key)
            targets.append(t)
        overlap = sum(1 for t in asked if identity(t) in window)
        if overlap:
            say("named list: %d target(s), %d of them already in the window"
                % (len(asked), overlap))

    return targets, weights, uuids


def as_url(target):
    """Accept an EIS URL, a bare procurement id, or an IUB notice uuid.

    Raises `net.Unreachable` when the register could not be asked, rather than returning
    None. The caller reports the two apart: "no EIS procurement behind it" is a fact about
    the purchase and sends a reader to the portal; "could not reach the register" is a fact
    about this run and sends them nowhere. They read identically as a bare None.
    """
    target = target.strip()
    if target.lower().startswith("http") and "iub.gov.lv" not in target:
        return target
    if target.isdigit():
        return eis_tool.eis_page.PAGE % target
    return eis_tool.resolve(target, strict=True)


def post_process(pack, llm_max_files=None):
    """Everything that happens after the bytes are on disk. Runs off the download thread."""
    code = eis_tool.extract(pack, keep_unpacked=True)
    if code:
        raise RuntimeError("normalize exited %s" % code)
    eis_tool.read_scans(pack, limit=llm_max_files)


def resolutions(out):
    """What each asked-for string turned into, as identity → identity, from `resolved.tsv`."""
    path = os.path.join(out, "resolved.tsv")
    found = {}
    if not os.path.exists(path):
        return found
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            asked, _, pid = line.rstrip("\n").partition("\t")
            key = identity(asked)
            if key and pid and pid != "-":
                found[key] = "eis:%s" % pid
    return found


def write_accounts(out, day_targets, failed, withdrawn):
    """What the WHOLE day was asked for, beside what this shard could not deliver of it.

    THE SHARD PARTITION IS NOT AS DETERMINISTIC AS IT LOOKS, and this file is how that stops
    being invisible. `plan_shards` is a greedy bin-pack over a list every shard computes for
    itself by walking the register; the shards are supposed to agree and take disjoint
    slices. Measured on one four-shard run: all four agreed there were 93 targets, one of
    them weighed a single notice differently, and the slices came to 21+23+23+23 — ninety
    assignments covering sixty-eight distinct tenders. Twenty-two fetched twice, and about
    two dozen fetched by nobody, while the day called itself complete because every shard
    had delivered something.

    So each shard publishes the day's whole target list, not just its own slice. `collect_day`
    unions them, subtracts what was delivered and what the shards reported as failed or
    withdrawn, and what is left is a tender nobody fetched. This measures the gap; it does
    not close it. Closing it means changing how the work is divided, which is a larger change
    and a different trade — a stable split costs the balancing that keeps one runner from
    taking every heavy tender.

    Identities rather than URLs, so the reader compares strings and never parses anything.
    """
    # A NOTICE UUID IS NOT WHAT THE DAY DELIVERS. It is asked for as `iub:<uuid>` and comes
    # back as a procurement id, so left alone it could never match a delivered tender and
    # would sit in `unaccounted` for ever — the coverage check calling every day short on a
    # target that arrived. `resolved.tsv` is what each asked-for string turned into, so the
    # identities are normalised through it before anyone compares them.
    seen = resolutions(out)
    key = lambda t: seen.get(identity(t), identity(t))
    body = {"schema": "shard-accounts/1",
            "targets": sorted({key(t) for t in day_targets if identity(t)}),
            "failed": sorted({key(l.split()[0]) for l in failed if l.split()}),
            "withdrawn": sorted({key(l.split()[0]) for l in withdrawn if l.split()}),
            # What this shard resolved, so the collector can normalise a uuid that another
            # shard owned and this one only ever saw as a target.
            "resolved": {k: v for k, v in sorted(seen.items()) if k != v}}
    with open(os.path.join(out, "accounts.json"), "w", encoding="utf-8") as fh:
        json.dump(body, fh, ensure_ascii=False)
    return body


def run(targets, out, llm_max_files=None, workers=1, sections=None, uuids=None):
    """Download in order, post-process behind it. Returns (done, failed)."""
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    work = queue_mod.Queue()
    done, failed = [], []
    # What each thing we were asked for turned into. A caller that names tenders by IUB
    # notice uuid cannot otherwise tell whether its request came back: the pack is keyed
    # by EIS id, and a procurement below the publication duty prints no register link at
    # all, so its procurement.json carries iub_uuid: null and echoes nothing. Without this
    # line a caller can only match by counting -- which is also wrong the moment two
    # notices turn out to be one procurement.
    resolved = []
    # EIS's own "no displayable stage" answer, kept apart from `failed`: the id was checked
    # and answered, not left in doubt, and a caller should neither retry it nor read its
    # presence as the run coming back short. See `eis_fetch.Withdrawn`.
    withdrawn = []
    lock = threading.Lock()

    def consume():
        while True:
            item = work.get()
            if item is None:
                work.task_done()
                return
            pack, url = item
            try:
                post_process(pack, llm_max_files)
                with lock:
                    done.append(os.path.basename(pack))
                say("  read   %s" % os.path.basename(pack))
            except Exception as exc:                    # one tender, not the batch
                with lock:
                    failed.append("%s post-processing failed: %s" % (url, str(exc)[:160]))
                say("  FAILED %s — %s" % (os.path.basename(pack), str(exc)[:80]))
            finally:
                work.task_done()

    hands = [threading.Thread(target=consume, daemon=True) for _ in range(max(1, workers))]
    for h in hands:
        h.start()

    started = time.time()
    for position, target in enumerate(targets, 1):
        try:
            url = as_url(target)
        except net.Unreachable as exc:
            resolved.append((str(target), "-"))
            failed.append("%s — %s" % (target, exc))
            say("  FAILED %s — could not reach the register to resolve it" % target)
            continue
        if not url:
            resolved.append((str(target), "-"))
            failed.append("%s — no EIS procurement behind it" % target)
            continue
        pid = url.rstrip("/").rsplit("/", 1)[-1]
        resolved.append((str(target), pid))
        pack = os.path.join(out, pid)
        say("[%d/%d] %s" % (position, len(targets), url))
        try:
            # The one place that talks to the portal, and it is single-file on purpose.
            # `target` is the key, not `url`: they differ when the caller named an IUB
            # notice and `as_url` resolved it, and the uuid map is keyed by what discovery
            # produced.
            eis_fetch.fetch(url, pack, sections,
                            register_uuid=(uuids or {}).get(target) or (uuids or {}).get(url))
        except eis_fetch.Withdrawn as exc:
            # Caught ahead of the plain `Fail` it subclasses: this id is settled, not
            # outstanding, so it is recorded apart and never repeated back as a gap.
            withdrawn.append("%s — %s" % (url, str(exc)[:200]))
            say("  WITHDRAWN %s — %s" % (pid, str(exc)[:100]))
            continue
        except eis_fetch.Fail as exc:
            failed.append("%s — %s" % (url, str(exc)[:200]))
            say("  FAILED %s — %s" % (pid, str(exc)[:100]))
            continue
        except Exception as exc:
            failed.append("%s — %s" % (url, str(exc)[:200]))
            say("  FAILED %s — %s" % (pid, str(exc)[:100]))
            continue
        work.put((pack, url))
        say("  queued %s for reading (%d waiting)" % (pid, work.qsize()))

    say("downloads finished in %.1f min — waiting for the reader to catch up"
        % ((time.time() - started) / 60.0))
    work.join()
    for _ in hands:
        work.put(None)
    for h in hands:
        h.join(timeout=30)

    with open(os.path.join(out, "done.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(sorted(done)) + ("\n" if done else ""))
    with open(os.path.join(out, "failed.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(failed) + ("\n" if failed else ""))
    # Its own file, not a section of failed.txt: a reader greping failed.txt for outstanding
    # work should not have to also know which lines are settled and which are not.
    with open(os.path.join(out, "withdrawn.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(withdrawn) + ("\n" if withdrawn else ""))
    # "<what was asked>\t<EIS id, or - when nothing was behind it>", one line
    # per target, in the order asked. Additive: done.txt and failed.txt are untouched.
    with open(os.path.join(out, "resolved.tsv"), "w", encoding="utf-8") as fh:
        fh.write("".join("%s\t%s\n" % pair for pair in resolved))
    say("batch · %d read · %d failed · %d withdrawn · %.1f min total"
        % (len(done), len(failed), len(withdrawn), (time.time() - started) / 60.0))
    return done, failed, withdrawn


def main(argv=None):
    utf8_streams()
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("--targets", help="file of EIS URLs, ids or notice uuids, one per line")
    ap.add_argument("--days", type=int, default=None,
                    help="also discover this window; combines with --targets")
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--out", default="packs")
    ap.add_argument("--llm-max-files", type=int, default=None)
    ap.add_argument("--shard", type=int, default=1, help="which slice this runner takes")
    ap.add_argument("--of", type=int, default=1, help="how many runners share the list")
    ap.add_argument("--skip-archive", action="store_true",
                    help="take only the governing documents, not superseded ones")
    ap.add_argument("--no-policy-gate", "--no-cpv-gate", dest="no_gate", action="store_true",
                    help="ignore EIS_POLICY and fetch every discovered notice")
    args = ap.parse_args(argv)

    if not args.targets and args.days is None and not (args.date_from or args.date_to):
        ap.error("give --targets, or --days/--from to discover, or both")
    try:
        targets, weights, uuids = targets_from(args.targets, args.days, args.date_from,
                                               args.date_to, args.no_gate)
    except (RuntimeError, net.Unreachable) as exc:
        # Two refusals, one exit. `RuntimeError` is discovery declining to ship a window it
        # cannot prove; `net.Unreachable` is the register never answering at all. Both mean
        # this runner has no list, and neither means anything about a tender — so the shard
        # fails loudly, the chain draws a fresh address, and nothing is recorded as absent.
        print(exc, file=sys.stderr)
        return 2
    whole_list = list(targets)
    whole = len(targets)
    heavy = sum(1 for t in targets if weights.get(t, 1) > 1)
    targets = take_shard(targets, args.shard, args.of)
    if not targets:
        say("nothing to fetch")
        return 0

    if args.of > 1:
        mine = sum(weights.get(t, 1.0) for t in targets)
        say("shard %d of %d — %d of %d target(s), weight %.0f (%d heavy in the day)"
            % (args.shard, args.of, len(targets), whole, mine, heavy))
    else:
        say("batch of %d target(s)" % len(targets))
    sections = eis_fetch.SECTIONS[:1] if args.skip_archive else None
    done, failed, withdrawn = run(targets, args.out, args.llm_max_files, sections=sections,
                                  uuids=uuids)
    # The WHOLE day's targets, not this shard's slice — `whole_list` is the list before it
    # was split, which is exactly what a reader needs to tell a short day from a full one.
    write_accounts(args.out, whole_list, failed, withdrawn)
    # A tender that could not be fetched is recorded, not fatal: the run still delivered
    # everything else, and a caller that treated this as total failure would throw it away.
    # A tender EIS itself says has nothing to show is weaker still: `failed` alone decides
    # this, so a shard whose whole slice turned out withdrawn exits 0 — settled, not short.
    return 0 if done else (1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
