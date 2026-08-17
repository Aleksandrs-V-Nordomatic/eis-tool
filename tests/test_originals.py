#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The originals archive: everything downloaded, each thing once.

Found on a live run rather than by reading: fetching procurement 174527 printed eight
`UserWarning: Duplicate name` lines and produced an archive where 8 of 21 documents were
stored twice — 1.1 MB of 4.9 MB. EIS attaches one document to several records routinely
(a specification listed under both the notice and its amendment), and the assembly walked
records rather than files.

Nothing failed. `zipfile` writes duplicate names happily, `testzip` passes, and the sha256
is stable — so the archive was reproducibly wrong, which is the kind of wrong a test has to
catch because nothing else will.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import warnings
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eis_fetch


class Originals(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="eis_orig_")
        self.addCleanup(shutil.rmtree, self.out, True)
        os.makedirs(os.path.join(self.out, "documents", "actual"))
        for name, body in (("spec.docx", b"SPEC" * 400), ("form.docx", b"FORM" * 400),
                           ("only.docx", b"ONLY" * 400)):
            with open(os.path.join(self.out, "documents", "actual", name), "wb") as fh:
                fh.write(body)
        with open(os.path.join(self.out, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": 2}, fh)

        # The shape EIS really serves: two records, and two of the three files hang off both.
        self.manifest = [
            {"record": "8353386", "files": [
                {"path": "documents/actual/spec.docx", "size": 1600},
                {"path": "documents/actual/form.docx", "size": 1600}]},
            {"record": "8359416", "files": [
                {"path": "documents/actual/spec.docx", "size": 1600},
                {"path": "documents/actual/form.docx", "size": 1600},
                {"path": "documents/actual/only.docx", "size": 1600}]},
        ]

    def build(self):
        return eis_fetch.write_originals(self.out, "174527", self.manifest)

    def test_a_file_on_two_records_is_stored_once(self):
        with zipfile.ZipFile(self.build()) as z:
            names = z.namelist()
        self.assertEqual(sorted(names),
                         ["documents/actual/form.docx", "documents/actual/only.docx",
                          "documents/actual/spec.docx", "manifest.json"])
        self.assertEqual(len(names), len(set(names)))

    def test_nothing_downloaded_is_left_out(self):
        # The other half of the promise: deduplicating must not become dropping.
        with zipfile.ZipFile(self.build()) as z:
            names = set(z.namelist())
        for rec in self.manifest:
            for f in rec["files"]:
                self.assertIn(f["path"], names)

    def test_assembling_it_warns_about_nothing(self):
        # The duplicate names were reported for months, to stderr, where a batch run buries
        # them under the tender it is fetching. A warning nobody reads is not a report.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.build()
        self.assertEqual([str(w.message) for w in caught], [])

    def test_the_archive_stores_unique_paths_plus_the_manifest(self):
        # summary.json carries both counts side by side: `files` counts references — what
        # the run downloaded, a shared document once per record — and `unique_files` counts
        # paths, which is what this archive stores. The relation an auditor will reach for
        # is pinned here: members are the unique paths plus manifest.json, nothing else.
        with zipfile.ZipFile(self.build()) as z:
            members = z.namelist()
        unique = {f["path"] for r in self.manifest for f in r["files"]}
        self.assertEqual(len(members), len(unique) + 1)

    def test_the_manifest_still_records_both_records(self):
        # The repetition is information and stays in the manifest; only the bytes are
        # deduplicated. A consumer must still be able to see which records named the file.
        holders = [r["record"] for r in self.manifest
                   if any(f["path"] == "documents/actual/spec.docx" for f in r["files"])]
        self.assertEqual(holders, ["8353386", "8359416"])


if __name__ == "__main__":
    unittest.main()
