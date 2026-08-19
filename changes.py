#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""What changed about one tender since the last time it was fetched.

Pure functions over a pack's own JSON. No network, no model, no clock: the same pack
compared against the same previous state answers the same thing on any machine, which is
the only reason this is worth having at all. A consumer that must re-read a tender because
it cannot tell whether anything moved is a consumer reading forty documents to learn that
none of them changed.

WHAT IT COMPARES, AND WHAT IT REFUSES TO.

    facts       the tender's own published fields — deadline, status, value, CPV
    records     the page's document records, by id: publish date, title, and the digests
                of the files under each
    documents   the extracted text, addressed by the digest of the file it came from
    unreadable  the files no decoder could read, by their own digest

Every comparison is over sha256 of ORIGINAL bytes, never over the Markdown. That is the
line that keeps this honest: `normalize.py` is deterministic for a given version, but two
versions of it may render one unchanged PDF differently, and a diff taken over the Markdown
would report that as the buyer replacing a document. The extractor's fingerprint is carried
separately (`tool`), so "the text was extracted again" is a different sentence from "the
tender changed" and a reader is never asked to guess which one it is looking at.

WHAT IS DELIBERATELY NOT A FACT. `register_check` and `eis_only` record how this tool came
to know about the procurement — by discovery, by a link on the page, or not at all. A
tender found through the register on Monday and fetched by id on Tuesday has not changed;
only the route to it has. Comparing them would report a change every time the caller varied
how it asked, which is the one kind of false positive that would train a reader to ignore
the whole file.
"""

import hashlib
import os

STATE_SCHEMA = "tender-state/1"
SEEN_SCHEMA = "tender-seen/1"
CHANGE_SCHEMA = "tender-change/1"

# The published fields whose movement is news. Named one by one rather than taken as
# "everything in procurement.json", because that file also carries `fields` — the raw
# label-to-value map the values above are read out of — and diffing a value against its own
# source reports every change twice.
FACTS = (
    "title", "status", "published", "ref", "link", "iub_uuid",
    "deadline", "opening", "docs_until", "consultation_until",
    "value", "currency", "buyer", "buyer_reg",
    "procedure", "profile", "legal_basis", "work_kind", "award_criteria",
    "cpv_main", "cpv_additional", "place", "lots", "framework", "contract_duration",
)

# THE FIELDS WHOSE VALUE IS A DISPLAY STRING, NOT A FACT.
#
# EIS serves the same procurement in Latvian or in English and does not let the caller
# decide: the downloader has always asked for Latvian first and is answered in English often
# enough to matter — twice in four consecutive runs of one tender, an hour apart. The stable
# `#id` keys survive that; the values under them do not. `Izsludināts` becomes `Announced`,
# `Nē` becomes `No`, and `28.08.2026 09:00 (plānots)` becomes the same instant `(scheduled)`.
#
# Compared blind, every one of these reports as an amendment on every language flip, for
# every tender in the run — the exact false positive this module's docstring says would train
# a reader to ignore the file. So they are compared only between two pages served in the same
# language, and a flip is reported as itself rather than as ten amendments that did not
# happen. Nothing is dropped quietly: `facts_not_compared` names what was skipped.
LOCALIZED = ("status", "procedure", "legal_basis", "work_kind", "award_criteria",
             "lots", "framework", "contract_duration",
             "deadline", "opening", "docs_until", "consultation_until")

# Which language a page came in, read off the labels the parser matched — it keeps them as
# keys beside the stable `#id` ones, so this needs nothing the pack does not already carry.
_LANGUAGE_LABELS = (("lv", "Iepirkuma statuss"), ("en", "Procurement status"))


def page_language(procurement):
    """`lv`, `en`, or None when the page carried neither label."""
    fields = (procurement or {}).get("fields") or {}
    for code, label in _LANGUAGE_LABELS:
        if label in fields:
            return code
    return None


# What is compared about a record, beyond the files under it. `section` is included because
# a document moving from the live set into the archive is exactly the kind of change a
# bidder needs to see, and it costs nothing to notice.
RECORD_FIELDS = ("publish_date", "title", "type_code", "section", "withheld")


# WHAT DECIDES WHETHER THE SAME FILE YIELDS THE SAME MARKDOWN, AND NOTHING ELSE.
#
# The extractor's version is recorded beside the text so that re-extraction can be told apart
# from an amendment, and a tender whose version moved has its text refreshed. That makes the
# value expensive to get wrong in one direction: anything it tracks which does NOT change the
# output turns an unrelated edit into a re-upload of every document of every tender ever
# fetched. The run's commit is exactly such a value — a corrected comment moves it.
#
# So it is a digest of the extraction path itself: the extractor, and the library versions it
# is pinned to. Editing this file, the delivery, or a test does not move it, because none of
# them can change a single character of the Markdown.
#
# WHAT IT DOES NOT COVER, SAID PLAINLY. The toolchain image is pinned by tag rather than by
# digest, and LibreOffice and Tesseract live inside it — so a rebuilt image can change the
# text of Word 97 attachments and scans without moving this value. Closing that means pinning
# the image by digest, which is a different change in a different file.
PIPELINE_FILES = ("normalize.py", "requirements.txt")

# AND SEPARATELY, WHAT DECIDES THE FACTS. The published fields are read by `eis_page`, not by
# the extractor, and the two move for different reasons and cost different things. A parser
# improvement — one more spelling of a label, a field read that used to come back null — will
# change facts across the whole corpus in a single day, and a diff that did not know would
# report every one of them as an amendment somebody made. That is the same false positive the
# page language produces, arriving from our own side.
#
# It is deliberately NOT in PIPELINE_FILES. Tracking it there would re-upload every document
# of every tender for an edit that cannot change one character of Markdown.
PARSER_FILES = ("eis_page.py",)


def _digest(root, names):
    """A digest of the named files, or None when any of them cannot be read.

    None rather than a fallback: an unreadable file is not evidence that nothing changed, and
    a made-up value would either force work or suppress it, both silently.
    """
    root = root or os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in names:
        try:
            with open(os.path.join(root, name), "rb") as fh:
                h.update(fh.read())
        except OSError:
            return None
        h.update(b"|")
    return h.hexdigest()[:12]


def pipeline_version(root=None):
    """A digest of the extraction path — what decides whether a file yields the same text."""
    return _digest(root, PIPELINE_FILES)


def parser_version(root=None):
    """A digest of the page parser — what decides the facts read off a procurement page."""
    return _digest(root, PARSER_FILES)


def document_key(original_sha256, source):
    """The address a document keeps for as long as its bytes and its place do.

    Both halves are needed. `original_sha256` alone does not identify a document, because
    `normalize.py` stamps the CARRIER's digest on every entry it produced — so all two
    hundred members of one archive share one digest and would collapse into a single key.
    `source` alone does not identify it either: it is a path built from the record section
    and a slug of the filename, and a buyer who replaces a file without renaming it would
    reuse the same path for different bytes.

    Together they are stable across runs and change exactly when the document does, which
    is what makes an unchanged document free to leave where it already lies.
    """
    seed = "%s\n%s" % (original_sha256 or "", source or "")
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _facts(procurement):
    return {k: (procurement or {}).get(k) for k in FACTS}


def _records(manifest):
    """Every record the page carried, downloadable or withheld, keyed by its id."""
    out = {}
    for record in (manifest or {}).get("documents", []):
        rid = str(record.get("id"))
        files = {}
        for f in record.get("files", []):
            digest = f.get("sha256")
            if digest:
                files[digest] = f.get("original_name") or f.get("filename")
        out[rid] = {"publish_date": record.get("publish_date"),
                    "title": record.get("title"),
                    "type_code": record.get("type_code"),
                    "section": record.get("section"),
                    "withheld": False,
                    "files": files}
    # A record the portal published without a download is still a record, and its arrival is
    # news of exactly the same kind. Listed with no files rather than omitted, so it cannot
    # later look like a record that vanished.
    for record in (manifest or {}).get("withheld_records", []):
        rid = str(record.get("id"))
        out.setdefault(rid, {"publish_date": record.get("publish_date"),
                             "title": record.get("title"),
                             "type_code": record.get("type_code"),
                             "section": record.get("section"),
                             "withheld": True,
                             "files": {}})
    return out


def _documents(normalized):
    """The extracted text, by document key. Duplicates are one document, as they are read."""
    out = {}
    for entry in (normalized or {}).get("documents", []):
        if entry.get("also_listed_under") or not entry.get("markdown_path"):
            continue
        key = document_key(entry.get("original_sha256"), entry.get("source"))
        out[key] = {"name": (entry.get("original_file")
                             or (entry.get("source") or "").rsplit("/", 1)[-1]),
                    "source": entry.get("source"),
                    "section": entry.get("section"),
                    "record": entry.get("record_title"),
                    "record_id": str(entry.get("record_id") or "") or None,
                    "chars": entry.get("markdown_chars")}
    return out


def _unreadable(normalized):
    """Files named but not read, by their own digest — which for an archive member is its
    own, not its carrier's, because `normalize.unreadable` hashes the file in front of it."""
    out = {}
    for gap in (normalized or {}).get("unreadable_files", []):
        key = gap.get("sha256") or document_key("", gap.get("file"))
        out[key] = {"file": gap.get("file"), "reason": gap.get("reason"),
                    "bytes": gap.get("bytes")}
    return out


def fingerprint(pid, procurement, manifest, normalized, tool=None, parser=None):
    """One tender's state, as the one dict everything downstream compares.

    NOTHING IN HERE MOVES UNLESS THE TENDER DOES. No date, no run id, no counter — so two
    runs over an unchanged tender produce equal fingerprints, and the delivery can leave the
    stored one alone instead of rewriting a few hundred kilobytes to advance a timestamp.
    When a tender was last looked at is a fact about the run, and lives in `seen`.

    `tool` is the extractor's own version — the run's commit, in production. It is recorded
    rather than compared: see the module docstring for why the two must not be confused.
    """
    return {"schema": STATE_SCHEMA,
            "pid": str(pid),
            "tool": tool,
            "parser": parser,
            "language": page_language(procurement),
            "facts": _facts(procurement),
            "records": _records(manifest),
            "documents": _documents(normalized),
            "unreadable": _unreadable(normalized)}


def seen(pid, previous, record, date):
    """The small file every run rewrites, so the fingerprint need not be.

    Freshness is what a reader asks of a tender nobody has touched in a while, and it is the
    one thing that changes on a day when nothing else did. Kept apart and kept tiny.
    """
    return {"schema": SEEN_SCHEMA,
            "pid": str(pid),
            "first_seen": (previous or {}).get("first_seen") or date,
            "last_seen": date,
            "last_change": (date if record.get("status") != "unchanged"
                            else (previous or {}).get("last_change")),
            "tool": record.get("tool")}


def _fact_moves(before, after, comparable=FACTS):
    moves = []
    for key in comparable:
        was, now = before.get(key), after.get(key)
        if was != now:
            moves.append({"field": key, "from": was, "to": now})
    return moves


def _record_moves(before, after):
    """Records added, removed, and altered — the last named field by field.

    A record whose files changed is reported as changed even when its publish date did not.
    That combination is the one worth watching: it is a document replaced in place, and it
    is the only shape of update the page's own dates would not have told anyone about.
    """
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = []
    for rid in sorted(set(before) & set(after)):
        was, now = before[rid], after[rid]
        moved = [f for f in RECORD_FIELDS if was.get(f) != now.get(f)]
        files_added = sorted(set(now.get("files", {})) - set(was.get("files", {})))
        files_gone = sorted(set(was.get("files", {})) - set(now.get("files", {})))
        if not (moved or files_added or files_gone):
            continue
        changed.append({"id": rid,
                        "title": now.get("title"),
                        "fields": [{"field": f, "from": was.get(f), "to": now.get(f)}
                                   for f in moved],
                        "files_added": [now["files"][d] for d in files_added],
                        "files_removed": [was["files"][d] for d in files_gone],
                        # Named plainly, because it is the case a reader must not have to
                        # infer: the record says it is the same as yesterday and is not.
                        "silent": bool((files_added or files_gone)
                                       and "publish_date" not in moved)})
    return added, removed, changed


def diff(previous, current, date=None, run_id=None, seen=None):
    """What moved between two fingerprints of one tender.

    `previous` is None the first time a tender is seen, and the answer is then "new" with
    nothing enumerated: everything about it is being delivered anyway, and listing every
    document as an addition would make the day's change file a copy of the day.

    `seen` is the small per-run file described above; absent, this is treated as a tender
    nobody has a record of looking at, which is the honest answer when it is missing.

    A tender whose extractor changed but whose content did not comes back `unchanged` with
    `reextracted` set: the tender stood still and the pipeline moved, and a reader filtering
    on status must not be shown the second as though it were the first. Delivery still
    refreshes the text in that case, because the Markdown on the drive was produced by the
    older extractor and no longer matches what this one would write.
    """
    pid = str(current.get("pid"))
    seen = seen or {}
    record = {"schema": CHANGE_SCHEMA, "pid": pid, "date": date, "run_id": run_id,
              "tool": current.get("tool")}

    if not previous:
        record.update({
            "status": "new",
            "first_seen": date,
            "counts": {"records": len(current.get("records", {})),
                       "documents": len(current.get("documents", {})),
                       "unreadable": len(current.get("unreadable", {}))},
        })
        return record

    # A page served in the other language is not an amended tender. Only the fields whose
    # values survive translation are compared across one, and the skip is named rather than
    # taken silently.
    was_lang, now_lang = previous.get("language"), current.get("language")
    translated = bool(was_lang and now_lang and was_lang != now_lang)
    # A page read by a different parser is not an amended tender either, and the reason is
    # the same one: the difference came from our side. Every fact is suspect in that case,
    # not only the ones that translate, so none of them is compared and the run is spent
    # refreshing the fingerprint so the next one compares clean.
    reparsed = bool(previous.get("parser") and current.get("parser")
                    and previous["parser"] != current["parser"])
    if reparsed:
        comparable = ()
    elif translated:
        comparable = tuple(f for f in FACTS if f not in LOCALIZED)
    else:
        comparable = FACTS
    facts = _fact_moves(previous.get("facts", {}), current.get("facts", {}), comparable)
    added, removed, changed = _record_moves(previous.get("records", {}),
                                            current.get("records", {}))

    docs_before, docs_now = previous.get("documents", {}), current.get("documents", {})
    docs_added = [dict(docs_now[k], key=k) for k in sorted(set(docs_now) - set(docs_before))]
    docs_gone = [dict(docs_before[k], key=k) for k in sorted(set(docs_before) - set(docs_now))]

    gaps_before, gaps_now = previous.get("unreadable", {}), current.get("unreadable", {})
    gaps_added = [gaps_now[k] for k in sorted(set(gaps_now) - set(gaps_before))]
    gaps_gone = [gaps_before[k] for k in sorted(set(gaps_before) - set(gaps_now))]

    moved = bool(facts or added or removed or changed or docs_added or docs_gone
                 or gaps_added or gaps_gone)
    record.update({
        "status": "changed" if moved else "unchanged",
        "reextracted": previous.get("tool") != current.get("tool"),
        "first_seen": seen.get("first_seen"),
        "previously_seen": seen.get("last_seen"),
        "facts": facts,
        "language": {"from": was_lang, "to": now_lang} if translated else None,
        "reparsed": reparsed,
        "facts_not_compared": (sorted(FACTS) if reparsed
                               else sorted(LOCALIZED) if translated else []),
        "records_added": [dict(current["records"][r], id=r) for r in added],
        "records_removed": [dict(previous["records"][r], id=r) for r in removed],
        "records_changed": changed,
        "documents_added": docs_added,
        "documents_removed": docs_gone,
        "unreadable_added": gaps_added,
        "unreadable_removed": gaps_gone,
        # What did NOT have to travel. The number this whole arrangement exists to make
        # large, and the one to watch if it ever stops being.
        "carried_over": len(set(docs_before) & set(docs_now)),
        "counts": {"records": len(current.get("records", {})),
                   "documents": len(docs_now),
                   "unreadable": len(gaps_now)},
    })
    return record


def refreshed(record):
    """Whether the home must be rewritten, which is not the same as documents travelling.

    A tender that moved, obviously. A re-extraction, because the Markdown on the drive came
    from a version this run no longer agrees with. And a re-parse, because the facts beside
    it did — the manifests and the index carry them, and the fingerprint has to record the
    new parser or every later run would report the same re-parse for ever.
    """
    return (record.get("status") != "unchanged"
            or bool(record.get("reextracted")) or bool(record.get("reparsed")))


def documents_to_send(record, current):
    """The document keys this delivery must actually upload.

    Everything for a tender nobody has seen; the additions alone for one already on the
    drive; and everything again when the extractor moved under a tender that did not, since
    the text sitting there was written by a version that no longer exists.
    """
    if record.get("status") == "new" or record.get("reextracted"):
        return sorted(current.get("documents", {}))
    return sorted(d["key"] for d in record.get("documents_added", []))


def summary(record):
    """One line's worth of a change record, for an index that must stay small."""
    if record.get("status") == "new":
        return {"status": "new", "documents": record["counts"]["documents"]}
    return {"status": record.get("status"),
            "reextracted": bool(record.get("reextracted")),
            "facts": len(record.get("facts", [])),
            "records_added": len(record.get("records_added", [])),
            "records_changed": len(record.get("records_changed", [])),
            "documents_added": len(record.get("documents_added", [])),
            "documents_removed": len(record.get("documents_removed", [])),
            "carried_over": record.get("carried_over", 0),
            # A record whose files moved without its date moving. Surfaced this high because
            # it is the one update the page's own metadata does not announce.
            "silent_records": sum(1 for r in record.get("records_changed", [])
                                  if r.get("silent"))}
