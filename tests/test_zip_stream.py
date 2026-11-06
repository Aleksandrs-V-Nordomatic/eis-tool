import io
import os
import struct
import unittest
import zipfile
import zlib

import zip_stream


class Unseekable(io.RawIOBase):
    """A sink the writer cannot go back in, so it writes each member's sizes after its data."""

    def __init__(self):
        self.sink = io.BytesIO()

    def writable(self):
        return True

    def seekable(self):
        return False

    def write(self, data):
        return self.sink.write(data)


def make(members, method=zipfile.ZIP_DEFLATED, stream=False):
    out = Unseekable() if stream else io.BytesIO()
    with zipfile.ZipFile(out, "w", method) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return out.sink.getvalue() if stream else out.getvalue()


def read(blob, size=7):
    chunks = (blob[i:i + size] for i in range(0, len(blob), size))
    return {name: b"".join(body) for name, body in zip_stream.members(chunks)}


class Members(unittest.TestCase):
    def test_each_member_comes_out_whole_whatever_the_chunk_size(self):
        members = {"a/spec.pdf": os.urandom(5000), "b.txt": b"hello" * 2000, "empty": b""}
        for size in (1, 7, 4096, 10 ** 6):
            self.assertEqual(read(make(members), size), members)

    def test_stored_members_come_out_too(self):
        members = {"a.bin": os.urandom(3000), "b.bin": b"x"}
        self.assertEqual(read(make(members, zipfile.ZIP_STORED)), members)

    def test_deflated_members_with_their_sizes_after_the_data(self):
        members = {"a.bin": os.urandom(3000), "b.txt": b"hello" * 2000}
        self.assertEqual(read(make(members, stream=True)), members)

    def test_data_that_looks_like_a_header_stays_data(self):
        members = {"inner.zip": make({"x": b"y"}, zipfile.ZIP_STORED), "after": b"z"}
        self.assertEqual(read(make(members, zipfile.ZIP_STORED)), members)

    def test_a_large_member_in_zip64_form(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            with z.open("big.bin", "w", force_zip64=True) as fh:
                fh.write(b"0123456789" * 1000)
            z.writestr("after.txt", b"after")
        got = read(out.getvalue())
        self.assertEqual(got["big.bin"], b"0123456789" * 1000)
        self.assertEqual(got["after.txt"], b"after")

    def test_a_member_left_unread_is_read_past(self):
        blob = make({"skip.bin": os.urandom(4000), "keep.txt": b"kept"})
        seen = {}
        for name, body in zip_stream.members([blob]):
            if name == "keep.txt":
                seen[name] = b"".join(body)
        self.assertEqual(seen, {"keep.txt": b"kept"})

    def test_an_archive_in_an_archive_reads_as_it_arrives(self):
        inner = make({"task.docx": b"task"})
        outer = make({"folder/": b"", "inner.zip": inner, "last.txt": b"last"})
        for name, body in zip_stream.members([outer[i:i + 11] for i in range(0, len(outer), 11)]):
            if name == "inner.zip":
                self.assertEqual({n: b"".join(b) for n, b in zip_stream.members(body)},
                                 {"task.docx": b"task"})

    def test_the_download_behind_it_is_read_to_its_end(self):
        blob = make({"a": b"a"})
        read_out = []

        def chunks():
            for i in range(0, len(blob), 3):
                read_out.append(1)
                yield blob[i:i + 3]
            read_out.append("end")

        list(zip_stream.members(chunks()))
        self.assertEqual(read_out[-1], "end")


class Names(unittest.TestCase):
    def test_a_utf8_name_without_the_flag_and_with_it(self):
        name = "Tehniskā specifikācija.pdf"
        self.assertEqual(zip_stream.member_name(name.encode("utf-8"), zip_stream.UTF8_NAME), name)
        self.assertEqual(zip_stream.member_name(name.encode("utf-8"), 0), name)

    def test_an_old_name_is_cp437_and_a_backslash_is_a_folder(self):
        self.assertEqual(zip_stream.member_name("mape\\é.pdf".encode("cp437"), 0), "mape/é.pdf")


class StoredOfUnknownSize(unittest.TestCase):
    def test_each_member_ends_where_its_checksum_and_size_match(self):
        members = {"a.bin": os.urandom(5000), "b.txt": b"hello", "empty": b"", "c.bin": os.urandom(70)}
        for size in (1, 3, 7, 4096, 10 ** 6):
            self.assertEqual(read(make(members, zipfile.ZIP_STORED, stream=True), size), members)

    def test_sizes_after_the_data_without_a_signature_and_in_64_bits(self):
        data = os.urandom(3000)
        crc = zlib.crc32(data)
        name = b"a.pdf"
        header = struct.pack("<4sHHHHHIIIHH", zip_stream.LOCAL_HEADER, 45, 0x808, 0, 0, 0, 0, 0, 0,
                             len(name), 0)
        trailer = struct.pack("<IQQ", crc, len(data), len(data))
        blob = header + name + data + trailer + header + b"b.pdf" + b"more"             + struct.pack("<IQQ", zlib.crc32(b"more"), 4, 4) + b"PK the central directory"
        for size in (1, 7, 4096):
            self.assertEqual(read(blob, size), {"a.pdf": data, "b.pdf": b"more"})

    def test_an_archive_stored_inside_it_is_not_mistaken_for_the_end(self):
        inner = make({"task.docx": b"task", "more.txt": b"more"}, zipfile.ZIP_STORED, stream=True)
        members = {"inner.zip": inner, "after.txt": b"after"}
        got = read(make(members, zipfile.ZIP_STORED, stream=True), 11)
        self.assertEqual(got, members)

    def test_data_holding_the_signature_stays_data(self):
        members = {"a.bin": zip_stream.DESCRIPTOR * 50 + b"tail", "b.bin": b"PK\x07"}
        self.assertEqual(read(make(members, zipfile.ZIP_STORED, stream=True), 5), members)

    def test_an_archive_cut_short(self):
        blob = make({"a": os.urandom(2000)}, zipfile.ZIP_STORED, stream=True)
        with self.assertRaises(zip_stream.NotStreamable):
            read(blob[:1000])


class NotStreamable(unittest.TestCase):
    def test_an_encrypted_member(self):
        blob = bytearray(make({"a": b"data"}))
        flags = struct.unpack_from("<H", blob, 6)[0]
        struct.pack_into("<H", blob, 6, flags | zip_stream.ENCRYPTED)
        with self.assertRaises(zip_stream.NotStreamable):
            read(bytes(blob))

    def test_a_compression_method_it_does_not_know(self):
        blob = bytearray(make({"a": b"data"}))
        struct.pack_into("<H", blob, 8, 9)     # Deflate64
        with self.assertRaises(zip_stream.NotStreamable):
            read(bytes(blob))

    def test_a_damaged_member(self):
        data = zlib.compress(b"x" * 1000)
        blob = bytearray(make({"a": os.urandom(2000) + data}, zipfile.ZIP_STORED))
        blob[60] ^= 0xFF
        with self.assertRaises(zip_stream.NotStreamable):
            read(bytes(blob))

    def test_an_archive_cut_short(self):
        blob = make({"a": os.urandom(2000)})
        with self.assertRaises(zip_stream.NotStreamable):
            read(blob[:500])

    def test_what_does_not_start_as_a_member(self):
        with self.assertRaises(zip_stream.NotStreamable):
            read(b"Rar!\x1a\x07 not a zip at all")


if __name__ == "__main__":
    unittest.main()
