#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deterministic downloader for the public documents of one Latvian EIS procurement.

    python3 eis_fetch.py https://www.eis.gov.lv/EKEIS/Supplier/Procurement/178345 --out out/

Runs on a GitHub Actions Ubuntu runner, which is the egress this project has. No login, no
browser, no LLM, no OCR: everything below is a documented public flow that EIS itself
describes for a non-authorised guest.

TWO LEVELS, TWO IDENTIFIERS — the trap that costs an afternoon. A row in
`ActualDocuments_items` is a document CONTAINER; its `FileId` is null because it is not a
file. Files appear only after `ViewDocument`. In `DownloadDocumentFile`, `Id` is the
DOCUMENT id and `FileId` is the FILE id; passing the file id as both returns a small HTML
error page that reads like an access denial and is not one. `ProcurementIdentifier` is
mandatory everywhere and is NOT the null copy nested inside `DocumentTitle`.

PARTIAL SUCCESS IS FAILURE. If any expected record or file is missing, this exits non-zero
and writes nothing to the success path. A tender that looks downloaded but is not is worse
than one that plainly failed, because only the second gets fixed.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
import zipfile
from urllib.parse import urlencode, urlparse

import eis_page

BASE = "https://www.eis.gov.lv"
ALLOWED_HOSTS = {"eis.gov.lv", "www.eis.gov.lv"}
PROC_RE = re.compile(r"^/EKEIS/Supplier/Procurement/(\d+)/?$", re.IGNORECASE)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

# TWO SECTIONS, AND WHETHER TO TAKE BOTH IS THE CALLER'S CHOICE, NOT A CONSTANT.
#
# `Dokumenti (aktuālie)` governs; `Dokumenti (arhīvs)` holds what it replaced — a superseded
# specification, and the ORIGINAL of anything a buyer re-uploaded corrected. The archive is
# a small share of a day's bytes but a large share of its FILES, and every record costs a
# pause, so taking it is cheap in bandwidth and expensive in wall clock.
#
# Neither answer is right for both callers, which is why this is a switch and not a rule.
# Judging RELEVANCE needs the version that governs — the archive adds superseded text and
# pause. Preparing a BID may need to see what changed. Two tools against the same portal had
# drifted into opposite defaults, which is how one of them was quietly answering a question
# nobody had put to it.
#
# The default stays "take everything", because a missing document is the failure this
# project refuses and a caller who wants speed can say so.
SECTIONS = (("actual", "ActualDocuments_items"), ("archive", "HistoricalDocuments_items"))
TOKEN_RE = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')


# HOW LONG TO WAIT BETWEEN RECORDS, AND HOW TO CHANGE IT HONESTLY.
#
# Five seconds was chosen defensively, never measured, and it dominates a small tender's
# wall clock: the work per record is a fraction of the wait that follows it.
#
# It is settable (`EIS_PAUSE=2.5`) so the value can be tested without a code change and
# reverted without a deploy. What matters is being able to tell the two failures apart,
# because they demand opposite responses and this project has already confused them once:
#
#   * REFUSED ADDRESS — the very first request fails, before any volume. Nothing to do with
#     pace; redraw the runner.
#   * EARNED THROTTLING — the run starts fine and degrades PART WAY THROUGH, into pages
#     without the procurement markers or a refused connection after N good requests.
#
# So the rule for lowering it: only a mid-run degradation is evidence about the pace. If a
# 2.5 s day fails at request one, that says nothing and the pause is not the culprit.
PAUSE_BETWEEN_RECORDS = float(os.environ.get("EIS_PAUSE", "5"))

# COURTESY SHOULD TRACK THE LOAD WE IMPOSE, NOT HOW THE PLATFORM SLICED ITS METADATA.
#
# A flat pause per record is the wrong unit, and heavily subdivided tenders make that
# obvious. A procurement split into many lots gets one document container per lot, so it
# carries a large number of small records — and a flat pause then charges the portal's
# comfort for the platform's data model rather than for any load we imposed.
#
# The pause is therefore proportional to what was just taken, between a floor and the old
# flat value: a small lot sheet costs the floor, a large archive still costs the full five
# seconds. Same total courtesy per megabyte, far less per tender that happens to be
# administratively subdivided.
PAUSE_FLOOR = float(os.environ.get("EIS_PAUSE_FLOOR", "0.75"))
PAUSE_PER_MB = float(os.environ.get("EIS_PAUSE_PER_MB", "0.5"))


def pause_for(bytes_taken):
    """Seconds to wait after taking `bytes_taken`. Floor ≤ result ≤ PAUSE_BETWEEN_RECORDS."""
    if PAUSE_BETWEEN_RECORDS <= 0:
        return 0.0
    earned = PAUSE_FLOOR + (max(0, bytes_taken) / 1048576.0) * PAUSE_PER_MB
    return max(PAUSE_FLOOR, min(PAUSE_BETWEEN_RECORDS, earned))


class Fail(Exception):
    """Anything that must stop the run. There is no partial success."""


def _describe(html):
    """Say what page EIS actually served, in one line, so a log is enough to diagnose it."""
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = re.sub(r"\s+", " ", title.group(1)).strip() if title else "no <title>"
    marks = [name for name, probe in (
        ("login form", "__RequestVerificationToken"),
        ("error page", "kļūda"),
        ("throttling", "too many"),
        ("maintenance", "tehnisk"),
    ) if probe.lower() in html.lower()]
    return "title=%r%s" % (title[:90], (" [%s]" % ", ".join(marks)) if marks else "")


# --------------------------------------------------------------------------- input safety
def canonical_url(raw):
    p = urlparse((raw or "").strip())
    if p.scheme.lower() != "https" or (p.hostname or "").lower() not in ALLOWED_HOSTS:
        raise Fail("only https://www.eis.gov.lv procurement URLs are accepted, got %r" % raw)
    m = PROC_RE.match(p.path)
    if not m:
        raise Fail("expected /EKEIS/Supplier/Procurement/<numeric-id>, got %r" % p.path)
    pid = m.group(1)
    return "%s/EKEIS/Supplier/Procurement/%s" % (BASE, pid), pid


# ------------------------------------------------------------------------------ transport
class Curl(object):
    """System curl with a shared cookie jar.

    curl rather than a Python HTTP client on purpose: it is what the runner has, it keeps
    the cookie jar in one file across every call, and its behaviour under redirects and
    chunked 400 MB downloads is the boring, well-understood one.
    """

    def __init__(self, workdir, referer):
        self.jar = os.path.join(workdir, "cookies.txt")
        self.referer = referer

    def _run(self, args, dest=None, timeout=1800):
        cmd = ["curl", "-sS", "--fail-with-body", "--location", "--compressed",
               "--connect-timeout", "45", "--max-time", str(timeout),
               "--retry", "5", "--retry-delay", "3", "--retry-all-errors",
               "--retry-max-time", "300",
               "--cookie", self.jar, "--cookie-jar", self.jar,
               "-A", UA, "-H", "Accept-Language: lv,en;q=0.8",
               "-H", "Referer: " + self.referer,
               "-D", "-"] + args
        if dest:
            cmd += ["-o", dest]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 120)
        if proc.returncode != 0:
            raise Fail("curl failed (%d): %s" % (proc.returncode, proc.stderr.decode()[:300]))
        return proc.stdout

    @staticmethod
    def _split_headers(blob):
        text = blob.decode("utf-8", "replace")
        head, _, body = text.partition("\r\n\r\n")
        while body.startswith("HTTP/"):                     # redirects leave stacked headers
            head, _, body = body.partition("\r\n\r\n")
        headers = {}
        for line in head.splitlines()[1:]:
            k, _, v = line.partition(":")
            if v:
                headers[k.strip().lower()] = v.strip()
        return headers, body

    def get_text(self, url):
        return self._split_headers(self._run([url]))[1]

    def get_file(self, url, dest):
        headers, _ = self._split_headers(self._run([url], dest=dest))
        return headers

    def post_json(self, url, payload, token):
        args = ["-X", "POST", url,
                "-H", "Content-Type: application/json; charset=utf-8",
                "-H", "Accept: text/html, */*; q=0.01",
                "-H", "Origin: " + BASE,
                "-H", "X-Requested-With: XMLHttpRequest",
                "-H", "__RequestVerificationToken: " + token,
                "--data-binary", json.dumps(payload)]
        return self._split_headers(self._run(args))[1]


# ------------------------------------------------------------------- embedded JS payloads
def embedded_array(html, name):
    """The page's JS array, read strictly — an unparsable one stops the run.

    The reader itself lives in `eis_page` so the walk and the download share one parser
    rather than two that drift. Only the failure policy differs here, and it is the whole
    reason this wrapper exists: a download that cannot read the document array may be
    missing documents, and there is no partial success in this file.
    """
    try:
        return eis_page.embedded_array(html, name, strict=True)
    except eis_page.MalformedArray as exc:
        raise Fail(str(exc))


def downloadable(record):
    """The spec's own rule: a record is downloadable unless it says otherwise."""
    title = record.get("DocumentTitle") or {}
    return title.get("ShowDownloadIcon") is not False


def describe(record, section):
    title = record.get("DocumentTitle") or {}
    return {
        "section": section,
        "id": record.get("Id"),
        "title": (record.get("Title") or title.get("Title") or "").strip(),
        "document_link_type_code": record.get("DocumentLinkTypeCode") or "PRCDOC",
        "type_title": record.get("TypeTitle"),
        "type_code": record.get("TypeCode"),
        "publish_date": record.get("PublishDate"),
    }


# ------------------------------------------------------------------------------- fs safety
def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def safe_member(name):
    """Reject archive members that would escape the extraction directory."""
    if not name or name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
        return False
    return not os.path.isabs(name) and ":" not in name.split("/")[0][1:2]


def place(src, dest_dir, filename, seen):
    """Move a downloaded file into place, deduplicating by content.

    Identical bytes under the same name are one file. Different bytes under the same name
    both survive, distinguished by a hash suffix — silently overwriting one with the other
    is how a manifest ends up describing a file that is not there.
    """
    digest = sha256_file(src)
    key = (filename, digest)
    if key in seen:
        os.remove(src)
        return seen[key], digest, True

    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, filename)
    if os.path.exists(target):
        stem, ext = os.path.splitext(filename)
        target = os.path.join(dest_dir, "%s_%s%s" % (stem, digest[:10], ext))
    shutil.move(src, target)
    seen[key] = target
    return target, digest, False


# -------------------------------------------------------------------------------- the journal
# Written after every record, so an interrupted run keeps what it already paid for.
#
# This lesson has been learned twice on the same portal. An earlier downloader wrote only at
# the end and lost its downloads to one timeout; the next held a long run's work in memory
# before writing anything. A large tender is many minutes of downloading over a link that
# refuses a good share of runners — the run has to be resumable, or every failure costs the
# whole tender again.
#
# Partial success is still failure for the RUN (nothing incomplete reaches the success path).
# The journal changes what a failure costs, not what it means.
JOURNAL = "journal.jsonl"


def read_journal(out_dir):
    """Records finished by an earlier run, by record id. Unreadable lines are ignored."""
    path = os.path.join(out_dir, JOURNAL)
    done = {}
    if not os.path.exists(path):
        return done
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue                  # a line torn in half by a kill is not a record
            if entry.get("id") is not None:
                done[entry["id"]] = entry
    return done


def write_originals(out_dir, pid, manifest):
    """Every downloaded file in one archive, each stored once. Returns its path.

    ONE PATH, ONE MEMBER. A document can hang off more than one record, and the manifest
    records that on purpose — which record referenced a file is information. The archive is
    not the place to repeat it. Writing per record produced a ZIP with duplicate names,
    which `zipfile` permits with a warning and extractors resolve however they please.
    Measured on procurement 174527: 8 of its 21 documents were stored twice, 1.1 MB of a
    4.9 MB archive — 23% of it bytes nobody asked for, carried to SharePoint every run.
    """
    originals = os.path.join(out_dir, "eis_%s_originals.zip" % pid)
    with zipfile.ZipFile(originals, "w", zipfile.ZIP_DEFLATED, compresslevel=1,
                         allowZip64=True) as z:
        z.write(os.path.join(out_dir, "manifest.json"), "manifest.json")
        written = set()
        for rec in manifest:
            for f in rec["files"]:
                if f["path"] in written:
                    continue
                written.add(f["path"])
                z.write(os.path.join(out_dir, f["path"]), f["path"])
    with zipfile.ZipFile(originals) as z:
        if z.testzip() is not None:
            raise Fail("the originals archive failed its own integrity check")
    return originals


def append_journal(out_dir, entry):
    """One record, on disk, now. Everything resumable depends on this being immediate."""
    with open(os.path.join(out_dir, JOURNAL), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def still_on_disk(out_dir, entry):
    """Is what the journal claims about this record still true?

    Existence and size, not a re-hash: the point is to catch a wiped or truncated working
    directory, and re-hashing a 486 MB tender on every resume would tax the common case to
    describe the rare one. `verify.py` re-hashes the archive at the end regardless, so a
    corrupted file cannot reach the success path unnoticed.
    """
    for f in entry.get("files") or []:
        path = os.path.join(out_dir, f["path"])
        if not os.path.exists(path) or os.path.getsize(path) != f.get("size"):
            return False
    return bool(entry.get("files"))


# --------------------------------------------------------------------------- download path
def record_zip(curl, pid, record, workdir):
    """Primary route: the whole document as one archive. None when EIS will not give it."""
    url = "%s/EKEIS/Document/DownloadDocumentFilesInZip?%s" % (BASE, urlencode({
        "Id": record["id"],
        "DocumentLinkTypeCode": record["document_link_type_code"],
        "ProcurementIdentifier": pid}))
    dest = os.path.join(workdir, "record_%s.zip" % record["id"])
    try:
        headers = curl.get_file(url, dest)
    except Fail:
        return None
    if "text/html" in (headers.get("content-type") or "").lower():
        return None                                   # the refusal path is a 200 with HTML
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        return None
    try:
        with zipfile.ZipFile(dest) as z:
            if z.testzip() is not None:
                return None
            if not z.namelist():
                return None
    except zipfile.BadZipFile:
        return None
    return dest


def list_files(curl, pid, record, token):
    """Fallback route: open the document and read the physical files inside it."""
    payload = {"Id": record["id"], "FileId": None,
               "DocumentLinkTypeCode": record["document_link_type_code"],
               "ParentObjectTypeCode": "", "ParentId": None, "ParentIdentifier": None,
               "ProcurementIdentifier": pid, "StageIdentifier": None}
    body = curl.post_json(BASE + "/EKEIS/Document/ViewDocument", payload, token)
    if body.lstrip().startswith("{"):
        raise Fail("ViewDocument refused record %s: %s" % (record["id"], body[:200]))
    files = embedded_array(body, "ViewDocumentModel_Files_items")
    if not files:
        raise Fail("record %s returned no files and no record zip" % record["id"])
    return [{"file_id": f.get("Id"), "name": (f.get("Title") or "").strip(),
             "type_code": f.get("TypeCode")} for f in files]


def download_file(curl, pid, record, fil, workdir):
    url = "%s/EKEIS/Document/DownloadDocumentFile?%s" % (BASE, urlencode({
        "Id": record["id"],                            # DOCUMENT id, never the file id
        "FileId": fil["file_id"],
        "DocumentLinkTypeCode": record["document_link_type_code"],
        "ProcurementIdentifier": pid}))
    dest = os.path.join(workdir, "file_%s" % fil["file_id"])
    headers = curl.get_file(url, dest)
    if "text/html" in (headers.get("content-type") or "").lower():
        raise Fail("EIS returned an HTML page instead of file %s of record %s"
                   % (fil["file_id"], record["id"]))
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise Fail("file %s of record %s came back empty" % (fil["file_id"], record["id"]))
    return dest


def unpack_record_zip(path, dest_dir, record, seen, workdir):
    """Every member of a record archive, on disk, with traversal refused."""
    out = []
    with zipfile.ZipFile(path) as z:
        for member in z.namelist():
            if member.endswith("/"):
                continue
            if not safe_member(member):
                raise Fail("unsafe path in record %s archive: %r" % (record["id"], member))
            tmp = os.path.join(workdir, "x_%s" % hashlib.sha1(member.encode()).hexdigest()[:12])
            with z.open(member) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            name = os.path.basename(member) or "file"
            target, digest, dup = place(tmp, dest_dir, name, seen)
            out.append({"filename": os.path.basename(target), "original_name": name,
                        "path": target, "size": os.path.getsize(target),
                        "sha256": digest, "duplicate": dup})
    return out


# ------------------------------------------------------------------------------------ main
def fetch(url, out_dir, sections=None, register_uuid=None):
    page_url, pid = canonical_url(url)
    out_dir = os.path.abspath(out_dir)
    docs_dir = os.path.join(out_dir, "documents")
    os.makedirs(docs_dir, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="eis_")
    curl = Curl(workdir, page_url)

    # A throttled or interstitial response and an unpublished tender look identical unless
    # they are told apart. Two concurrent runs hitting EIS in the same second can leave one
    # of them holding a page without the marker, which the first version of this code
    # reported as "not published" — about a tender that plainly was.
    html, last = "", None
    for attempt in range(6):
        try:
            html = curl.get_text(page_url)
        except Fail as exc:
            # A connect timeout raises out of the transport, and the first version of this
            # loop never caught it — so the retry it exists for did nothing. EIS answers a
            # runner normally; it rate-limits briefly after a large download, and that is
            # exactly the moment the retry has to survive.
            last, html = str(exc), ""
        if eis_page.is_published(html):
            break
        if html:
            # EIS answered with a real page that is not a procurement. Keeping it is the
            # difference between diagnosing the next failure and guessing at it, which has
            # already cost this project several wrong conclusions — among them a full page
            # "without the published marker" that turned out to be the tender itself, served
            # in English. That is why the check is structural (eis_page.is_published).
            last = "%d bytes, not a procurement page: %s" % (len(html), _describe(html))
        if attempt < 5:
            time.sleep(20 * (attempt + 1))
    else:
        if html:
            evidence = os.path.join(out_dir, "unexpected_page.html")
            with open(evidence, "w", encoding="utf-8") as fh:
                fh.write(html)
            kept = " The page EIS did serve is saved at %s." % evidence
        else:
            kept = (" EIS did not answer at all. If IUB is reachable in the same run, this is "
                    "EIS refusing this address, not an egress problem.")
        raise Fail("%s: EIS never returned the procurement page in 6 attempts (last: %s). "
                   "This is a fetch failure, not proof that the tender is unpublished.%s"
                   % (page_url, last or "no response at all", kept))
    token_match = TOKEN_RE.search(html)
    if not token_match:
        raise Fail("no __RequestVerificationToken on the procurement page")
    token = token_match.group(1)

    # The page is in hand and it carries the tender's own facts — title, buyer, deadline,
    # value, CPV. Reading them here costs nothing and is usually the only chance: most live
    # procurements have no IUB notice to fall back on. `register_uuid` is the one thing the
    # page cannot be trusted for, so it comes down from the caller instead of up from here.
    procurement = eis_page.parse_notice(html, pid, register_uuid=register_uuid)
    if procurement:
        with open(os.path.join(out_dir, "procurement.json"), "w", encoding="utf-8") as fh:
            json.dump(procurement, fh, ensure_ascii=False, indent=2)
        print("  %s · %s · deadline %s"
              % (procurement.get("ref") or pid, procurement.get("buyer") or "unknown buyer",
                 procurement.get("deadline") or "not stated"), flush=True)

    records, withheld = [], []
    for section, var in (sections or SECTIONS):
        for raw in embedded_array(html, var):
            if downloadable(raw):
                records.append(describe(raw, section))
            else:
                # EIS says this record has no download. Recording it is what makes the
                # coverage claim provable from the PAGE onward rather than only from the
                # download onward: without this line, a record the portal withheld and a
                # record that never existed look identical in the manifest, and "nothing
                # was lost" becomes a statement nobody can check.
                withheld.append(describe(raw, section))
    if not records:
        raise Fail("no downloadable document records on %s" % page_url)
    if withheld:
        print("  %d record(s) published without a download — listed as withheld"
              % len(withheld), flush=True)

    done = read_journal(out_dir)
    if done:
        print("  resuming: %d record(s) already on disk" % len(done), flush=True)

    seen, manifest, fetched, last_bytes = {}, [], 0, 0
    for record in records:
        # A record an earlier run finished is not fetched again — that is the whole point of
        # the journal. It is re-fetched if the files it claims are gone or the wrong size.
        prior = done.get(record["id"])
        if prior and still_on_disk(out_dir, prior):
            manifest.append(prior)
            print("  record %-9s %-16s %2d file(s)  %s"
                  % (record["id"], "resumed", len(prior["files"]), record["title"][:48]),
                  flush=True)
            continue

        # EIS throttles a client that pulls hard, and the throttle escalates from a wrong page
        # to a refused TCP connection. Pausing between records costs seconds
        # against a run measured in minutes and keeps the next run possible at all. Resumed
        # records cost no request, so they earn no pause.
        # Sized by what the PREVIOUS record cost, because that is the load already imposed
        # and the only figure known before this one is asked for. Keeps the trailing pause
        # off the end of a tender, where nobody is waiting behind us.
        if fetched:
            time.sleep(pause_for(last_bytes))
        fetched += 1
        dest_dir = os.path.join(docs_dir, record["section"])
        zip_path = record_zip(curl, pid, record, workdir)
        if zip_path:
            files = unpack_record_zip(zip_path, dest_dir, record, seen, workdir)
            method = "record_zip"
            for f in files:
                f["file_id"] = None
        else:
            files = []
            for fil in list_files(curl, pid, record, token):
                tmp = download_file(curl, pid, record, fil, workdir)
                name = fil["name"] or ("file_%s" % fil["file_id"])
                target, digest, dup = place(tmp, dest_dir, name, seen)
                files.append({"filename": os.path.basename(target), "original_name": name,
                              "path": target, "size": os.path.getsize(target),
                              "sha256": digest, "duplicate": dup,
                              "file_id": fil["file_id"]})
            method = "individual_files"
        if not files:
            raise Fail("record %s produced no files by either route" % record["id"])
        entry = dict(record, download_method=method, files=[
            dict(f, path=os.path.relpath(f["path"], out_dir).replace("\\", "/")) for f in files])
        manifest.append(entry)
        append_journal(out_dir, entry)
        last_bytes = sum(f["size"] for f in entry["files"])
        print("  record %-9s %-16s %2d file(s)  %s"
              % (record["id"], method, len(files), record["title"][:48]), flush=True)

    shutil.rmtree(workdir, ignore_errors=True)

    total_files = sum(len(r["files"]) for r in manifest)
    manifest_doc = {"schema": 2, "procurement_id": pid, "source_url": page_url,
                    "records": len(manifest), "files": total_files,
                    "records_on_page": len(manifest) + len(withheld),
                    "sections": [name for name, _ in (sections or SECTIONS)],
                    "withheld_records": withheld, "documents": manifest}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest_doc, fh, ensure_ascii=False, indent=2)

    originals = write_originals(out_dir, pid, manifest)

    summary = {"procurement_id": pid, "source_url": page_url,
               # The pace this pack was fetched at. Without it, a later comparison of two
               # days cannot tell a faster portal from a shorter pause.
               "pause_seconds": PAUSE_BETWEEN_RECORDS,
               # `files` and `bytes` count references: a document two records share is
               # downloaded for each and counted for each, which is what the run cost.
               # `unique_files` counts paths, which is what the originals archive stores —
               # its member count is unique_files + 1, the one being manifest.json. Both
               # numbers are honest; carrying both is what keeps an audit of one against
               # the other from reading as loss.
               "records": len(manifest), "files": total_files,
               "unique_files": len({f["path"] for r in manifest for f in r["files"]}),
               "bytes": sum(f["size"] for r in manifest for f in r["files"]),
               "originals_zip": os.path.basename(originals),
               "originals_sha256": sha256_file(originals),
               "methods": {m: sum(1 for r in manifest if r["download_method"] == m)
                           for m in ("record_zip", "individual_files")}}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Download every public document of one EIS procurement.")
    ap.add_argument("url")
    ap.add_argument("--out", default="out")
    ap.add_argument("--skip-archive", action="store_true",
                    help="take only the governing documents, not the superseded ones "
                         "(a small share of the bytes, a large share of the files)")
    args = ap.parse_args(argv)
    sections = SECTIONS[:1] if args.skip_archive else SECTIONS
    try:
        s = fetch(args.url, args.out, sections)
    except Fail as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        return 2
    print("OK  procurement %s · %d records · %d files · %.1f MB · zip %s"
          % (s["procurement_id"], s["records"], s["files"], s["bytes"] / 1048576.0,
             s["originals_sha256"][:16]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
