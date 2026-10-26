import os
import shutil
import tempfile
import unittest

import synced


class Folder(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.states = {}
        self.lib = synced.Library(self.root, state=lambda path: self.states.get(os.path.basename(path)))
        self.source = os.path.join(self.root, "source.bin")
        with open(self.source, "wb") as fh:
            fh.write(b"abc")

    def test_a_copied_file_is_in_the_library_folder(self):
        self.assertEqual(self.lib.upload("p/work/LV/inbox/b/1/a.bin", self.source), 3)
        with open(os.path.join(self.root, "p", "work", "LV", "inbox", "b", "1", "a.bin"), "rb") as fh:
            self.assertEqual(fh.read(), b"abc")
        self.assertEqual(self.lib.list_names("p/work/LV/inbox/b"), ["1"])

    def test_a_missing_file_or_folder_reads_as_nothing(self):
        self.assertIsNone(self.lib.read_text("p/kit/LV/x.json"))
        self.assertEqual(self.lib.list_names("p/work/LV/batches"), [])

    def test_only_an_in_sync_placeholder_counts_as_carried(self):
        self.states = {"done.bin": synced.PLACEHOLDER | synced.IN_SYNC, "fresh.bin": 0,
                       "partial.bin": synced.PLACEHOLDER}
        paths = ["x/done.bin", "x/fresh.bin", "x/partial.bin", "x/missing.bin"]
        self.assertEqual(self.lib.not_in_sync(paths), ["x/fresh.bin", "x/partial.bin", "x/missing.bin"])


if __name__ == "__main__":
    unittest.main()
