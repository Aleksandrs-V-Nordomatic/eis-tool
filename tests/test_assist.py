#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The fallback lane, tested without a provider.

The provider is injected, so these tests prove the parts that decide whether the lane is
safe to keep: that it only ever touches files the deterministic extractor gave up on, that
a digest already answered is never paid for twice, and that running out of free quota ends
the run cleanly instead of failing it.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import assist


def pack_with(unreadable, files):
    """A finished fetch+normalize directory, on disk, with nothing else in it."""
    root = tempfile.mkdtemp(prefix="eis_llm_")
    os.makedirs(os.path.join(root, "documents", "current"), exist_ok=True)
    os.makedirs(os.path.join(root, "normalized"), exist_ok=True)
    manifest_files = []
    for name, digest, content in files:
        rel = "documents/current/" + name
        with open(os.path.join(root, rel.replace("/", os.sep)), "wb") as fh:
            fh.write(content)
        manifest_files.append({"filename": name, "path": rel, "sha256": digest,
                               "size": len(content)})
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"procurement_id": "1", "documents": [
            {"id": 1, "title": "Nolikums", "files": manifest_files}]}, fh)
    with open(os.path.join(root, "normalized", "manifest_normalized.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"schema": 2, "unreadable_files": unreadable}, fh)
    return root


def scan(name="skenets.pdf", digest="a" * 64, size=1000):
    return {"file": name, "bytes": size, "sha256": digest,
            "reason": "no text layer — a scan or a vector drawing"}


class Recorder(object):
    """A provider that answers, counts, and can be told to run out of quota."""

    def __init__(self, text="Nolikums\n\n1. Prasības", quota_after=None, fail_on=None):
        self.text, self.quota_after, self.fail_on = text, quota_after, fail_on
        self.calls = []

    def __call__(self, blob, mime, model, api_key):
        self.calls.append({"bytes": len(blob), "mime": mime, "model": model})
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError("provider refused (500): boom")
        if self.quota_after is not None and len(self.calls) > self.quota_after:
            raise assist.Quota("out of free quota")
        return self.text, {"totalTokenCount": 42}


class Queue(unittest.TestCase):
    def _queue(self, unreadable, files=None, **kw):
        root = pack_with(unreadable, files if files is not None
                         else [("skenets.pdf", "a" * 64, b"%PDF-1.4 scan")])
        self.addCleanup(shutil.rmtree, root, True)
        entries, by_digest = assist.load_pack(root)
        return assist.queue(entries, by_digest, **kw)

    def test_a_scan_with_a_retained_source_is_sent(self):
        send, _ = self._queue([scan()])
        self.assertEqual([e["file"] for e in send], ["skenets.pdf"])
        self.assertEqual(send[0]["path"], "documents/current/skenets.pdf")

    def test_a_drawing_is_never_sent(self):
        # DWG is a named non-goal: it is not prose, and a model asked to read one invents.
        send, skip = self._queue([dict(scan(name="plans.dwg"))])
        self.assertEqual(send, [])
        self.assertEqual(skip[0]["skipped"], "drawing-not-sent")

    def test_a_file_the_extractor_read_is_not_in_the_queue_at_all(self):
        # Nothing readable ever reaches this lane — the queue is built only from the
        # extractor's own list of files it gave up on.
        send, _ = self._queue([])
        self.assertEqual(send, [])

    def test_other_reasons_are_left_alone_unless_asked_for(self):
        other = dict(scan(), reason="no deterministic text extraction for .dgn")
        send, skip = self._queue([other])
        self.assertEqual(send, [])
        self.assertEqual(skip[0]["skipped"], "reason-not-selected")
        send, _ = self._queue([other], reasons=("no deterministic text",))
        self.assertEqual(len(send), 1)

    def test_an_archive_member_says_why_it_cannot_be_sent(self):
        # normalize deletes its unpacking scratch, so a scan found inside a 7z has no
        # original left to send. A silent shortfall here would look like coverage.
        send, skip = self._queue([scan(digest="b" * 64)])
        self.assertEqual(send, [])
        self.assertEqual(skip[0]["skipped"], "source-not-retained")

    def test_a_file_too_large_to_inline_is_named_not_dropped(self):
        send, skip = self._queue([scan(size=assist.INLINE_LIMIT + 1)])
        self.assertEqual(send, [])
        self.assertEqual(skip[0]["skipped"], "too-large-for-inline-upload")

    def test_the_run_limit_defers_the_rest_visibly(self):
        files = [("a.pdf", "a" * 64, b"x"), ("b.pdf", "c" * 64, b"y")]
        send, skip = self._queue([scan(), scan(name="b.pdf", digest="c" * 64)],
                                 files=files, limit=1)
        self.assertEqual(len(send), 1)
        self.assertEqual(skip[0]["skipped"], "over-the-run-limit")


class Run(unittest.TestCase):
    def setUp(self):
        self.pack = pack_with([scan()], [("skenets.pdf", "a" * 64, b"%PDF-1.4 scan")])
        self.addCleanup(shutil.rmtree, self.pack, True)

    def test_the_transcription_and_its_provenance_land_beside_each_other(self):
        provider = Recorder()
        doc = assist.run(self.pack, send_fn=provider, api_key="k", pace=0)
        self.assertEqual(doc["read"], 1)
        with open(os.path.join(self.pack, "llm", "a" * 64 + ".md"), encoding="utf-8") as fh:
            self.assertIn("Prasības", fh.read())
        with open(os.path.join(self.pack, "llm", "a" * 64 + ".provenance.json"),
                  encoding="utf-8") as fh:
            prov = json.load(fh)
        # Which reader produced it is asserted in Providers below; what matters here is
        # that the text never arrives without a record saying it is a fallback at all.
        self.assertTrue(prov["extraction"].endswith("-fallback"))
        self.assertEqual(prov["source_sha256"], "a" * 64)
        self.assertTrue(prov["provider"] and prov["prompt_sha256"] and prov["at"])

    def test_nothing_is_written_into_the_deterministic_output(self):
        assist.run(self.pack, send_fn=Recorder(), api_key="k", pace=0)
        produced = os.listdir(os.path.join(self.pack, "normalized"))
        self.assertEqual(produced, ["manifest_normalized.json"])

    def test_a_second_run_costs_nothing(self):
        provider = Recorder()
        assist.run(self.pack, send_fn=provider, api_key="k", pace=0)
        again = assist.run(self.pack, send_fn=provider, api_key="k", pace=0)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(again["requests_spent"], 0)
        self.assertEqual(again["documents"][0]["source"], "cache")

    def test_dry_run_sends_nothing(self):
        provider = Recorder()
        doc = assist.run(self.pack, send_fn=provider, api_key="k", pace=0, dry_run=True)
        self.assertEqual(provider.calls, [])
        self.assertEqual(doc["deferred"], 1)

    def test_running_out_of_quota_defers_instead_of_failing(self):
        pack = pack_with([scan(), scan(name="b.pdf", digest="c" * 64)],
                         [("skenets.pdf", "a" * 64, b"x"), ("b.pdf", "c" * 64, b"y")])
        self.addCleanup(shutil.rmtree, pack, True)
        doc = assist.run(pack, send_fn=Recorder(quota_after=1), api_key="k", pace=0)
        self.assertEqual(doc["read"], 1)
        self.assertEqual(doc["deferred"], 1)
        self.assertIn("quota", doc["deferred_files"][0]["deferred"])

    def test_one_provider_error_does_not_end_the_queue(self):
        pack = pack_with([scan(), scan(name="b.pdf", digest="c" * 64)],
                         [("skenets.pdf", "a" * 64, b"x"), ("b.pdf", "c" * 64, b"y")])
        self.addCleanup(shutil.rmtree, pack, True)
        doc = assist.run(pack, send_fn=Recorder(fail_on=1), api_key="k", pace=0)
        self.assertEqual(doc["read"], 1)
        self.assertTrue(any("provider-error" in s["skipped"] for s in doc["skipped_files"]))


class Providers(unittest.TestCase):
    """The seam that makes swapping readers a configuration change, not a code change."""

    def setUp(self):
        self.pack = pack_with([scan()], [("skenets.pdf", "a" * 64, b"%PDF-1.4 scan")])
        self.addCleanup(shutil.rmtree, self.pack, True)

    def test_the_default_reader_needs_no_key(self):
        # The whole point: the pipeline must not depend on one person's account.
        _, needs_key, _, _ = assist.PROVIDERS[assist.DEFAULT_PROVIDER]
        self.assertFalse(needs_key)
        self.assertEqual(assist.DEFAULT_PROVIDER, "tesseract")

    def test_a_provider_that_needs_a_key_refuses_to_run_without_one(self):
        with self.assertRaises(RuntimeError):
            assist.run(self.pack, provider="gemini", api_key=None, pace=0)

    def test_the_provider_is_recorded_so_you_can_tell_who_read_it(self):
        doc = assist.run(self.pack, send_fn=Recorder(), api_key="k", pace=0,
                         provider="gemini")
        self.assertEqual(doc["provider"], "gemini")
        self.assertEqual(doc["extraction"], "llm-fallback")
        with open(os.path.join(self.pack, "llm", "a" * 64 + ".provenance.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["provider"], "gemini")

    def test_local_ocr_is_labelled_ocr_and_not_llm(self):
        # Downstream decides how much to trust the text by this field, so a machine that
        # read pixels must not be filed under the same word as a model that read pixels.
        doc = assist.run(self.pack, send_fn=Recorder(), pace=0, provider="tesseract")
        self.assertEqual(doc["extraction"], "ocr-fallback")

    def test_the_size_ceiling_belongs_to_the_reader(self):
        # A hosted model has a request limit; the local one does not, so a big scan that
        # a model must refuse is still readable on the runner.
        big = scan(size=assist.INLINE_LIMIT + 1)
        entries, by_digest = assist.load_pack(self.pack)
        entries = [big]
        by_digest = {big["sha256"]: "documents/current/skenets.pdf"}
        send, skip = assist.queue(entries, by_digest, size_limit=assist.INLINE_LIMIT)
        self.assertEqual(skip[0]["skipped"], "too-large-for-inline-upload")
        send, skip = assist.queue(entries, by_digest, size_limit=None)
        self.assertEqual(len(send), 1)


class LocalOcr(unittest.TestCase):
    def test_it_says_plainly_when_tesseract_is_missing(self):
        # A machine without the binary must get a sentence it can act on, not a traceback
        # from somewhere inside a subprocess call.
        import shutil as sh
        if sh.which("tesseract"):
            self.skipTest("tesseract is installed here; the missing-binary path needs a box without it")
        with self.assertRaises(RuntimeError) as caught:
            assist.tesseract_read(b"%PDF-1.4", "application/pdf", None, None)
        self.assertIn("tesseract", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()


class ScannedImages(unittest.TestCase):
    """A scan does not always arrive as a PDF.

    Day one: 26 files reported "no text layer" and another 52 were jpg/png that no decoder
    tried at all. Buyers photograph or scan a page and attach the picture. Same document,
    different wrapper — same queue.
    """

    def _queue(self, name, reason):
        digest = "a" * 64
        entry = {"file": name, "bytes": 1000, "sha256": digest, "reason": reason}
        return assist.queue([entry], {digest: "documents/current/" + name})

    def test_a_scanned_page_attached_as_an_image_is_read(self):
        for name in ("lapa.jpg", "Lapa.JPEG", "skenets.png", "fakss.tiff"):
            send, skip = self._queue(name, "no deterministic text extraction for .jpg")
            self.assertEqual(len(send), 1, (name, skip))

    def test_vector_drawings_are_still_never_sent(self):
        # Rasterising a floor plan to guess at it is worse than the honest gap.
        for name in ("plans.dwg", "shema.dxf", "rasejums.emf", "skice.wmf"):
            send, skip = self._queue(name, "no deterministic text extraction for .dwg")
            self.assertEqual(send, [], name)
            self.assertEqual(skip[0]["skipped"], "drawing-not-sent", name)

    def test_an_unrelated_format_is_still_left_alone(self):
        send, skip = self._queue("model.ifc", "no deterministic text extraction for .ifc")
        self.assertEqual(send, [])
        self.assertEqual(skip[0]["skipped"], "reason-not-selected")

    def test_a_file_found_inside_an_archive_is_reachable_by_its_path(self):
        # normalize now names where the bytes are; without that these were 20 of 26 scans.
        entry = {"file": "current/Pack/in.edoc/Skenets.pdf", "bytes": 900,
                 "sha256": "b" * 64, "reason": "no text layer — a scan",
                 "path": "normalized/_unpacked/ab12/Skenets.pdf"}
        send, skip = assist.queue([entry], {})
        self.assertEqual(len(send), 1, skip)
        self.assertEqual(send[0]["path"], "normalized/_unpacked/ab12/Skenets.pdf")
