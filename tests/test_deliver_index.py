#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The delivered index, tested without a drive.

The index exists because a reader has one context window and a day of extracted text is
tens of millions of characters. That argument does not stop at the shard: a reader
opening one tender should not
pay 4k-30k tokens of index about the other fifty-seven to find its own entry. So every
tender carries its own copy, and these tests hold the two properties that make it safe —
it says the same thing the shard index says about that tender and nothing about any
other, and it is written after the documents it names.
"""

import io
import json
import os
import shutil
import sys
import zipfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deliver_graph


def pack(root, pid, doc_name, text):
    """A finished pack, small but the real shape: one document, manifest, procurement."""
    p = os.path.join(root, pid)
    stem = doc_name.replace(".", "_")
    os.makedirs(os.path.join(p, "normalized", stem))
    with open(os.path.join(p, "normalized", stem, "document.md"), "w",
              encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(p, "normalized", "manifest_normalized.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"documents": [{"source": "docs/%s" % doc_name,
                                  "markdown_path": "%s/document.md" % stem,
                                  "markdown_chars": len(text),
                                  "section": "actual",
                                  "record_title": "Nolikums %s" % pid,
                                  "original_sha256": "0" * 64}],
                   "unreadable_files": []}, fh)
    with open(os.path.join(p, "procurement.json"), "w", encoding="utf-8") as fh:
        json.dump({"title": "Tender %s" % pid, "buyer": "Buyer %s" % pid,


                   "link": "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/%s" % pid,
                   "procedure": "Atklāts konkurss", "profile": "PIL_Atklāts_konkurss",
                   "work_kind": "Būvdarbi"},
                  fh)
    return p


def archive_entries(blob):
    """A delivered tender ZIP as {portable path: bytes}."""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}




class ChunkedUpload(unittest.TestCase):
    class Response(object):
        def __init__(self, body=b"{}", status=200):
            self.body = body
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body

    def test_large_files_are_sent_in_ordered_ranges(self):
        old_open = deliver_graph.urllib.request.urlopen
        old_limit = deliver_graph.SIMPLE_UPLOAD_LIMIT
        old_chunk = deliver_graph.UPLOAD_CHUNK
        ranges, chunks = [], []

        def fake_open(req, timeout=None):
            if req.full_url.endswith("createUploadSession"):
                return self.Response(b'{"uploadUrl":"https://upload.example/session"}')
            ranges.append(req.get_header("Content-range"))
            chunks.append(req.data)
            return self.Response(status=201 if len(chunks) == 3 else 202)

        deliver_graph.urllib.request.urlopen = fake_open
        deliver_graph.SIMPLE_UPLOAD_LIMIT = 1
        deliver_graph.UPLOAD_CHUNK = 4
        try:
            deliver_graph.upload("drive", "day/shards.zip", b"abcdefghij", "token")
        finally:
            deliver_graph.urllib.request.urlopen = old_open
            deliver_graph.SIMPLE_UPLOAD_LIMIT = old_limit
            deliver_graph.UPLOAD_CHUNK = old_chunk

        self.assertEqual(ranges, ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"])
        self.assertEqual(chunks, [b"abcd", b"efgh", b"ij"])
class DeliveredStructure(unittest.TestCase):
    """The Word-numbering sidecar arrives as one file per tender, not one per document."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="eis_deliver_struct_")
        p = pack(self.root, "111", "nolikums.docx", "clause text")
        with open(os.path.join(p, "normalized", "nolikums_docx", "structure.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"schema": "structure/1", "paragraphs": [{"index": 0, "numId": "7"}]}, fh)
        self.sent = []
        self._token, self._upload = deliver_graph.token, deliver_graph.upload
        deliver_graph.token = lambda *a: "t"
        deliver_graph.upload = lambda drive, dest, data, tok: self.sent.append((dest, data))
        for k in ("GRAPH_DRIVE_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
            os.environ[k] = "x"
        # Shaped like a real one, because the delivery now refuses a tenant id that is not
        # — a live run died on a value that was some other secret entirely.
        os.environ["GRAPH_TENANT_ID"] = "3b1f0a64-9c2e-4d5a-8f70-1e2d3c4b5a69"
        os.environ["GRAPH_DEST_ROOT"] = "dest"
        deliver_graph.main(["--packs", self.root, "--shard", "1", "--date", "2026-08-11"])
        self.paths = [d for d, _ in self.sent]
        self.base = "dest/2026-08-11/shards/eis-batch-shard-1"

    def tearDown(self):
        deliver_graph.token, deliver_graph.upload = self._token, self._upload
        shutil.rmtree(self.root, ignore_errors=True)

    def entries(self, pid="111", shard="1"):
        base = self.base.rsplit("-", 1)[0] + "-" + shard
        return archive_entries(dict(self.sent)["%s/%s.zip" % (base, pid)])

    def test_one_merged_sidecar_lands_inside_the_tender_archive(self):
        self.assertIn("structure.json", self.entries())

    def test_no_sidecar_is_kept_beside_its_document(self):
        beside = [p for p in self.entries()
                  if p.endswith("structure.json") and "/normalized/" in "/" + p]
        self.assertEqual([], beside)

    def test_it_is_keyed_by_the_name_a_reader_actually_sees(self):
        entries = self.entries()
        body = json.loads(entries["structure.json"].decode("utf-8"))
        self.assertEqual(body["pid"], "111")
        self.assertEqual(list(body["documents"]), ["normalized/n/0000.md"])
        self.assertEqual(body["documents"]["normalized/n/0000.md"]["paragraphs"][0]["numId"],
                         "7")
        index = json.loads(entries["index.json"].decode("utf-8"))
        self.assertEqual([d["path"] for d in index["documents"]], list(body["documents"]))

    def test_a_tender_without_word_numbering_gets_no_sidecar(self):
        other = tempfile.mkdtemp(prefix="eis_deliver_plain_")
        try:
            pack(other, "222", "spec.pdf", "no numbering here")
            self.sent[:] = []
            deliver_graph.main(["--packs", other, "--shard", "2", "--date", "2026-08-11"])
            self.assertNotIn("structure.json", self.entries("222", "2"))
        finally:
            shutil.rmtree(other, ignore_errors=True)


class DeliveredIndex(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="eis_deliver_")
        pack(self.root, "111", "nolikums.pdf", "first tender text")
        pack(self.root, "222", "tehniska.pdf", "second tender text")
        with open(os.path.join(self.root, "done.txt"), "w", encoding="utf-8") as fh:
            fh.write("111\n222\n")

        self.sent = []
        self._token, self._upload = deliver_graph.token, deliver_graph.upload
        deliver_graph.token = lambda *a: "t"
        deliver_graph.upload = lambda drive, dest, data, tok: self.sent.append((dest, data))
        for k in ("GRAPH_DRIVE_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
            os.environ[k] = "x"
        os.environ["GRAPH_TENANT_ID"] = "3b1f0a64-9c2e-4d5a-8f70-1e2d3c4b5a69"
        os.environ["GRAPH_DEST_ROOT"] = "dest"

    def tearDown(self):
        deliver_graph.token, deliver_graph.upload = self._token, self._upload
        shutil.rmtree(self.root, ignore_errors=True)

    def deliver(self):
        deliver_graph.main(["--packs", self.root, "--shard", "1", "--date", "2026-08-11"])
        return [d for d, _ in self.sent]

    def body(self, dest):
        return json.loads(dict(self.sent)[dest].decode("utf-8"))

    def own(self, pid="111"):
        base = "dest/2026-08-11/shards/eis-batch-shard-1"
        return self.body("%s/%s.index.json" % (base, pid))

    def archived(self, pid="111"):
        base = "dest/2026-08-11/shards/eis-batch-shard-1"
        return archive_entries(dict(self.sent)["%s/%s.zip" % (base, pid)])

    def test_every_tender_carries_its_own_index(self):
        paths = self.deliver()
        for pid in ("111", "222"):
            base = "dest/2026-08-11/shards/eis-batch-shard-1"
            self.assertIn("%s/%s.zip" % (base, pid), paths)
            self.assertIn("%s/%s.index.json" % (base, pid), paths)

    def test_the_tenders_copy_says_what_the_shard_index_says(self):
        self.deliver()
        base = "dest/2026-08-11/shards/eis-batch-shard-1"
        own = self.own()
        line = [t for t in self.body("%s/index.json" % base)["tenders"]
                if t["pid"] == "111"][0]
        inside = json.loads(self.archived()["index.json"].decode("utf-8"))
        self.assertEqual(own, inside)
        for field in ("pid", "key", "title", "buyer", "link", "documents", "unreadable"):
            self.assertEqual(own[field], line[field])
        # It is opened alone, so it names the day and the shard it came from.
        self.assertEqual((own["date"], own["shard"]), ("2026-08-11", "1"))

    def test_how_it_is_bought_and_what_is_bought_travel(self):
        # A person filtering on these is quoting the buyer rather than trusting a
        # judgement, which is why they are carried at all.
        self.deliver()
        own = self.own()
        self.assertEqual(own["procedure"], "Atklāts konkurss")
        self.assertEqual(own["work_kind"], "Būvdarbi")

    def test_the_profile_code_travels_because_it_does_not_translate(self):
        # EIS serves some pages in English, and the same field then reads "Construction works"
        # where another tender reads "Būvdarbi". A column keyed on the display string grows
        # two labels for one thing; the profile code does not.
        self.deliver()
        own = self.own()
        self.assertEqual(own["profile"], "PIL_Atklāts_konkurss")

    def test_a_tenders_copy_knows_nothing_of_its_neighbours(self):
        self.deliver()
        own = self.own()
        self.assertEqual([d["name"] for d in own["documents"]], ["nolikums.pdf"])
        self.assertNotIn("222", json.dumps(own))

    def test_archives_and_indexes_are_written_in_proof_order(self):
        paths = self.deliver()
        base = "dest/2026-08-11/shards/eis-batch-shard-1"
        for pid in ("111", "222"):
            archive_path = "%s/%s.zip" % (base, pid)
            index_path = "%s/%s.index.json" % (base, pid)
            self.assertLess(paths.index(archive_path), paths.index(index_path))
            blob = dict(self.sent)[archive_path]
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                self.assertEqual(zf.namelist()[-1], "index.json")

        self.assertEqual(paths[-1], "%s/index.json" % base)

    def test_each_tender_is_delivered_as_a_folder_as_well_as_an_archive(self):
        # A reader that wants one document should not have to take the whole tender to get
        # it, and a reader that wants the tender whole should not have to walk a folder.
        # Both are published, so neither has to.
        paths = self.deliver()
        base = "dest/2026-08-11/shards/eis-batch-shard-1"
        for pid in ("111", "222"):
            in_folder = [p for p in paths if p.startswith("%s/%s/" % (base, pid))]
            self.assertTrue(in_folder, "no folder delivered for %s" % pid)
            self.assertIn("%s/%s/index.json" % (base, pid), in_folder)

    def test_the_folder_and_the_archive_hold_exactly_the_same_members(self):
        # They are two renderings of one list. If they ever diverge, a reader comparing the
        # folder against the archive finds a file in one and not the other, with nothing
        # saying which is right — so this is the property worth pinning, not the contents.
        paths = self.deliver()
        base = "dest/2026-08-11/shards/eis-batch-shard-1"
        for pid in ("111", "222"):
            prefix = "%s/%s/" % (base, pid)
            folder = {p[len(prefix):]: b for p, b in self.sent if p.startswith(prefix)}
            archived = archive_entries(dict(self.sent)["%s/%s.zip" % (base, pid)])
            self.assertEqual(sorted(folder), sorted(archived))
            for name in folder:
                self.assertEqual(folder[name], archived[name], name)

    def test_the_folders_index_is_written_after_the_files_it_names(self):
        paths = self.deliver()
        base = "dest/2026-08-11/shards/eis-batch-shard-1"
        for pid in ("111", "222"):
            prefix = "%s/%s/" % (base, pid)
            in_folder = [p for p in paths if p.startswith(prefix)]
            self.assertEqual(in_folder[-1], prefix + "index.json")


if __name__ == "__main__":
    unittest.main()
