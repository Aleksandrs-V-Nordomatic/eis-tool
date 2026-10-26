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


class Library:
    """Library paths ("<project>/kit/LV/...") under the local folder OneDrive syncs."""

    def __init__(self, root, state=placeholder_state):
        self.root = root
        self.state = state

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

    def not_in_sync(self, paths):
        """The paths OneDrive has not yet carried to SharePoint."""
        out = []
        for path in paths:
            state = self.state(self.local(path))
            if state is None or not (state & PLACEHOLDER and state & IN_SYNC):
                out.append(path)
        return out
