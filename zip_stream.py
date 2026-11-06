"""Read a ZIP as it arrives, without the archive ever being on disk.

A ZIP can be read front to back: every member starts with its own header. EIS does not serve byte
ranges, and some archives are larger than a computer may hold, so the downloader sorts a ZIP from
the download itself. What this reader cannot follow raises NotStreamable, and the caller takes the
archive to disk if it fits: encryption, Deflate64, damaged data.
"""

import re
import struct
import zlib

LOCAL_HEADER = b"PK\x03\x04"
DESCRIPTOR = b"PK\x07\x08"
# Anything of these after a member means the members have ended.
THE_END = (b"PK\x01\x02", b"PK\x05\x06", b"PK\x06\x06", b"PK\x06\x07", b"PK\x05\x05", b"PK\x06\x08")
STORED, DEFLATED = 0, 8
ENCRYPTED, HAS_DESCRIPTOR, UTF8_NAME = 0x1, 0x8, 0x800
ZIP64_EXTRA = 1
UNKNOWN_32 = 0xFFFFFFFF
# What may stand between a stored member and the next item: its checksum and sizes, with or
# without their signature, in the 32-bit or the 64-bit form.
_NEXT_ITEM = re.compile(b"PK(?:|||)")
_TRAILERS = ("<IQQ", "<III", "<4sIQQ", "<4sIII")
_LONGEST_TRAILER = struct.calcsize("<4sIQQ") + 3


class NotStreamable(Exception):
    """This ZIP cannot be read front to back."""


class _Source:
    """The arriving bytes, with what was read too far put back."""

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.held = b""

    def _fill(self, wanted):
        while len(self.held) < wanted:
            chunk = next(self.chunks, None)
            if chunk is None:
                return
            self.held += chunk

    def peek(self, count):
        self._fill(count)
        return self.held[:count]

    def take(self, count):
        self._fill(count)
        if len(self.held) < count:
            raise NotStreamable("the archive ends inside a header")
        out, self.held = self.held[:count], self.held[count:]
        return out

    def chunk(self):
        """Whatever comes next, of any length; empty at the end."""
        self._fill(1)
        out, self.held = self.held, b""
        return out

    def give_back(self, data):
        self.held = data + self.held

    def drain(self):
        """Read to the end: the download behind it has to finish."""
        self.held = b""
        for _ in self.chunks:
            pass


def member_name(raw, flags):
    """A member's name the way the archiver meant it, with `/` as separator."""
    if flags & UTF8_NAME:
        name = raw.decode("utf-8", "replace")
    else:
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError:
            name = raw.decode("cp437")
    return name.replace("\\", "/")


def _compressed_size(extra, compressed, size):
    """The compressed size, from the extra field when the header's own says to look there."""
    at = 0
    while at + 4 <= len(extra):
        kind, length = struct.unpack_from("<HH", extra, at)
        if kind == ZIP64_EXTRA:
            values = extra[at + 4:at + 4 + length]
            if size == UNKNOWN_32 and len(values) >= 8:
                values = values[8:]
            if compressed == UNKNOWN_32 and len(values) >= 8:
                compressed = struct.unpack_from("<Q", values)[0]
            return compressed
        at += 4 + length
    return compressed


def _stored(source, compressed, crc):
    left, seen = compressed, 0
    while left:
        chunk = source.chunk()
        if not chunk:
            raise NotStreamable("the archive ends inside a member")
        if len(chunk) > left:
            source.give_back(chunk[left:])
            chunk = chunk[:left]
        left -= len(chunk)
        seen = zlib.crc32(chunk, seen)
        yield chunk
    if seen != crc:
        raise NotStreamable("a member differs from its checksum")


def _stored_of_unknown_size(source):
    """A stored member whose sizes come only after its data. The end is a signature of what comes
    next, with the checksum and the sizes before it matching what has passed. Data that merely
    looks like an ending, as an archive stored inside this one does, fails that test."""
    seen, passed, held = 0, 0, b""
    while True:
        for found in _NEXT_ITEM.finditer(held):
            for form in _TRAILERS:
                end = found.start() - struct.calcsize(form)
                if end < 0:
                    continue
                fields = struct.unpack_from(form, held, end)
                signed = len(fields) == 4
                if signed and fields[0] != DESCRIPTOR:
                    continue
                crc, compressed, size = fields[-3:]
                if compressed == size == passed + end and zlib.crc32(held[:end], seen) == crc:
                    if end:
                        yield held[:end]
                    source.give_back(held[found.start():])
                    return
        out, held = held[:-_LONGEST_TRAILER], held[-_LONGEST_TRAILER:]
        if out:
            seen = zlib.crc32(out, seen)
            passed += len(out)
            yield out
        chunk = source.chunk()
        if not chunk:
            raise NotStreamable("the archive ends inside a member")
        held += chunk


def _read_descriptor(source):
    """The checksum and sizes written after the data. Returns the checksum. The sizes take 8 bytes
    or 16, and nothing says which: what follows them has to be the next signature."""
    if source.peek(4) == DESCRIPTOR:
        source.take(4)
    crc = struct.unpack("<I", source.take(4))[0]
    for length in (8, 16):
        following = source.peek(length + 4)[length:]
        if not following or following == LOCAL_HEADER or following in THE_END:
            source.take(length)
            return crc
    raise NotStreamable("no member follows a member's sizes")


def _deflated(source, flags, crc):
    inflater, seen = zlib.decompressobj(-15), 0
    while not inflater.eof:
        chunk = source.chunk()
        if not chunk:
            raise NotStreamable("the archive ends inside a member")
        try:
            out = inflater.decompress(chunk)
        except zlib.error as exc:
            raise NotStreamable("a member cannot be inflated: %s" % exc)
        if out:
            seen = zlib.crc32(out, seen)
            yield out
    source.give_back(inflater.unused_data)
    if flags & HAS_DESCRIPTOR:
        crc = _read_descriptor(source)
    if seen != crc:
        raise NotStreamable("a member differs from its checksum")


def members(chunks):
    """Yield (name, its data as chunks) for each member, folders too, in the archive's order. A
    member not read to its end is read past. The bytes after the last member are read as well, so
    a download behind `chunks` always finishes."""
    source = _Source(chunks)
    while True:
        signature = source.peek(4)
        if signature != LOCAL_HEADER:
            if signature in THE_END or not signature:
                source.drain()
                return
            raise NotStreamable("no member starts here")
        fields = struct.unpack("<4sHHHHHIIIHH", source.take(30))
        flags, method, crc, compressed, size = fields[2], fields[3], fields[6], fields[7], fields[8]
        raw = source.take(fields[9])
        extra = source.take(fields[10])
        if flags & ENCRYPTED:
            raise NotStreamable("the member is encrypted")
        if method == DEFLATED:
            body = _deflated(source, flags, crc)
        elif method == STORED and flags & HAS_DESCRIPTOR:
            body = _stored_of_unknown_size(source)
        elif method == STORED:
            body = _stored(source, _compressed_size(extra, compressed, size), crc)
        else:
            raise NotStreamable("compression method %d, flags %x" % (method, flags))
        yield member_name(raw, flags), body
        for _ in body:
            pass
