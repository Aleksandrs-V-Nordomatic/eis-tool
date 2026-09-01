#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Which country a run is for, and everything that follows from it.

One run is one country. The tool fetches from that country's portal and publishes under
that country's folder, and the two must never come apart — a run reading Lithuania and
writing under `work/LV/` is a failure that succeeds: the upload returns 200, the index is
valid, and a Latvian reader is handed Lithuanian tenders with nothing anywhere saying so.

This repository is the Latvian tool and reads EIS only. The check below still runs, and
still refuses, because a country tool that assumes its own country is a country tool that
cannot say when it was pointed at the wrong drive.

So the country is resolved once, here, and both the source and the destination are derived
from it rather than configured beside it. Configuring them separately is what makes the
mismatch expressible in the first place.

THE DESTINATION IS DERIVED, NOT CONFIGURED. `GRAPH_DEST_ROOT` names the project's runtime
folder — the `work/` a reader knows — and the country code is appended to it. It is not
enough to ask the deployment to set `GRAPH_DEST_ROOT` to `…/work/LT` and trust it: that is
the same two-places-to-get-right this file exists to remove, and the first symptom of
getting it wrong is a day of one country's tenders sitting in another's folder. A root that
already ends in a country code is therefore refused rather than appended to, because
`work/LV/LV` is the other way the same mistake shows up.

THE SOURCE IS DERIVED TOO. A code with no source module is refused, never guessed at and
never quietly served by the other country's reader.
"""

import re

# The country this repository fetches, and the modules that read its portal.
#
# ONE COUNTRY PER REPOSITORY, AND THE TABLE STAYS. It would be shorter to delete this file
# in a single-country tool and hard-code LV, and that is exactly what must not happen: the
# table is what makes `resolve` able to REFUSE. A run that names no country, or names the
# wrong one, stops here with a sentence instead of quietly fetching Latvia and filing it
# under somebody else's folder. That guard is worth more than the line it costs.
#
# Making the next country's tool: fork this repository, replace the entry below and the two
# modules it names, and change the name and the README. Nothing else in the tool learns a
# country name, so nothing else has to be found and edited. `epps-tool` is Lithuania's.
SOURCES = {
    "LV": {"page": "eis_page", "fetch": "eis_fetch",
           "portal": "EIS", "timezone": "Europe/Riga"},
}

CODE = re.compile(r"^[A-Z]{2}$")


class Mismatch(ValueError):
    """The country could not be resolved, or the destination contradicts it."""


def resolve(explicit=None, environ=None):
    """The run's country, from the flag or from `EIS_COUNTRY`. Never a default.

    There is deliberately no fallback, not even to this repository's one country. A default
    would mean a run launched without the flag publishes under whichever folder the tool
    guessed, and the way that surfaces is a morning of the wrong country's cards.
    """
    environ = {} if environ is None else environ
    code = (explicit or environ.get("EIS_COUNTRY") or "").strip().upper()
    if not code:
        raise Mismatch("no country: pass --country %s or set EIS_COUNTRY"
                       % "|".join(sorted(SOURCES)))
    if not CODE.match(code):
        raise Mismatch("%r is not a two-letter country code" % code)
    if code not in SOURCES:
        raise Mismatch("no source for %s — known: %s" % (code, ", ".join(sorted(SOURCES))))
    return code


def source(code):
    """The modules that read this country's portal, imported on demand.

    Imported here rather than at module scope so that a run for one country does not need
    the other's dependencies present, and so that a broken reader for a country nobody
    asked for cannot stop a run.
    """
    if code not in SOURCES:
        raise Mismatch("no source for %s" % code)
    import importlib
    entry = SOURCES[code]
    return (importlib.import_module(entry["page"]),
            importlib.import_module(entry["fetch"]))


def destination(base, code):
    """Where this country publishes: the runtime root, then the country's own folder.

    `base` is the project's `work/`. The country code is appended, giving the shape the
    library already carries — `work/LV/`, `work/LT/` — so a reader that knows one country's
    layout knows every country's.
    """
    code = resolve(code)
    trimmed = (base or "").strip().strip("/")
    if not trimmed:
        raise Mismatch("no destination root: set GRAPH_DEST_ROOT to the project's work "
                       "folder, without a country code")
    tail = trimmed.rsplit("/", 1)[-1].upper()
    if CODE.match(tail):
        # Almost always the deployment doing the tool's job for it, which is how
        # `work/LV/LV` gets created and then quietly filled.
        raise Mismatch("destination root %r already ends in a country code; give the "
                       "folder that CONTAINS the country folders" % trimmed)
    return "%s/%s" % (trimmed, code)


def parser_files(code):
    """The files that decide this country's facts, for the version stamped beside them.

    `changes` records a parser version so that an improvement to a page reader — one more
    spelling of a label, a field that used to come back null — is not reported as an
    amendment on every procurement in the corpus. That only works if the version follows
    the reader the run actually used: stamping Latvia's digest onto a Lithuanian
    fingerprint makes an `eis_page` edit invisible and another country's a false alarm, in
    the same run and for the same reason.
    """
    return (SOURCES[resolve(code)]["page"] + ".py",)


def describe(code):
    """What a report says about the country it ran for."""
    entry = SOURCES[resolve(code)]
    return {"country": resolve(code), "portal": entry["portal"],
            "timezone": entry["timezone"]}


def add_argument(parser):
    """The one flag, spelled the same way everywhere it appears."""
    parser.add_argument("--country", default=None,
                        help="which country this run is for (%s); may also come from "
                             "EIS_COUNTRY" % "|".join(sorted(SOURCES)))
    return parser
