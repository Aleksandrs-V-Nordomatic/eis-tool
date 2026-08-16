#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Printing Latvian without letting the terminal end the run.

Everything here is Latvian: tender titles, filenames, the text of a précis. On a Windows
console the default codec cannot encode `ā`, and `print()` then raises — which killed a CLI
once on the first tender title it tried to show, and killed a library function again later
because a caller's stdout happened to be cp1252.

Two rules follow, and they are different:

  * A COMMAND may reconfigure its own streams to UTF-8. `utf8_streams()` does that.
  * A LIBRARY may not — the streams belong to whoever called it. So `say()` prints, and on
    a codec it cannot satisfy falls back to a lossy rendering instead of raising.

Progress output is never worth a traceback. The work is already done by the time we are
describing it.
"""

import sys


def utf8_streams():
    """Make this process's stdout and stderr carry Latvian. For entry points only."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def say(line, stream=None):
    """One line of progress, never an exception."""
    stream = stream or sys.stdout
    try:
        stream.write(line + "\n")
    except UnicodeEncodeError:
        codec = getattr(stream, "encoding", None) or "ascii"
        stream.write(line.encode(codec, "replace").decode(codec, "replace") + "\n")
    try:
        stream.flush()
    except (ValueError, OSError):
        pass
