"""List and unpack the members of a downloaded archive: ZIP natively, 7z and RAR with 7-Zip."""

import os
import shutil
import subprocess
import tempfile
import zipfile

import eis_fetch

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SEVEN_ZIP_PLACES = (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe")


class Unreadable(Exception):
    """The member list cannot be read."""


def seven_zip():
    found = os.environ.get("SEVEN_ZIP") or shutil.which("7z") or shutil.which("7z.exe")
    if found:
        return found
    return next((p for p in SEVEN_ZIP_PLACES if os.path.exists(p)), None)


MAGIC = (b"PK\x03\x04", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07")


def looks_like_archive(path):
    """True when the file starts like a ZIP, 7z or RAR archive, whatever its name says."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    return any(head.startswith(m) for m in MAGIC)


def starts_like_archive(head):
    """True when these first bytes are those of a ZIP, 7z or RAR archive."""
    return any(head.startswith(m) for m in MAGIC)


def members(path):
    """Paths of the files inside an archive, with `/` as separator."""
    return [name for name, _ in sized_members(path)]


def sized_members(path):
    """Each file inside an archive as (path with `/` as separator, its unpacked size)."""
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as z:
                found = [(_zip_name(info), info.file_size) for info in z.infolist()
                         if not info.is_dir()]
        except (zipfile.BadZipFile, OSError) as exc:
            raise Unreadable(str(exc))
        return [(n, size) for n, size in found if n]
    binary = seven_zip()
    if not binary:
        raise Unreadable("7-Zip is not installed")
    proc = subprocess.run([binary, "l", "-slt", "-ba", "-sccUTF-8", path],
                          capture_output=True, timeout=600, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        raise Unreadable(proc.stderr.decode("utf-8", "replace")[:200] or "7-Zip failed")
    return listed_sizes(proc.stdout.decode("utf-8", "replace"))


def listed_files(listing):
    """The file paths of a `7z l -slt` listing."""
    return [name for name, _ in listed_sizes(listing)]


def listed_sizes(listing):
    """The files of a `7z l -slt` listing as (path, size). A folder is marked `Folder = +` in ZIP
    and RAR archives, but only by the D of its attributes in 7z ones; extracting a folder would
    pour all its files into one."""
    out, current = [], {}
    for line in listing.splitlines() + [""]:
        if not line.strip():
            attributes = current.get("Attributes", "").split(" ")[0]
            if current.get("Path") and current.get("Folder") != "+" and "D" not in attributes:
                size = current.get("Size", "")
                out.append((current["Path"].replace("\\", "/"), int(size) if size.isdigit() else 0))
            current = {}
            continue
        key, _, value = line.partition(" = ")
        current[key.strip()] = value
    return out


def member_chunks(path, member, size=2 ** 20):
    """One member of an archive on disk as it is unpacked, written nowhere."""
    if not eis_fetch.safe_member(member):
        raise Unreadable("unsafe member path %r" % member)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            info = next((i for i in z.infolist() if _zip_name(i) == member), None)
            if info is None:
                raise Unreadable("no member %r" % member)
            with z.open(info) as src:
                while True:
                    chunk = src.read(size)
                    if not chunk:
                        return
                    yield chunk
    binary = seven_zip()
    if not binary:
        raise Unreadable("7-Zip is not installed")
    proc = subprocess.Popen([binary, "e", "-so", "-sccUTF-8", path, member],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW)
    finished = False
    try:
        while True:
            chunk = proc.stdout.read(size)
            if not chunk:
                break
            yield chunk
        finished = True
    finally:
        if not finished:
            proc.kill()
        proc.stdout.close()
        error = proc.stderr.read()
        proc.stderr.close()
        proc.wait()
    if proc.returncode != 0:
        raise Unreadable(error.decode("utf-8", "replace")[:200] or "7-Zip failed")


def _zip_name(info):
    """A member name decoded the way the archiver meant it."""
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name.replace("\\", "/")


def extract(path, member, dest):
    """Write one member of the archive to `dest`. `dest` appears only once it is whole, so a
    run stopped halfway leaves no member that looks finished."""
    if not eis_fetch.safe_member(member):
        raise Unreadable("unsafe member path %r" % member)
    part = dest + ".part"
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            info = next((i for i in z.infolist() if _zip_name(i) == member), None)
            if info is None:
                raise Unreadable("no member %r" % member)
            with z.open(info) as src, open(part, "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.replace(part, dest)
        return
    binary = seven_zip()
    if not binary:
        raise Unreadable("7-Zip is not installed")
    with open(part, "wb") as dst:
        proc = subprocess.run([binary, "e", "-so", "-sccUTF-8", path, member],
                              stdout=dst, stderr=subprocess.PIPE, timeout=1800,
                              creationflags=NO_WINDOW)
    if proc.returncode != 0:
        raise Unreadable(proc.stderr.decode("utf-8", "replace")[:200] or "7-Zip failed")
    os.replace(part, dest)


def extract_many(path, picks):
    """Write each chosen member to its own `dest`, as pairs (member, dest), reading the archive
    once. A solid 7z archive is one compressed block, so 7-Zip has to unpack it from the start up
    to any member; one call per member read a 768 MB archive again for each of its 114 kept
    members and took an hour. Each `dest` appears only once it is whole."""
    picks = list(picks)
    for member, _ in picks:
        if not eis_fetch.safe_member(member):
            raise Unreadable("unsafe member path %r" % member)
    if not picks:
        return
    if zipfile.is_zipfile(path):
        for member, dest in picks:
            extract(path, member, dest)
        return
    binary = seven_zip()
    if not binary:
        raise Unreadable("7-Zip is not installed")
    scratch = tempfile.mkdtemp(prefix=".unpack-", dir=os.path.dirname(path))
    try:
        listing, out = os.path.join(scratch, "members.txt"), os.path.join(scratch, "out")
        with open(listing, "w", encoding="utf-8") as fh:
            fh.write("\n".join(member.replace("/", os.sep) for member, _ in picks) + "\n")
        # -spd: a name with * or ? in it is a name, not a pattern.
        proc = subprocess.run([binary, "x", "-y", "-spd", "-sccUTF-8", "-scsUTF-8", "-o" + out,
                               path, "@" + listing],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800,
                              creationflags=NO_WINDOW)
        if proc.returncode != 0:
            raise Unreadable(proc.stderr.decode("utf-8", "replace")[:200] or "7-Zip failed")
        missing = [m for m, _ in picks
                   if not os.path.isfile(long_path(os.path.join(out, *m.split("/"))))]
        if missing:
            raise Unreadable("no member %r" % missing[0])
        for member, dest in picks:
            os.replace(long_path(os.path.join(out, *member.split("/"))), dest)
    finally:
        shutil.rmtree(long_path(scratch), ignore_errors=True)


def long_path(path):
    """A Windows path that may run past 260 characters: members sit deep in their folders."""
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\" + os.path.abspath(path)
