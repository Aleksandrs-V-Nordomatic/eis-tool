#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The précis lane, tested without a provider.

A day runs to millions of characters across thousands of documents, so something has to
say what each file is before anything decides what to read. These tests hold the properties
that make that safe: it never touches the deterministic text, it is paid for once per
distinct file, and it is stamped so no consumer can mistake it for evidence.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import precis


def pack_with(documents):
    """A finished normalize output, with markdown on disk for each readable document."""
    root = tempfile.mkdtemp(prefix="eis_precis_")
    norm = os.path.join(root, "normalized")
    os.makedirs(norm)
    entries = []
    for name, digest, text, extra in documents:
        rel = "%s/document.md" % name.replace(".", "_")
        path = os.path.join(norm, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        entry = {"source": "current/" + name, "kind": "markdown", "markdown_path": rel,
                 "markdown_chars": len(text), "original_file": name,
                 "original_sha256": digest, "preferred_for_agent": True}
        entry.update(extra or {})
        entries.append(entry)
    with open(os.path.join(norm, "manifest_normalized.json"), "w", encoding="utf-8") as fh:
        json.dump({"schema": 2, "documents": entries}, fh)
    return root


LONG = "NOLIKUMS. Iepirkuma priekšmets ir būvdarbu veikšana objektā. " * 20


class Recorder(object):
    def __init__(self, text="Šis ir nolikums par būvdarbu veikšanu.",
                 quota_after=None, fail_on=None):
        self.text, self.quota_after, self.fail_on = text, quota_after, fail_on
        self.calls = []

    def __call__(self, name, text, model, api_key):
        self.calls.append({"name": name, "chars": len(text), "model": model})
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError("provider refused (500): boom")
        if self.quota_after is not None and len(self.calls) > self.quota_after:
            raise precis.Quota("out of free quota")
        return self.text, {"total_tokens": 120}


class Queue(unittest.TestCase):
    def test_a_real_document_is_queued(self):
        root = pack_with([("Nolikums.docx", "a" * 64, LONG, None)])
        self.addCleanup(shutil.rmtree, root, True)
        send, skip = precis.queue(precis.load_pack(root))
        self.assertEqual(len(send), 1, skip)

    def test_a_duplicate_listing_is_summarised_once(self):
        # The same file listed under a second record is one document, not two.
        root = pack_with([("A.docx", "a" * 64, LONG, None),
                          ("A.docx", "a" * 64, LONG, {"also_listed_under": 7})])
        self.addCleanup(shutil.rmtree, root, True)
        self.assertEqual(len(precis.load_pack(root)), 1)

    def test_a_document_too_short_to_need_one_is_skipped(self):
        root = pack_with([("Tiny.txt", "b" * 64, "Paraksts", None)])
        self.addCleanup(shutil.rmtree, root, True)
        send, skip = precis.queue(precis.load_pack(root))
        self.assertEqual(send, [])
        self.assertEqual(skip[0]["skipped"], "too-short-to-summarise")

    def test_the_biggest_documents_are_offered_first(self):
        # A run that hits its limit should have spent it on the documents that would
        # otherwise dominate a reading packet.
        root = pack_with([("Small.docx", "a" * 64, LONG, None),
                          ("Huge.docx", "c" * 64, LONG * 5, None)])
        self.addCleanup(shutil.rmtree, root, True)
        send, _ = precis.queue(precis.load_pack(root), limit=1)
        self.assertEqual(send[0]["original_file"], "Huge.docx")


class Run(unittest.TestCase):
    def setUp(self):
        self.pack = pack_with([("Nolikums.docx", "a" * 64, LONG, None)])
        self.addCleanup(shutil.rmtree, self.pack, True)

    def test_it_writes_a_precis_stamped_as_derived(self):
        doc = precis.run(self.pack, send_fn=Recorder(), api_key="k", workers=1, interval=0)
        self.assertEqual(doc["summarised"], 1)
        with open(os.path.join(self.pack, "precis", "a" * 64 + ".json"),
                  encoding="utf-8") as fh:
            rec = json.load(fh)
        # The field a consumer must read before trusting a word of it.
        self.assertEqual(rec["derived"], "precis")
        self.assertTrue(rec["model"] and rec["prompt_sha256"] and rec["at"])
        self.assertIn("būvdarbu", rec["precis"])

    def test_it_never_writes_into_the_deterministic_output(self):
        precis.run(self.pack, send_fn=Recorder(), api_key="k", workers=1, interval=0)
        produced = sorted(os.listdir(os.path.join(self.pack, "normalized")))
        self.assertEqual(produced, ["Nolikums_docx", "manifest_normalized.json"])

    def test_only_the_head_of_a_document_is_sent(self):
        # A tender document says what it is on its first page; sending 300k characters to
        # learn "this is the contract draft" would spend the quota this lane protects.
        big = pack_with([("Huge.docx", "d" * 64, LONG * 50, None)])
        self.addCleanup(shutil.rmtree, big, True)
        provider = Recorder()
        precis.run(big, send_fn=provider, api_key="k", workers=1, interval=0)
        self.assertLessEqual(provider.calls[0]["chars"], precis.HEAD_CHARS)

    def test_a_second_run_costs_nothing(self):
        provider = Recorder()
        precis.run(self.pack, send_fn=provider, api_key="k", workers=1, interval=0)
        again = precis.run(self.pack, send_fn=provider, api_key="k", workers=1, interval=0)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(again["requests_spent"], 0)
        self.assertEqual(again["documents"][0]["source"], "cache")

    def test_running_out_of_quota_defers_the_rest(self):
        pack = pack_with([("A.docx", "a" * 64, LONG, None),
                          ("B.docx", "b" * 64, LONG, None)])
        self.addCleanup(shutil.rmtree, pack, True)
        doc = precis.run(pack, send_fn=Recorder(quota_after=1), api_key="k", workers=1, interval=0)
        self.assertEqual(doc["summarised"], 1)
        self.assertEqual(doc["deferred"], 1)

    def test_one_provider_error_does_not_end_the_queue(self):
        pack = pack_with([("A.docx", "a" * 64, LONG, None),
                          ("B.docx", "b" * 64, LONG, None)])
        self.addCleanup(shutil.rmtree, pack, True)
        doc = precis.run(pack, send_fn=Recorder(fail_on=1), api_key="k", workers=1, interval=0)
        self.assertEqual(doc["summarised"], 1)
        self.assertTrue(any("provider-error" in s["skipped"] for s in doc["skipped_files"]))

    def test_dry_run_calls_nothing(self):
        provider = Recorder()
        doc = precis.run(self.pack, send_fn=provider, api_key="k", workers=1, interval=0, dry_run=True)
        self.assertEqual(provider.calls, [])
        self.assertEqual(doc["deferred"], 1)




class Concurrency(unittest.TestCase):
    """The calls go wide, and the quota stop still holds when they do.

    Fourteen sequential calls took about thirty seconds on a real tender — most of a small
    tender's post-processing. These requests go to a commercial API on another host, so the
    politeness that governs the EIS downloader has nothing to say about them.
    """

    def _pack(self, n):
        docs = [("D%02d.docx" % i, ("%02d" % i) * 32, LONG, None) for i in range(n)]
        root = pack_with(docs)
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def test_calls_actually_overlap(self):
        import threading
        live, peak, lock = [0], [0], threading.Lock()

        def slow(name, text, model, api_key):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            try:
                import time as t
                # Long enough that four workers cannot possibly take turns.
                t.sleep(0.25)
                return "précis", {}
            finally:
                with lock:
                    live[0] -= 1

        precis.run(self._pack(8), send_fn=slow, api_key="k", workers=4, interval=0)
        self.assertGreater(peak[0], 1, "no two calls were ever in flight together")

    def test_one_worker_is_still_strictly_sequential(self):
        import threading
        live, peak, lock = [0], [0], threading.Lock()

        def slow(name, text, model, api_key):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            try:
                import time as t
                t.sleep(0.01)
                return "précis", {}
            finally:
                with lock:
                    live[0] -= 1

        precis.run(self._pack(4), send_fn=slow, api_key="k", workers=1, interval=0)
        self.assertEqual(peak[0], 1)

    def test_the_quota_stop_holds_across_workers(self):
        # Meeting a rate limit must end the run, not fire forty more 429s at it.
        provider = Recorder(quota_after=1)
        doc = precis.run(self._pack(10), send_fn=provider, api_key="k", workers=4, interval=0)
        self.assertLessEqual(doc["summarised"], 4, doc["summarised"])
        self.assertGreater(doc["deferred"], 0)
        self.assertEqual(doc["summarised"] + doc["deferred"] + doc["skipped"], 10)


if __name__ == "__main__":
    unittest.main()


class RateLimit(unittest.TestCase):
    """The provider publishes 0.83 requests per second; four workers would do about two.

    Concurrency is for hiding the latency of a slow answer. Staying inside somebody else's
    published budget is a separate job, and doing one must not require breaking the other.
    """

    def test_the_submission_rate_is_capped_however_many_workers_there_are(self):
        import time as t
        starts, lock = [], __import__("threading").Lock()

        def note(name, text, model, api_key):
            with lock:
                starts.append(t.monotonic())
            return "précis", {}

        docs = [("D%02d.docx" % i, ("%02d" % i) * 32, LONG, None) for i in range(6)]
        root = pack_with(docs)
        self.addCleanup(shutil.rmtree, root, True)
        precis.run(root, send_fn=note, api_key="k", workers=4, interval=0.05)

        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertEqual(len(starts), 6)
        # Every consecutive pair honours the interval, even though four workers were free.
        self.assertTrue(all(g >= 0.04 for g in gaps), gaps)

    def test_an_interval_of_zero_lets_them_all_go(self):
        provider = Recorder()
        docs = [("D%02d.docx" % i, ("%02d" % i) * 32, LONG, None) for i in range(4)]
        root = pack_with(docs)
        self.addCleanup(shutil.rmtree, root, True)
        doc = precis.run(root, send_fn=provider, api_key="k", workers=4, interval=0)
        self.assertEqual(doc["summarised"], 4)


class SharedBudget(unittest.TestCase):
    """A limiter inside one process is a lie when the process is one of four.

    Each shard politely holds 0.8 req/s of its own, and the account then sees four times
    that against a single 0.83 budget — so most of the queue comes back 429. Nothing is
    lost, because the lane defers and caches rather than failing, but nothing is summarised
    either.
    """

    def test_the_account_budget_is_divided_not_multiplied(self):
        for shards in (1, 2, 4, 8):
            each = precis.share_of_budget(shards)
            together = shards / each
            self.assertAlmostEqual(together, 1 / precis.MIN_REQUEST_INTERVAL, places=6,
                                   msg="%d shards" % shards)

    def test_a_single_caller_is_unchanged(self):
        self.assertEqual(precis.share_of_budget(1), precis.MIN_REQUEST_INTERVAL)

    def test_nonsense_shard_counts_do_not_remove_the_limit(self):
        for bad in (0, None, -3):
            self.assertGreaterEqual(precis.share_of_budget(bad), precis.MIN_REQUEST_INTERVAL)
