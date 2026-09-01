#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The resume journal: what an interrupted run is allowed to keep.

The rule these tests hold: a record is skipped only when what the journal claims about it
is still true on disk. Skipping on the journal alone would let a wiped working directory
produce a manifest describing files that are not there — which is the exact shape of
failure this project refuses, arrived at from the other direction.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eis_fetch


class Journal(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="eis_journal_")
        self.addCleanup(shutil.rmtree, self.out, True)

    def _file(self, rel, content=b"12345"):
        path = os.path.join(self.out, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return {"filename": os.path.basename(rel), "path": rel,
                "size": len(content), "sha256": "x" * 64}

    def test_no_journal_is_an_empty_start_not_an_error(self):
        self.assertEqual(eis_fetch.read_journal(self.out), {})

    def test_a_record_survives_the_round_trip(self):
        entry = {"id": 111, "title": "Nolikums", "files": [self._file("documents/a.pdf")]}
        eis_fetch.append_journal(self.out, entry)
        self.assertEqual(eis_fetch.read_journal(self.out)[111]["title"], "Nolikums")

    def test_records_accumulate_across_runs(self):
        eis_fetch.append_journal(self.out, {"id": 1, "files": [self._file("documents/a")]})
        eis_fetch.append_journal(self.out, {"id": 2, "files": [self._file("documents/b")]})
        self.assertEqual(sorted(eis_fetch.read_journal(self.out)), [1, 2])

    def test_a_line_torn_in_half_by_a_kill_does_not_poison_the_journal(self):
        # The process can die mid-write. The records already flushed are still good, and
        # losing them to one broken tail would defeat the point of writing early.
        eis_fetch.append_journal(self.out, {"id": 1, "files": [self._file("documents/a")]})
        with open(os.path.join(self.out, eis_fetch.JOURNAL), "a", encoding="utf-8") as fh:
            fh.write('{"id": 2, "files": [{"pa')
        journal = eis_fetch.read_journal(self.out)
        self.assertEqual(list(journal), [1])

    def test_the_last_write_wins_for_a_repeated_record(self):
        eis_fetch.append_journal(self.out, {"id": 7, "title": "first", "files": []})
        eis_fetch.append_journal(self.out, {"id": 7, "title": "second", "files": []})
        self.assertEqual(eis_fetch.read_journal(self.out)[7]["title"], "second")


class StillOnDisk(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="eis_disk_")
        self.addCleanup(shutil.rmtree, self.out, True)

    def _entry(self, content=b"12345"):
        path = os.path.join(self.out, "documents", "a.pdf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return {"id": 1, "files": [{"path": "documents/a.pdf", "size": len(content)}]}, path

    def test_intact_files_are_kept(self):
        entry, _ = self._entry()
        self.assertTrue(eis_fetch.still_on_disk(self.out, entry))

    def test_a_deleted_file_forces_the_record_to_be_fetched_again(self):
        entry, path = self._entry()
        os.remove(path)
        self.assertFalse(eis_fetch.still_on_disk(self.out, entry))

    def test_a_truncated_file_forces_the_record_to_be_fetched_again(self):
        entry, path = self._entry()
        with open(path, "wb") as fh:
            fh.write(b"1")
        self.assertFalse(eis_fetch.still_on_disk(self.out, entry))

    def test_a_record_that_claims_no_files_is_never_trusted(self):
        # An empty file list cannot prove a record was downloaded, and treating it as done
        # would silently drop a document from the pack.
        self.assertFalse(eis_fetch.still_on_disk(self.out, {"id": 1, "files": []}))
        self.assertFalse(eis_fetch.still_on_disk(self.out, {"id": 1}))


if __name__ == "__main__":
    unittest.main()
