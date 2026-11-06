"""The Automations library as the OneDrive client syncs it to this computer.

The downloader reads the kit from this folder and copies a batch into it; OneDrive carries the
files to SharePoint, whatever their size. A file is in the library once Windows reports it as
an in-sync placeholder: a file just written is a plain file, and OneDrive turns it into a
placeholder in sync when SharePoint has taken it.
"""

import ctypes
import os
import shutil

PLACEHOLDER = 0x1
IN_SYNC = 0x8
# The file's bytes are in the cloud only. OneDrive sets these when it has let the local bytes go.
ONLY_IN_THE_CLOUD = 0x1000 | 0x400000
PINNED = 0x80000
UNPINNED = 0x100000


def _long(path):
    """Windows paths in the library pass 260 characters; the extended-length form opens them."""
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\" + os.path.abspath(path)


def placeholder_state(path):
    """The Cloud Files placeholder state of a file, or None when it is not there."""
    from ctypes import wintypes as w

    class FindData(ctypes.Structure):
        _fields_ = [("dwFileAttributes", w.DWORD), ("ftCreationTime", w.FILETIME),
                    ("ftLastAccessTime", w.FILETIME), ("ftLastWriteTime", w.FILETIME),
                    ("nFileSizeHigh", w.DWORD), ("nFileSizeLow", w.DWORD),
                    ("dwReserved0", w.DWORD), ("dwReserved1", w.DWORD),
                    ("cFileName", w.WCHAR * 260), ("cAlternateFileName", w.WCHAR * 14),
                    ("dwFileType", w.DWORD), ("dwCreatorType", w.DWORD), ("wFinderFlags", w.WORD)]

    # Without this a process sees placeholders as plain files and every state reads 0.
    ctypes.WinDLL("ntdll").RtlSetProcessPlaceholderCompatibilityMode(2)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.FindFirstFileW.restype = w.HANDLE
    kernel.FindClose.argtypes = [w.HANDLE]
    cloud = ctypes.WinDLL("cldapi")
    cloud.CfGetPlaceholderStateFromFindData.restype = ctypes.c_uint
    cloud.CfGetPlaceholderStateFromFindData.argtypes = [ctypes.POINTER(FindData)]
    found = FindData()
    handle = kernel.FindFirstFileW(_long(path), ctypes.byref(found))
    if handle is None or handle == w.HANDLE(-1).value:
        return None
    kernel.FindClose(handle)
    return cloud.CfGetPlaceholderStateFromFindData(ctypes.byref(found))


def attributes(path):
    """The file's Windows attributes, as a process that sees placeholders reads them; None when
    it is not there."""
    ctypes.WinDLL("ntdll").RtlSetProcessPlaceholderCompatibilityMode(2)
    read = ctypes.WinDLL("kernel32").GetFileAttributesW
    read.restype = ctypes.c_ulong
    value = read(ctypes.c_wchar_p(_long(path)))
    return None if value == 0xFFFFFFFF else value


def let_go(path):
    """Ask OneDrive to keep the file in the cloud only, as "Free up space" does. OneDrive lets the
    local bytes go within seconds of the file being in sync; until then it keeps them."""
    value = attributes(path)
    if value is not None:
        wanted = (value | UNPINNED) & ~PINNED
        if wanted != value:
            ctypes.WinDLL("kernel32").SetFileAttributesW(_long(path), wanted)


class Library:
    """Library paths ("<project>/kit/LV/...") under the local folder OneDrive syncs."""

    def __init__(self, root, state=placeholder_state, attrs=attributes, release=let_go):
        self.root = root
        self.state = state
        self.attrs = attrs
        self.release = release

    def local(self, path):
        return _long(os.path.join(self.root, *path.split("/")))

    def read_text(self, path):
        """A whole UTF-8 file of the library, or None when it is not there."""
        try:
            with open(self.local(path), encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def list_names(self, path):
        """Names of the items in one library folder; empty when it does not exist."""
        try:
            return sorted(os.listdir(self.local(path)))
        except FileNotFoundError:
            return []

    def upload(self, path, local_file, replace=False):
        """Copy a file into the library folder; OneDrive uploads it. Returns its size.

        A file already there with the same size is left alone, so a resumed batch does not make
        OneDrive upload it again; `replace` copies anyway.
        """
        target = self.local(path)
        size = os.path.getsize(local_file)
        if not replace and os.path.exists(target) and os.path.getsize(target) == size:
            return size
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(local_file, target)
        return size

    def move_in(self, path, local_file):
        """Move a file into the library folder, so its bytes are on this computer only once.
        Returns its size."""
        target = self.local(path)
        source = _long(local_file)
        size = os.path.getsize(source)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            os.replace(source, target)
        except OSError:
            # Another drive: the two copies live only until the source is removed.
            shutil.copyfile(source, target)
            os.remove(source)
        return size

    def not_in_sync(self, paths):
        """The paths OneDrive has not yet carried to SharePoint."""
        out = []
        for path in paths:
            state = self.state(self.local(path))
            if state is None or not (state & PLACEHOLDER and state & IN_SYNC):
                out.append(path)
        return out

    def let_go(self, paths):
        """Ask OneDrive to free each file's local bytes. Returns the paths still held here."""
        out = []
        for path in paths:
            value = self.attrs(self.local(path))
            if value is None or value & ONLY_IN_THE_CLOUD:
                continue
            self.release(self.local(path))
            out.append(path)
        return out

    def let_go_of_folder(self, path):
        """Ask OneDrive to free every file under a library folder. Returns the bytes still held
        on this computer; a file not carried to SharePoint yet is among them."""
        held = 0
        for folder, _, names in os.walk(self.local(path)):
            for name in names:
                local = os.path.join(folder, name)
                value = self.attrs(local)
                if value is None or value & ONLY_IN_THE_CLOUD:
                    continue
                self.release(local)
                try:
                    held += os.path.getsize(local)
                except OSError:
                    pass
        return held

    def bytes_held(self, path):
        """How many bytes of a library folder are on this computer."""
        held = 0
        for folder, _, names in os.walk(self.local(path)):
            for name in names:
                local = os.path.join(folder, name)
                value = self.attrs(local)
                if value is None or value & ONLY_IN_THE_CLOUD:
                    continue
                try:
                    held += os.path.getsize(local)
                except OSError:
                    pass
        return held
