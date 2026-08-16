#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One line per document saying what it is — so a reader can choose what to open.

    python3 precis.py --pack out                  # needs MISTRAL_API_KEY
    python3 precis.py --pack out --dry-run        # what would be summarised, and how much

WHY THIS EXISTS, AND IT IS NOT TOKEN THRIFT. A day through this pipeline runs to millions
of characters across thousands of documents, and one tender can hold most of it. Nothing
reads that. Whatever assembles a reading packet is therefore already truncating, which
means a consumer sees a SAMPLE and cannot know what it did not get. A précis per document
turns that blind cut into a routing decision: here is every document, here is what each one
is, now spend the expensive context on the few that matter.

THE RULE THAT KEEPS IT HONEST: A PRÉCIS IS NAVIGATION, NEVER EVIDENCE.
It is generated text about a document, not text from it. If a summary says "this file is
about ventilation" and a reader trusts that instead of opening the file, a requirement on
page 40 becomes invisible — and invisible is the one failure this project refuses. So:

  * précis live in `precis/`, never in `normalized/`;
  * every one is stamped `derived: precis`, with the model and prompt that made it;
  * a consumer's own evidence rule is untouched — a claim still needs a quote located in
    the source text, and a précis can never be that quote. It may say where to look.

CACHED BY CONTENT, LIKE EVERYTHING ELSE HERE. The key is the source file's sha256. Tender
paperwork repeats: `Finanšu piedāvājuma forma` and `Līguma projekts` recur across buyers
and across days, so the same digest is paid for once and then never again. A warm re-run
costs nothing and returns identical bytes.

WHY MISTRAL IS THE DEFAULT. Daily volume here is thousands of documents at a couple of
thousand input tokens each, which is millions of tokens a day. Providers whose free tier
caps REQUESTS per day are ruled out by document count alone; those capping tokens per day
are ruled out by a single day's volume. Mistral's free tier is the one with room to spare,
and it is an EU company that registers on a work address, so the account can belong to an
organisation rather than to an individual sign-up. Turn OFF training in the console
(Privacy → "Allow the use of your API calls to train Mistral's AI models"); it is on by
default.

AND THE HONEST CAVEAT, BECAUSE THIS LANE IS THE LEAST TRUSTWORTHY THING HERE. It is the
only non-deterministic step in the delivery, its coverage in practice is a fraction of the
queue, and an absent précis means "unknown" rather than "nothing found" — so nothing
downstream may use presence or absence as a filter. Everything it answers about a document
being a substring search away in text this pipeline already holds in full, this lane is a
convenience and never a dependency.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

from concurrent import futures

from console import say, utf8_streams

ENDPOINT = "https://api.mistral.ai/v1/chat/completions"
DEFAULT_MODEL = "mistral-small-latest"

# How much of a document the reader sees. A tender document says what it is in its first
# page — title block, purpose, the clause that names the subject. Sending 300k characters
# to learn "this is the contract draft" would spend the quota this lane exists to protect.
HEAD_CHARS = 8000
MIN_CHARS = 200          # shorter than this and the filename already says more

# These calls go to a commercial API on another host, not to the state portal, so the
# politeness that governs eis_fetch does not apply here. Four is a working default that
# a free tier's request rate can still absorb; --workers 1 restores the old behaviour.
DEFAULT_WORKERS = 4

# THE PROVIDER PUBLISHES ITS LIMIT, SO RESPECT THE NUMBER RATHER THAN GUESS AT IT.
# Mistral's console gives `mistral-small` 0.83 requests per second and 50,000 tokens per
# minute. Four workers at a couple of seconds a call is well over the first of those.
#
# So the pool still has four workers and the SUBMISSION rate is capped. That is the right
# split: concurrency hides the latency of a slow answer, and the limiter keeps us inside
# somebody else's published budget. Widening one does not require breaking the other.
#
# ONLY ONE OF THE TWO PUBLISHED LIMITS IS ACTUALLY HONOURED, AND IT IS WORTH SAYING SO.
# This paces requests per second; nothing here counts tokens per minute. At HEAD_CHARS per
# document across four shards the token budget is the one that binds first, which is why
# this lane meets 429s while comfortably inside its request rate. Fixing that means
# accounting for tokens as well as for calls.
MIN_REQUEST_INTERVAL = 1.25          # seconds — 0.8/s, just inside the published 0.83/s

# AND THE LIMIT BELONGS TO THE ACCOUNT, NOT TO THE PROCESS.
#
# A limiter inside one process is a lie the moment that process is one of four: each shard
# politely holds 0.8 req/s of its own, and the account sees four times that against a
# single budget. Nothing is lost when the provider refuses, because the lane caches and
# defers rather than failing — but nothing is summarised either.
#
# So a caller that is one of N tells the lane so, and each takes 1/N of the budget.
def share_of_budget(shards, interval=MIN_REQUEST_INTERVAL):
    """The interval one of `shards` parallel callers must keep for the account to stay inside."""
    return interval * max(1, int(shards or 1))


class Pace(object):
    """Lets one request start every `interval` seconds, whichever worker asks."""

    def __init__(self, interval):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


# The prompt names no trade and no target, for the same reason `EIS_POLICY` is a secret:
# a prompt that asks "does this mention X" tells a reader of this repository what the
# caller is looking for. Question 3 therefore asks the document to list what it names, and
# leaves the deciding to whoever reads the answer.
PROMPT_VERSION = 2
PROMPT = """You are indexing the documents of a Latvian public procurement so a reader
can decide which ones to open. Below is the beginning of one document.

Answer in Latvian, in at most three sentences:
1. What this document IS (nolikums, tehniskā specifikācija, līguma projekts, forma, ...).
2. What it is ABOUT — the concrete subject, works or equipment, if the text says.
3. Which specific technical systems, installations or equipment it names, if any. If it
   names none, say "nav minēts".

Do not summarise requirements, do not quote figures, do not guess at anything the text
does not state. If the beginning is uninformative, say so plainly.

--- document: %s ---
%s
"""


class Quota(RuntimeError):
    """The provider is out of free quota. Not a failure — a tomorrow."""


def prompt_digest():
    return hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()[:16]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------- the queue
def load_pack(pack):
    """Readable documents from a finished normalize, largest first."""
    with open(os.path.join(pack, "normalized", "manifest_normalized.json"),
              encoding="utf-8") as fh:
        normalized = json.load(fh)
    docs = []
    for entry in normalized.get("documents") or []:
        # Duplicates are the same file listed under a second record; one précis serves both.
        if entry.get("also_listed_under") or not entry.get("markdown_path"):
            continue
        docs.append(entry)
    docs.sort(key=lambda e: -(e.get("markdown_chars") or 0))
    return docs


def queue(docs, limit=None):
    """What to summarise, and why anything is left out."""
    send, skip = [], []
    for entry in docs:
        digest = entry.get("original_sha256")
        if not digest:
            skip.append(dict(entry, skipped="no-digest-to-cache-by"))
        elif (entry.get("markdown_chars") or 0) < MIN_CHARS:
            # A 40-character document is its own summary.
            skip.append(dict(entry, skipped="too-short-to-summarise"))
        else:
            send.append(entry)
    if limit is not None and len(send) > limit:
        for entry in send[limit:]:
            skip.append(dict(entry, skipped="over-the-run-limit"))
        send = send[:limit]
    return send, skip


# ---------------------------------------------------------------------------- the provider
def mistral_send(name, text, model, api_key, timeout=120):
    """One document to Mistral. Returns (precis, usage). Raises Quota when the tier is out."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT % (name, text)}],
        "temperature": 0,
        "max_tokens": 400,
    }).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "Authorization": "Bearer %s" % api_key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (429, 503):
            raise Quota("out of free quota or rate limited (%d): %s" % (exc.code, detail))
        raise RuntimeError("provider refused (%d): %s" % (exc.code, detail))
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("provider returned no choice: %s" % json.dumps(payload)[:200])
    return (choices[0].get("message") or {}).get("content", "").strip(), payload.get("usage") or {}


PROVIDERS = {
    # name:     (function,      default model,   env var holding the key)
    "mistral": (mistral_send, DEFAULT_MODEL, "MISTRAL_API_KEY"),
}
DEFAULT_PROVIDER = "mistral"


# --------------------------------------------------------------------------------- the run
def read_head(pack, entry):
    path = os.path.join(pack, "normalized", entry["markdown_path"])
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read(HEAD_CHARS)


def cached(lane, digest):
    path = os.path.join(lane, digest + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def run(pack, send_fn=None, model=None, api_key=None, limit=None,
        dry_run=False, provider=DEFAULT_PROVIDER, workers=DEFAULT_WORKERS,
        interval=MIN_REQUEST_INTERVAL):
    """Summarise every readable document. Returns the lane manifest; never raises on quota.

    WHY THIS ONE IS ALLOWED TO GO WIDE, WHEN THE DOWNLOADER MAY NOT. The pause in
    `eis_fetch` protects EIS, a state portal that throttles and that this depends on. These
    requests go to a commercial API on a different host, sold by the request, with a quota
    anyone can read — nothing about being polite to EIS says anything about it. In sequence
    this lane is slow enough to dominate a small tender's post-processing.

    The workers are capped and the first Quota stops the pool submitting more, so a
    rate-limited tier is met with a stop, not with a burst.
    """
    reader, default_model, _ = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    send_fn = send_fn or reader
    model = model or default_model

    pack = os.path.abspath(pack)
    lane = os.path.join(pack, "precis")
    send, skipped = queue(load_pack(pack), limit)

    results, deferred = [], []
    fresh = []
    for entry in send:
        hit = cached(lane, entry["original_sha256"])
        if hit:
            results.append(dict(hit, source="cache"))
        elif dry_run:
            deferred.append(dict(entry, deferred="dry-run"))
        else:
            fresh.append(entry)

    lock = threading.Lock()
    pace = Pace(interval)
    out_of_quota = threading.Event()
    spent = [0]

    def one(entry):
        # Once the tier says stop, the rest is tomorrow's work rather than 40 more 429s.
        if out_of_quota.is_set():
            with lock:
                deferred.append(dict(entry, deferred="stopped after the quota ran out"))
            return
        digest = entry["original_sha256"]
        name = entry.get("original_file") or entry.get("source") or "document"
        pace.wait()
        try:
            text, usage = send_fn(name, read_head(pack, entry), model, api_key)
        except Quota as exc:
            out_of_quota.set()
            with lock:
                deferred.append(dict(entry, deferred=str(exc)[:160]))
            return
        except (RuntimeError, OSError) as exc:
            with lock:
                skipped.append(dict(entry, skipped="provider-error: %s" % str(exc)[:140]))
            return

        with lock:
            spent[0] += 1
        if not text:
            with lock:
                skipped.append(dict(entry, skipped="provider-returned-nothing"))
            return

        record = {"file": name, "source": entry.get("source"), "sha256": digest,
                  "precis": text, "record_title": entry.get("record_title"),
                  "markdown_path": entry.get("markdown_path"),
                  "markdown_chars": entry.get("markdown_chars"),
                  "provider": provider, "model": model,
                  "prompt_version": PROMPT_VERSION, "prompt_sha256": prompt_digest(),
                  "head_chars": HEAD_CHARS, "at": now(), "usage": usage,
                  # The field a consumer must read before trusting a word of this.
                  "derived": "precis"}
        os.makedirs(lane, exist_ok=True)
        # Each answer is its own file named by the source digest, so concurrent writers
        # never touch the same path and no lock is needed for the write itself.
        with open(os.path.join(lane, digest + ".json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
        with lock:
            results.append(dict(record, source="provider"))
            say("  %-52s %s" % (name[-52:], text.split("\n")[0][:60]))

    if fresh:
        with futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            list(pool.map(one, fresh))
    spent = spent[0]

    doc = {"schema": 1, "derived": "precis", "provider": provider, "model": model,
           "prompt_version": PROMPT_VERSION, "at": now(), "requests_spent": spent,
           "summarised": len(results), "deferred": len(deferred), "skipped": len(skipped),
           "documents": results, "deferred_files": deferred, "skipped_files": skipped}
    if not dry_run or results:
        os.makedirs(lane, exist_ok=True)
        with open(os.path.join(lane, "manifest_precis.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    return doc


def main(argv=None):
    utf8_streams()

    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("--pack", required=True, help="a finished fetch+normalize directory")
    ap.add_argument("--provider", default=os.environ.get("PRECIS_PROVIDER", DEFAULT_PROVIDER),
                    choices=sorted(PROVIDERS))
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="parallel provider calls (1 = strictly sequential)")
    ap.add_argument("--interval", type=float, default=MIN_REQUEST_INTERVAL,
                    help="minimum seconds between request starts, whichever worker asks")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be summarised; call nothing")
    args = ap.parse_args(argv)

    _, _, env_var = PROVIDERS[args.provider]
    api_key = os.environ.get(env_var)
    if not api_key and not args.dry_run:
        print("%s is not set. This lane is optional: without it the pack is complete, its "
              "documents simply arrive without an index." % env_var, file=sys.stderr)
        return 3

    doc = run(args.pack, model=args.model, api_key=api_key, provider=args.provider,
              limit=args.max_files, workers=args.workers, interval=args.interval,
              dry_run=args.dry_run)
    say("precis · %d summarised (%d calls) · %d deferred · %d skipped"
          % (doc["summarised"], doc["requests_spent"], doc["deferred"], doc["skipped"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
