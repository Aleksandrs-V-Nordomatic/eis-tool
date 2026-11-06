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
        self.attrs = {}
        self.released = []
        self.lib = synced.Library(self.root, state=lambda path: self.states.get(os.path.basename(path)),
                                  attrs=self.attr_of, release=self.released.append)
        self.source = os.path.join(self.root, "source.bin")
        with open(self.source, "wb") as fh:
            fh.write(b"abc")

    def attr_of(self, path):
        if not os.path.exists(path):
            return None
        return self.attrs.get(os.path.basename(path), 0x20)

    def test_a_moved_file_is_in_the_library_folder_and_no_longer_where_it_was(self):
        self.assertEqual(self.lib.move_in("p/work/LV/inbox/b/1/a.bin", self.source), 3)
        with open(os.path.join(self.root, "p", "work", "LV", "inbox", "b", "1", "a.bin"), "rb") as fh:
            self.assertEqual(fh.read(), b"abc")
        self.assertFalse(os.path.exists(self.source))

    def test_a_file_moved_again_replaces_the_one_there(self):
        self.lib.move_in("p/a.bin", self.source)
        with open(self.source, "wb") as fh:
            fh.write(b"abcd")
        self.assertEqual(self.lib.move_in("p/a.bin", self.source), 4)
        with open(os.path.join(self.root, "p", "a.bin"), "rb") as fh:
            self.assertEqual(fh.read(), b"abcd")

    def test_a_file_with_local_bytes_is_let_go_and_named_until_it_has_gone(self):
        self.lib.upload("p/a.bin", self.source)
        self.lib.upload("p/b.bin", self.source)
        self.attrs = {"b.bin": synced.ONLY_IN_THE_CLOUD}
        self.assertEqual(self.lib.let_go(["p/a.bin", "p/b.bin", "p/missing.bin"]), ["p/a.bin"])
        self.assertEqual([os.path.basename(p) for p in self.released], ["a.bin"])
        self.attrs["a.bin"] = 0x1000
        self.assertEqual(self.lib.let_go(["p/a.bin"]), [])

    def test_only_local_bytes_are_counted_in_a_folder(self):
        self.lib.upload("p/a/one.bin", self.source)
        self.lib.upload("p/two.bin", self.source)
        self.lib.upload("p/gone.bin", self.source)
        self.attrs = {"gone.bin": 0x400000}
        self.assertEqual(self.lib.bytes_held("p"), 6)
        self.assertEqual(self.released, [])
        self.assertEqual(self.lib.let_go_of_folder("p"), 6)
        self.assertEqual(sorted(os.path.basename(p) for p in self.released), ["one.bin", "two.bin"])

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
