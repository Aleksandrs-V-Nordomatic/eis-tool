#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Archives inside archives, and Latvian signed containers.

Buyers ship the specification inside a ZIP, and in Latvia they sign it: a `.edoc` is an
ASiC-E container — a ZIP holding the documents plus XAdES signature parts. They nest, and
they nested deeper than anyone expected: an earlier depth limit of 3 left three real
documents unread inside `7z > zip > edoc > edoc`, which is why MAX_DEPTH is 6.

Treating an archive as opaque is the worst outcome available, because a tender then looks
documented and unread at the same time — and that state is invisible. These tests hold the
two halves of the promise: the innermost document is reached, and the container's own
plumbing is not mistaken for content.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import normalize


def docx_bytes(text):
    """A real .docx, built by the library that writes them — a hand-rolled one is not a
    fixture but a second bug waiting to be blamed on the code under test."""
    from docx import Document
    document = Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def edoc_bytes(members):
    """An ASiC-E container: the reserved `mimetype`, the payload, and a signature part."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.etsi.asic-e+zip")
        for name, blob in members.items():
            z.writestr(name, blob)
        z.writestr("META-INF/signatures001.xml", "<XAdESSignatures>signed</XAdESSignatures>")
    return buf.getvalue()


class NestedContainers(unittest.TestCase):
    """zip > zip > edoc > docx, and zip > zip > edoc > edoc > docx."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="eis_nest_")
        os.makedirs(os.path.join(cls.root, "documents", "current"))

        inner_edoc = edoc_bytes({"Piel5.docx": docx_bytes("PIELIKUMS ventilacijas apjomi")})
        signed = edoc_bytes({"Spec.docx": docx_bytes("SPECIFIKACIJA automatizacija")})
        matroshka = edoc_bytes({"In.edoc": inner_edoc})

        middle = io.BytesIO()
        with zipfile.ZipFile(middle, "w") as z:
            z.writestr("Dok.edoc", signed)
            z.writestr("Matr.edoc", matroshka)

        pack = os.path.join(cls.root, "documents", "current", "Pack.zip")
        with zipfile.ZipFile(pack, "w") as z:
            z.writestr("in.zip", middle.getvalue())

        with open(os.path.join(cls.root, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": 2, "procurement_id": "TEST", "documents": [
                {"id": 1, "title": "Dokumentācija", "section": "current",
                 "files": [{"filename": "Pack.zip", "path": "documents/current/Pack.zip",
                            "size": os.path.getsize(pack), "sha256": "z" * 64}]}]}, fh)

        out = os.path.join(cls.root, "normalized")
        normalize.main(["--in", cls.root, "--out", out])
        with open(os.path.join(out, "manifest_normalized.json"), encoding="utf-8") as fh:
            cls.manifest = json.load(fh)
        cls.by_kind = {}
        for e in cls.manifest["documents"]:
            cls.by_kind.setdefault(e["kind"], []).append(e["source"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_a_document_three_containers_deep_is_read(self):
        # zip > zip > edoc > docx
        self.assertTrue(any(s.endswith("Dok.edoc/Spec.docx") for s in self.by_kind["markdown"]),
                        self.by_kind)

    def test_a_document_inside_an_edoc_inside_an_edoc_is_read(self):
        # The shape that went unread at depth 3 and cost three real documents.
        self.assertTrue(any(s.endswith("Matr.edoc/In.edoc/Piel5.docx")
                            for s in self.by_kind["markdown"]), self.by_kind)

    def test_the_text_actually_survives_the_journey(self):
        # Reaching the file is not reading it. An empty markdown at depth four would pass
        # every structural assertion above and still be a silent loss.
        self.assertGreater(self.manifest["chars"], 40)

    def test_container_plumbing_is_not_offered_as_a_document(self):
        # Two real documents produced seven entries before this rule: five were `mimetype`
        # and XAdES signature parts, which would pad a reading packet with signature XML.
        self.assertEqual(self.manifest["markdown"], 2)
        for source in self.by_kind.get("packaging", []):
            self.assertTrue(source.endswith("mimetype") or "META-INF/" in source, source)

    def test_the_plumbing_is_still_listed_rather_than_dropped(self):
        # "Nothing vanishes" is the claim this file exists to keep, and it applies to the
        # parts we decline to read as much as to the ones we cannot. Three containers
        # (Dok.edoc, Matr.edoc, the In.edoc inside it), each contributing its `mimetype`
        # and its signature part.
        self.assertEqual(len(self.by_kind.get("packaging", [])), 6, self.by_kind)

    def test_nothing_was_reported_unreadable(self):
        self.assertEqual(self.manifest["unsupported"], 0)
        self.assertEqual(self.manifest.get("unreadable_files"), [])


class ContainerParts(unittest.TestCase):
    def test_only_the_formats_own_reserved_names_count_as_plumbing(self):
        self.assertTrue(normalize.is_container_part("mimetype"))
        self.assertTrue(normalize.is_container_part("META-INF/signatures001.xml"))
        # A document is never plumbing because of where it sits or what it is called.
        self.assertFalse(normalize.is_container_part("Nolikums.docx"))
        self.assertFalse(normalize.is_container_part("pielikumi/mimetype.docx"))
        self.assertFalse(normalize.is_container_part("apjomi/META-INF-saraksts.xlsx"))


class ScanInsideAnArchive(unittest.TestCase):
    """The file the scan lane exists for, in the place it usually hides.

    Most files reported as scans live inside archives, and `normalize` deletes its unpacking
    scratch when it finishes — so without `--keep-unpacked` there is nothing left to send,
    and the lane can reach only the minority that arrived as their own download. A gap the
    lane cannot act on is a gap that stays open forever.
    """

    def _pack(self, keep):
        root = tempfile.mkdtemp(prefix="eis_scan_")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "documents", "current"))
        # A PDF with no text layer: what a scanned drawing looks like to the extractor.
        scan = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
        inner = edoc_bytes({"Skenets.pdf": scan})
        pack = os.path.join(root, "documents", "current", "Pack.zip")
        with zipfile.ZipFile(pack, "w") as z:
            z.writestr("in.edoc", inner)
        with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": 2, "procurement_id": "T", "documents": [
                {"id": 1, "title": "D", "section": "current",
                 "files": [{"filename": "Pack.zip", "path": "documents/current/Pack.zip",
                            "size": os.path.getsize(pack), "sha256": "z" * 64}]}]}, fh)
        out = os.path.join(root, "normalized")
        argv = ["--in", root, "--out", out] + (["--keep-unpacked"] if keep else [])
        normalize.main(argv)
        with open(os.path.join(out, "manifest_normalized.json"), encoding="utf-8") as fh:
            return root, json.load(fh)

    def test_the_scan_is_reported_as_a_gap_either_way(self):
        for keep in (False, True):
            _, manifest = self._pack(keep)
            self.assertEqual(len(manifest["unreadable_files"]), 1, keep)

    def test_without_the_flag_the_bytes_are_gone_and_the_gap_has_no_path(self):
        _, manifest = self._pack(keep=False)
        self.assertNotIn("path", manifest["unreadable_files"][0])

    def test_with_the_flag_the_gap_points_at_a_file_that_exists(self):
        root, manifest = self._pack(keep=True)
        gap = manifest["unreadable_files"][0]
        self.assertIn("path", gap)
        self.assertTrue(os.path.exists(os.path.join(root, gap["path"])), gap["path"])


if __name__ == "__main__":
    unittest.main()
