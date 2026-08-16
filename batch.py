#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Many tenders, one polite download stream, and the reading done in the gaps.

    python3 batch.py --targets targets.txt --out packs
    python3 batch.py --days 1 --out packs          # discover the window first

WHERE THE TIME GOES. A record costs a little work and then a pause several times longer —
the pause that keeps EIS answering us. Most of a tender's wall clock is therefore spent
waiting, while its own post-processing (extract, OCR, précis) sits behind it. Run strictly
sequentially, a day of tenders takes hours.

So the shape here is a producer and a consumer:

    main thread   download tender 1 ─ pause ─ download tender 2 ─ pause ─ ...
    worker thread              └─ extract 1, read scans, summarise ─┘

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
import queue as queue_mod
import sys
import threading
import time
import traceback

from console import say, utf8_streams

import eis_fetch
import eis_tool


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
            tuple(t.casefold() for t in (policy.get("hard_exclude_title_terms") or ())))

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
    recall_terms, exclude_prefixes, exclude_title_terms = policy

    title = str(notice.get("title") or notice.get("name") or "").casefold()
    if title and any(term in title for term in exclude_title_terms):
        return True

    codes = cpv_codes(notice)
    if codes and exclude_prefixes and all(c.startswith(exclude_prefixes) for c in codes):
        return True

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


def plan_shards(items, of, weight=None):
    """Split `items` into `of` lists of similar total weight.

    Longest-processing-time-first: heaviest item onto whichever shard is currently lightest.
    Deterministic — every shard computes the same plan from the same input and takes its own
    slice, so no planning job and no coordination is needed.
    """
    if not of or of < 2:
        return [list(items)]
    weight = weight or (lambda x: 1.0)
    # Sort by weight, then by the item itself, so ties never depend on dict ordering.
    ordered = sorted(items, key=lambda x: (-weight(x), str(x)))
    lists = [[] for _ in range(of)]
    loads = [0.0] * of
    for item in ordered:
        lightest = loads.index(min(loads))
        lists[lightest].append(item)
        loads[lightest] += weight(item)
    return lists


def take_shard(targets, shard, of, weights=None):
    """This runner's slice, or nothing at all if this runner was not asked for.

    With weights it is a balanced partition; without them, round-robin — which still beats
    contiguous blocks, because a block containing the day's monster leaves everyone else
    idle while it grinds.

    THE GUARD IS THE POINT. The workflow matrix is fixed at four shards while `--of` comes
    from an input, so asking for two shards still starts four jobs. Without this, shard 3
    wrapped onto shard 1's slice and shard 4 onto shard 2's, and both pairs downloaded the
    same tenders — the same duplicate pull the `needs:` ordering was added to stop, arriving
    through a different door. A shard outside the requested count has no work, and says so.
    """
    if of and shard > of:
        return []
    if not of or of < 2:
        return targets
    if weights:
        plan = plan_shards(targets, of, lambda t: weights.get(t, 1.0))
        return plan[(shard - 1) % of]
    return [t for i, t in enumerate(targets) if i % of == (shard - 1) % of]


def targets_from(path=None, days=None, date_from=None, date_to=None, no_gate=False):
    """(targets, weights). A hand-written list carries no register data, so it weighs 1."""
    if path:
        with open(path, encoding="utf-8") as fh:
            lines = [l.split("#")[0].strip() for l in fh]
        return [l for l in lines if l], {}
    found = eis_tool.discover(days=days or 1, date_from=date_from, date_to=date_to)
    policy = None if no_gate else load_policy()
    targets, weights, dropped = [], {}, 0
    for n in found["notices"]:
        if not n["eis_url"]:
            continue
        if outside_scope(n, policy):
            dropped += 1
            continue
        targets.append(n["eis_url"])
        weights[n["eis_url"]] = weigh(n)
    if dropped:
        say("pre-download filter: %d of %d notice(s) matched no recall term - not fetched"
            % (dropped, dropped + len(targets)))
    return targets, weights


def as_url(target):
    """Accept an EIS URL, a bare procurement id, or an IUB notice uuid."""
    target = target.strip()
    if target.lower().startswith("http") and "iub.gov.lv" not in target:
        return target
    if target.isdigit():
        return eis_tool.eis_page.PAGE % target
    return eis_tool.resolve(target)


def post_process(pack, llm_max_files=None, shards=1):
    """Everything that happens after the bytes are on disk. Runs off the download thread."""
    code = eis_tool.extract(pack, keep_unpacked=True)
    if code:
        raise RuntimeError("normalize exited %s" % code)
    eis_tool.read_scans(pack, limit=llm_max_files)
    eis_tool.summarise(pack, shards=shards)


def run(targets, out, llm_max_files=None, workers=1, sections=None, shards=1):
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
    lock = threading.Lock()

    def consume():
        while True:
            item = work.get()
            if item is None:
                work.task_done()
                return
            pack, url = item
            try:
                post_process(pack, llm_max_files, shards)
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
        url = as_url(target)
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
            eis_fetch.fetch(url, pack, sections)
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
    # "<what was asked>\t<EIS id, or - when nothing was behind it>", one line
    # per target, in the order asked. Additive: done.txt and failed.txt are untouched.
    with open(os.path.join(out, "resolved.tsv"), "w", encoding="utf-8") as fh:
        fh.write("".join("%s\t%s\n" % pair for pair in resolved))
    say("batch · %d read · %d failed · %.1f min total"
        % (len(done), len(failed), (time.time() - started) / 60.0))
    return done, failed


def main(argv=None):
    utf8_streams()
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("--targets", help="file of EIS URLs, ids or notice uuids, one per line")
    ap.add_argument("--days", type=int, default=None, help="discover this window instead")
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

    if not args.targets and args.days is None and not args.date_from:
        ap.error("give --targets, or --days/--from to discover")
    try:
        targets, weights = targets_from(args.targets, args.days, args.date_from,
                                        args.date_to, args.no_gate)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    whole = len(targets)
    heavy = sum(1 for t in targets if weights.get(t, 1) > 1)
    targets = take_shard(targets, args.shard, args.of, weights)
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
    done, failed = run(targets, args.out, args.llm_max_files, sections=sections,
                       shards=args.of)
    # A tender that could not be fetched is recorded, not fatal: the run still delivered
    # everything else, and a caller that treated this as total failure would throw it away.
    return 0 if done else (1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
