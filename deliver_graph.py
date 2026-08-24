#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deliver a pack tree to a Microsoft Graph drive, so a consumer that cannot fetch artifacts
can still read the day.

    python3 deliver_graph.py --packs packs --shard 1 --date 2026-08-10

EACH TENDER HAS ONE HOME, AND A DAY IS A LIST OF WHAT MOVED.

    tenders/<pid>/          the tender itself: doc/, its manifests, index.json, state.json,
                            seen.json, runs/<date>.json, and the whole thing as one ZIP
    <date>/shards/…/        the day: the shard index, which carries each tender's change
                            record inline, beside the shard's own accounting files

A tender delivered a second time uploads only the documents that were not there before.
Everything else — every unchanged document, which on an ordinary re-fetch is nearly all of
them — stays exactly where it already is, and `index.json` in the home goes on naming it,
so a reader that wants the tender whole never has to know which day any part of it arrived.

A tender that did not move at all writes only `seen.json` and `runs/<date>.json`, both of
which are a few hundred bytes. Nothing else in the home needs rewriting, because nothing
about the tender is different.

READING THE DESTINATION IS WHAT MAKES THAT POSSIBLE. A run remembers nothing: the runner is
new and the previous run's artifact is precisely what a consumer cannot reach. So the state
each tender was left in is read back out of the drive itself, with the credential this job
already holds — see `get`. No cache, no committed file, no second service.

WHY THIS EXISTS. A GitHub artifact's metadata is served by api.github.com but its bytes are
a 302 to `*.blob.core.windows.net` — a different host, from a rotating pool. A consumer
allowed to reach the first and not the second can list an artifact, see its size, see that
it has not expired, and download none of it. That is not a permissions problem anyone can
grant their way out of; the bytes have to arrive somewhere the consumer may already talk to.

WHAT THIS KNOWS ABOUT THE DESTINATION: NOTHING. Tenant, client, drive and path all arrive in
the environment. This file names no organisation, no site and no folder, and it prints
counts rather than paths, so neither the repository nor the run log says where the day went.
The one thing it cannot hide is the TLS connection itself: a runner talking to Graph resolves
`login.microsoftonline.com` and `graph.microsoft.com`, and whoever can watch the runner's
network sees that much. Scope the credential to one site (`Sites.Selected`) and a leak buys
the reader that site and nothing else.

WHY THE DOCUMENT PATHS ARE FLATTENED. Paths inside a pack run deep — an archive nested a
few levels holds documents whose relative path is already hundreds of characters — and a
SharePoint destination prefix adds its own on top of a hard limit for the whole
server-relative path. A minority of a day's files therefore fail to upload while the rest
succeed, and a delivery that loses a fraction of itself and reports success is the failure
mode this pipeline exists to refuse. So `normalized/<deep>/<path>/document.md` is written
as `doc/<digest>.md` and `manifest_normalized.json` is rewritten to match.

That rewrite is safe because of how a consumer reads: it opens `entry["markdown_path"]`
and never parses its shape, while the name a person sees and cites is `entry["source"]`,
which is left exactly as the carrier wrote it.

The digest is also what lets an unchanged document stay where it lies: a name derived from
the file's own bytes is the same name tomorrow. See `flatten`.

PARTIAL SUCCESS IS FAILURE, here as everywhere else in this tool. Any file that does not
land fails the run, because a day that looks delivered and is not costs more than one that
plainly broke.
"""

import argparse
import country
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import changes

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com/%s/oauth2/v2.0/token"

# Everything a consumer reads, and nothing it does not. The originals, the unpacked media
# and the drawing binaries stay in the artifact: they are 97% of the bytes and no reader
# opens them.
KEEP_NAMES = {
    "manifest.json", "summary.json", "procurement.json",
    "manifest_normalized.json", "document.md",
    "structure.json",
    "done.txt", "failed.txt", "withdrawn.txt", "resolved.tsv",
}
# THE CODES WORTH ASKING AGAIN ABOUT.
#
# 429 and the 5xx are Graph under load. 409 is Graph under CONTENTION: four shards deliver at
# once, a tender can legitimately land in two of them, and a path whose parent folder two
# requests create in the same instant comes back as a conflict. It resolves on the next
# attempt, because by then the folder exists.
#
# It was not in this set, so one such collision ended a whole shard's delivery — after the
# tenders had been fetched, extracted and uploaded as an artifact, which is the most
# expensive possible moment to give up. Retrying is bounded and a genuine, permanent conflict
# still fails the run loudly after the attempts are spent.
RETRY = (409, 429, 500, 502, 503, 504)

SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
# Microsoft Graph requires non-final upload fragments to be a multiple of 320 KiB.
# 32 such blocks are 10 MiB: efficient and far below Graph's fragment ceiling.
UPLOAD_CHUNK = 32 * 320 * 1024


def env(name):
    """One coordinate, or a refusal that names it. Surrounding whitespace is not a value.

    A secret pasted into a web form or piped into `gh secret set` routinely arrives with a
    trailing newline, and nothing downstream survives it: the newline lands in a URL and
    the error is an `InvalidURL` from inside urllib, four frames deep, quoting a string the
    log masks to `***`. The value is never printed here — only the name of the variable
    holding it, which is the part the reader needs and the part that is not a secret.
    """
    v = (os.environ.get(name) or "").strip()
    if not v:
        sys.exit("missing environment: %s" % name)
    return v


def coordinate(name, value, pattern=None):
    """Refuse a coordinate that cannot be what it claims to be, naming the variable.

    Whitespace INSIDE a value survives `strip`, and the two secrets most easily confused
    here are a tenant id and a destination path — one a GUID, the other a folder name with
    spaces in it. Swapped, the run dies in urllib with a masked string and no hint which of
    six variables to look at. This is the check that turns twenty minutes of log reading
    into one line.
    """
    if any(c.isspace() for c in value):
        sys.exit("%s contains whitespace, so it cannot be used in a request. "
                 "Check it is not another secret's value." % name)
    if pattern and not pattern.match(value):
        sys.exit("%s is not shaped like the value this expects. "
                 "Check it is not another secret's value." % name)
    return value


TENANT = re.compile(r"^[0-9a-fA-F-]{36}$")


def graph_token():
    """The access token, from coordinates checked before they are spent on a request."""
    return token(coordinate("GRAPH_TENANT_ID", env("GRAPH_TENANT_ID"), TENANT),
                 coordinate("GRAPH_CLIENT_ID", env("GRAPH_CLIENT_ID")),
                 env("GRAPH_CLIENT_SECRET"))


def token(tenant, client_id, client_secret):
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(LOGIN % tenant, data=body)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["access_token"]


def escaped(path):
    """A drive path as one addressable segment sequence. Never printed."""
    return "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))


def get(url, tok, tries=4):
    """One GET, retried on the codes Graph returns under load. Bytes, or None for absent.

    READING THE DRIVE IS HOW THIS DELIVERY REMEMBERS. A run has no memory of the one before
    it — a runner is new, its disk is empty, and the artifact of the previous run is exactly
    the thing a consumer cannot reach. So the state each tender was left in last time is read
    back out of the destination itself, which is the one place that is both durable and
    already reachable with the credential this job holds. No cache, no committed file, no
    second service: the drive already holds the answer, and asking it costs one request.

    A 404 is the ordinary first answer for every tender nobody has fetched before, so it is
    a value and not an error.
    """
    for attempt in range(tries):
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + tok)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in RETRY and attempt < tries - 1:
                wait = int(e.headers.get("Retry-After") or (2 ** attempt))
                time.sleep(min(wait, 60))
                continue
            raise SystemExit("read failed: HTTP %d after %d attempt(s)" % (e.code, attempt + 1))
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit("read failed: transport error after %d attempts" % tries)
    raise SystemExit("read failed: retries exhausted")


def item_at(drive, path, tok):
    """The drive item at `path`, or None. Its `id` is half of the address a reader needs."""
    body = get("%s/drives/%s/root:/%s" % (GRAPH, drive, escaped(path)), tok)
    return json.loads(body.decode("utf-8")) if body else None


def json_at(drive, path, tok):
    """One JSON file off the drive, or None. An unreadable one is None, not a crash: a
    corrupt state file must cost a tender its delta, never the whole delivery."""
    body = get("%s/drives/%s/root:/%s:/content" % (GRAPH, drive, escaped(path)), tok)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def text_at(drive, path, tok):
    body = get("%s/drives/%s/root:/%s:/content" % (GRAPH, drive, escaped(path)), tok)
    return body.decode("utf-8") if body else ""


def bytes_at(drive, path, tok):
    """Binary content at a Graph path, or None."""
    return get("%s/drives/%s/root:/%s:/content" % (GRAPH, drive, escaped(path)), tok)


def put(url, data, tok, content_type="application/octet-stream", tries=5):
    """One upload, retried on the throttling and transient codes Graph actually returns.

    Graph answers 429 with Retry-After under sustained writes, and this delivery is a couple
    of thousand of them in a row. Honouring the header is the difference between a delivery
    that finishes and one that half-finishes.
    """
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method="PUT")
        req.add_header("Authorization", "Bearer " + tok)
        req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code in RETRY and attempt < tries - 1:
                wait = int(e.headers.get("Retry-After") or (2 ** attempt))
                time.sleep(min(wait, 60))
                continue
            # The body can quote the destination path; report the code only.
            raise SystemExit("upload failed: HTTP %d after %d attempt(s)"
                             % (e.code, attempt + 1))
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit("upload failed: transport error after %d attempts" % tries)
    raise SystemExit("upload failed: retries exhausted")


def upload_stream(drive, dest, stream, size, tok):
    """PUT one seekable stream without keeping a day-sized archive in memory."""
    safe = "/".join(urllib.parse.quote(p, safe="") for p in dest.split("/"))
    if size < SIMPLE_UPLOAD_LIMIT:
        data = stream.read()
        put("%s/drives/%s/root:/%s:/content" % (GRAPH, drive, safe), data, tok)
        return

    req = urllib.request.Request(
        "%s/drives/%s/root:/%s:/createUploadSession" % (GRAPH, drive, safe),
        data=json.dumps({"item": {"@microsoft.graph.conflictBehavior": "replace"}}).encode(),
        method="POST")
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        url = json.load(r)["uploadUrl"]

    offset = 0
    while offset < size:
        data = stream.read(min(UPLOAD_CHUNK, size - offset))
        if not data:
            raise SystemExit("upload session failed: source ended at %d of %d bytes"
                             % (offset, size))
        end = offset + len(data) - 1
        for attempt in range(5):
            creq = urllib.request.Request(url, data=data, method="PUT")
            creq.add_header("Content-Range", "bytes %d-%d/%d" % (offset, end, size))
            try:
                with urllib.request.urlopen(creq, timeout=600) as r:
                    if r.status not in (200, 201, 202):
                        raise SystemExit("upload session failed: HTTP %d" % r.status)
                break
            except urllib.error.HTTPError as e:
                if e.code in RETRY and attempt < 4:
                    wait = int(e.headers.get("Retry-After") or (2 ** attempt))
                    time.sleep(min(wait, 60))
                    continue
                raise SystemExit("upload session failed: HTTP %d after %d attempt(s)"
                                 % (e.code, attempt + 1))
            except urllib.error.URLError:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit("upload session failed: transport error after 5 attempts")
        offset = end + 1


def upload(drive, dest, data, tok):
    """PUT one in-memory file. Paths are escaped, and destinations never reach stdout."""
    upload_stream(drive, dest, io.BytesIO(data), len(data), tok)


def upload_file(drive, dest, path, tok):
    """PUT one file from disk, streaming upload-session chunks when it is large."""
    with open(path, "rb") as fh:
        upload_stream(drive, dest, fh, os.path.getsize(path), tok)


def zip_write(zf, name, data):
    """One deterministic ZIP member with a portable path and permissions."""
    info = zipfile.ZipInfo(name.replace("\\", "/"), (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def tender_members(pack_files, pid, structures, entry_bytes):
    """Everything one tender publishes, in the order it is published.

    ONE LIST, TWO RENDERINGS. A tender is delivered twice — as a folder somebody can open
    without downloading anything, and as one archive somebody can take whole. Both are
    built from this, so a file that reaches one always reaches the other; two independent
    assemblies would drift the first time either changed, and the drift would be invisible
    until a reader compared them.

    `index.json` is last on purpose, in the folder for the same reason as in the ZIP: it is
    written after every file it names, so its presence is the proof the rest arrived.
    """
    members = sorted(pack_files)
    if structures:
        members.append(("structure.json",
                        json.dumps({"schema": "structure/1", "pid": pid,
                                    "documents": structures},
                                   ensure_ascii=False).encode("utf-8")))
    members.append(("index.json", entry_bytes))
    return members


def tender_archive(members):
    """The tender as one ZIP — one request however many documents it holds."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", allowZip64=True) as zf:
        for rel, data in members:
            zip_write(zf, rel, data)
    return out.getvalue()


def selection(pack):
    """(relative path, absolute path) for everything a consumer reads in one pack."""
    for root, _, names in os.walk(pack):
        for n in sorted(names):
            ap = os.path.join(root, n)
            rel = os.path.relpath(ap, pack).replace(os.sep, "/")
            if n in KEEP_NAMES:
                yield rel, ap


def flatten(pack):
    """Content-addressed names for the deep ones, plus the manifest rewritten to match.

    Returns (list of (destination-relative path, bytes)) for one pack. The manifest is
    emitted from memory rather than from disk precisely because its `markdown_path` values
    must agree with where the files actually land.

    THE NAME MUST FOLLOW THE FILE, NOT ITS POSITION. A name numbered by sort order within
    the pack changes for every document behind one the buyer inserts, which would make each
    delivery re-send documents nobody touched. `changes.document_key` derives the name from
    the file's own bytes and its place in the pack, so an unchanged document has the same
    address on every future day and can be skipped.

    IN THE DELIVERED COPY, `markdown_path` IS RELATIVE TO THE TENDER'S ROOT, not to
    `normalized/` as it is inside the pack: the text sits in `doc/` beside `normalized/`
    rather than under it, and a path relative to a directory the file is not in would be a
    small lie a reader has to know about. `index.json` hands out the same string, so the two
    agree without either prefixing anything.
    """
    out, renamed, manifest_rel, manifest = [], {}, None, None
    docs, structures = [], {}
    for rel, ap in selection(pack):
        if rel.endswith("/document.md") or rel == "document.md":
            docs.append((rel, ap))
        elif rel.endswith("manifest_normalized.json"):
            manifest_rel = rel
            with open(ap, "rb") as fh:
                manifest = json.loads(fh.read().decode("utf-8"))
        elif os.path.basename(rel) == "structure.json":
            # Held back, not uploaded where it lies. One sidecar per document would add a PUT
            # per readable Word file — hundreds a day against a delivery that already retries
            # on 429 — so they are merged into one file at the tender's root below. Keyed by
            # the directory, because that is what a sidecar shares with its document.
            with open(ap, "rb") as fh:
                structures[os.path.dirname(rel)] = fh.read()
        else:
            with open(ap, "rb") as fh:
                out.append((rel, fh.read()))

    # The key is derived from the manifest entry that PRODUCED the file, so it has to be
    # taken before anything is renamed. A duplicate entry — the same file reached through a
    # second record — names the same Markdown and must land on the same address, so only
    # primary entries assign a name and every entry is rewritten from the result.
    key_of = {}
    for entry in (manifest or {}).get("documents", []):
        mp = entry.get("markdown_path")
        if not mp or entry.get("also_listed_under"):
            continue
        key_of["normalized/" + mp.lstrip("/")] = changes.document_key(
            entry.get("original_sha256"), entry.get("source"))

    for rel, ap in sorted(docs):
        # A document the manifest does not name should not exist. If one ever does, it is
        # still delivered, under a name derived from where it sat — never dropped, and never
        # given an address that could collide with a document that has a digest.
        key = key_of.get(rel) or changes.document_key("", rel)
        short = "doc/%s.md" % key
        renamed[rel] = short
        with open(ap, "rb") as fh:
            out.append((short, fh.read()))

    if manifest is not None:
        for entry in manifest.get("documents", []):
            mp = entry.get("markdown_path")
            if not mp:
                continue
            was = "normalized/" + mp.lstrip("/")
            if was in renamed:
                # `source` is untouched: it is what a person opens and what a citation names.
                entry["markdown_path"] = renamed[was]
        out.append((manifest_rel,
                    json.dumps(manifest, ensure_ascii=False).encode("utf-8")))
    # The merged sidecar keys on the FLATTENED document name, because that is the only name a
    # reader ever sees on the drive. A structure whose document did not survive selection is
    # dropped rather than delivered pointing at nothing.
    merged = {}
    for directory, blob in structures.items():
        short = renamed.get(directory + "/document.md")
        if not short:
            continue
        try:
            merged[short] = json.loads(blob.decode("utf-8"))
        except ValueError:
            continue

    return out, manifest, merged


def publishable(packs):
    """The tenders this run may publish, or None when nobody kept the accounts.

    A pack directory exists for every tender the downloader STARTED. One whose extraction
    then failed is named in `failed.txt`; one EIS declines to show at all is named in
    `withdrawn.txt`; neither is in `done.txt`, and both leave a directory behind holding
    whatever was written before they stopped. Delivering those publishes a partial tender
    and — worse — records a fingerprint saying that partial tender is what the procurement
    is. The next run compares against that, finds the page unmoved, calls it unchanged and
    skips it, so the gap never closes.

    PARTIAL SUCCESS IS FAILURE, and this is where the delivery has to honour it. Measured on
    one four-shard run over three days of publications: 89 fetches, of which two failed in
    extraction and one more was withheld — and every one of their packs was on disk beside
    the successful ones.

    No `done.txt` at all means a caller that fetched a single tender by hand rather than a
    shard that kept accounts. Everything is delivered, which is what that caller asked for.
    """
    path = os.path.join(packs, "done.txt")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig") as fh:
        return {line.strip() for line in fh if line.strip()}


def load_json(pack, *rel):
    p = os.path.join(pack, *rel)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


# WHERE EIS SERVES THE FILE A DOCUMENT WAS EXTRACTED FROM.
#
# A consumer that shows somebody the sentence which made a tender interesting is asked, next,
# for the document itself — and the index is the only file it has open. The ids that address
# a download were learned during the walk this tool already did; they sit in `manifest.json`
# and in no file the reader opens. Rebuilding them costs a page fetch and a POST per record,
# against a portal that refuses a third of the addresses that ask.
#
# TWO ROUTES OUT, BECAUSE THE DOWNLOAD HAD TWO. A record fetched file by file gives every
# file its own id, and the link is that exact file. A record EIS would only hand over whole
# gives its members no id at all — `file_id` is None — so the link is the record's archive,
# which is also what the person clicking it receives.
#
# A file the extractor found INSIDE a published archive is the same case from the other end:
# `original_file` names the archive that was downloaded, so the link lands on the archive and
# `source` goes on naming the member to open. Both are honest; neither pretends EIS will
# serve a member on its own, because it will not.
EIS_DOCUMENT = "https://www.eis.gov.lv/EKEIS/Document/%s?%s"


def download_url(pid, downloaded, entry):
    """The EIS address of the file this document came out of, or None.

    None is an answer, and the only safe one when `manifest.json` cannot vouch for the link:
    a pack written before this existed, or a record the manifest does not carry. A guessed
    URL is worse than an absent one — it downloads some other tender's document and says
    nothing about having done so.
    """
    record = next((r for r in (downloaded or {}).get("documents") or []
                   if str(r.get("id")) == str(entry.get("record_id"))
                   and r.get("section") == entry.get("section")), None)
    if record is None:
        return None
    files = [f for f in record.get("files") or []
             if f.get("filename") == entry.get("original_file")]
    if not files:
        return None
    # `document_link_type_code` is what the downloader sent, and it defaults to PRCDOC there
    # for a record that arrived without one. Mirroring that default keeps the link identical
    # to the request that actually fetched the bytes.
    code = record.get("document_link_type_code") or "PRCDOC"
    file_id = files[0].get("file_id")
    route = "DownloadDocumentFile" if file_id is not None else "DownloadDocumentFilesInZip"
    params = [("Id", record.get("id"))]
    if file_id is not None:
        params.append(("FileId", file_id))
    params += [("DocumentLinkTypeCode", code), ("ProcurementIdentifier", pid)]
    return EIS_DOCUMENT % (route, urllib.parse.urlencode(params))


def index_entry(pid, pack, manifest):
    """One tender: what is here, and what it is worth opening.

    Delivered twice — once inside the tender as its own `index.json`, and once as a line in
    the shard index. The shard index is how a reader learns which tenders exist; the copy
    inside the tender is what it opens when it judges one, so that reading about ten
    documents does not cost reading about six hundred.

    THIS IS THE WHOLE POINT OF THE DELIVERY, AND IT IS A TINY FRACTION OF IT. A day of
    extracted text runs to tens of millions of characters; no reader holds a hundredth of
    that in one context window. This index is a few hundred kilobytes for a whole day. So
    the reader reads the index, decides which documents are worth the window, and opens
    only those.

    Everything here is the carrier's own data — the normalized manifest, `manifest.json` and
    the procurement page. No judgement is made and none can be: which tenders matter is the
    consumer's business and never travels through this repository.
    """
    proc = load_json(pack, "procurement.json") or {}
    downloaded = load_json(pack, "manifest.json") or {}

    docs = []
    for e in (manifest or {}).get("documents", []):
        mp = e.get("markdown_path")
        if not mp or e.get("also_listed_under"):
            continue
        doc = {
            "path": mp.lstrip("/"),        # already flattened, tender-root relative; open this
            "name": os.path.basename(e.get("source") or ""),
            "source": e.get("source"),                # the real path, for a citation
            "section": e.get("section"),
            "record": e.get("record_title"),
            "chars": e.get("markdown_chars"),
        }
        url = download_url(pid, downloaded, e)
        if url:
            doc["download"] = url       # where EIS serves the file this text came out of
        docs.append(doc)

    return {
        "pid": pid,
        "key": "EIS:%s" % pid,
        "title": proc.get("title"),
        "buyer": proc.get("buyer"),
        "buyer_reg": proc.get("buyer_reg"),
        "deadline": proc.get("deadline"),
        "value": proc.get("value"),
        "currency": proc.get("currency"),
        "cpv": proc.get("cpv"),
        "ref": proc.get("ref"),
        "link": proc.get("link"),
        "iub_uuid": proc.get("iub_uuid"),
        # How it is bought and what is bought, in the buyer's own words. Both are read off the
        # page, so a consumer showing them to a person is quoting rather than deciding.
        #
        # `profile` rides along because it is the only one of the three that does not change
        # language: EIS serves some pages in English, and the same tender then says
        # `Construction works` where another says `Būvdarbi`. A column keyed on the display
        # string quietly grows two labels for one thing; `PIL_Atklāts_konkurss` is stable
        # whatever language the page was served in.
        "procedure": proc.get("procedure"),
        "profile": proc.get("profile"),
        "work_kind": proc.get("work_kind"),
        "documents": docs,
        # A file nobody could decode is not an absent file, and the reader has to know.
        "unreadable": [
            {"file": g.get("file"), "reason": g.get("reason")}
            for g in (manifest or {}).get("unreadable_files", [])
        ],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deliver a pack tree to a Graph drive.")
    ap.add_argument("--packs", required=True, help="directory holding <pid>/ pack folders")
    ap.add_argument("--shard", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD, the run's own date")
    # THE EXTRACTOR'S OWN VERSION, RECORDED BESIDE THE TEXT IT WROTE. Without it a tender
    # re-read by a newer `normalize.py` is indistinguishable from a tender the buyer edited,
    # and a reader watching for amendments is handed a pipeline upgrade as news. It defaults
    # to a digest of the extraction path rather than to the run's commit: see
    # `changes.PIPELINE_FILES` for why the difference is the whole corpus.
    ap.add_argument("--tool", default=None,
                    help="the version that produced this text; defaults to the pipeline digest")
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""),
                    help="the workflow run this delivery came from")
    country.add_argument(ap)
    args = ap.parse_args(argv)

    drive = env("GRAPH_DRIVE_ID")
    # DERIVED FROM THE COUNTRY, NOT CONFIGURED BESIDE IT. `GRAPH_DEST_ROOT` names the
    # project's `work/`; the country folder under it is this run's, and a run that read one
    # country cannot address another's folder because it never learns the path separately.
    code = country.resolve(args.country, os.environ)
    base = country.destination(env("GRAPH_DEST_ROOT"), code)
    tok = graph_token()

    root = "%s/%s/shards/eis-batch-shard-%s" % (base, args.date, args.shard)
    files = bytes_sent = members = 0
    sent_docs = carried_docs = 0
    tally = {"new": 0, "changed": 0, "unchanged": 0}

    # The shard's own arithmetic rides at the top, beside the change records, exactly as it
    # sits in the artifact: `collect` reads it to prove a whole day arrived.
    for name in ("done.txt", "failed.txt", "withdrawn.txt", "resolved.tsv", "accounts.json"):
        p = os.path.join(args.packs, name)
        if os.path.exists(p):
            with open(p, "rb") as fh:
                data = fh.read()
            upload(drive, "%s/%s" % (root, name), data, tok)
            files += 1
            bytes_sent += len(data)

    # THE RUN THAT WROTE IT, because a day folder outlives the run that filled it. Fetching
    # one date twice leaves the earlier shard indexes in place, so a shard that delivered
    # nothing this time is still represented by last time's index — and the day counts it
    # present and calls itself complete. Measured: a shard died mid-delivery and `day.json`
    # reported all four present anyway. `collect_day` compares this against its own run id.
    # The day's whole target list as this shard saw it, carried so `collect_day` can tell a
    # short day from a full one — see `batch.write_accounts` for what it measures and why.
    index = {"date": args.date, "shard": args.shard, "run_id": args.run_id or None,
             "accounts": load_json(args.packs, "accounts.json"), "tenders": []}
    allowed, withheld = publishable(args.packs), []
    for pid in sorted(os.listdir(args.packs)):
        pack = os.path.join(args.packs, pid)
        if not os.path.isdir(pack):
            continue
        if allowed is not None and pid not in allowed:
            withheld.append(pid)
            continue
        pack_files, manifest, structures = flatten(pack)
        # One file per tender, keyed by the flattened document name — what Word keeps as a
        # paragraph property rather than as text, so a consumer can rebuild a clause number
        # instead of counting paragraphs and being wrong. Absent when nothing in the tender
        # was a numbered Word document, which is the ordinary case for a pack of PDFs.

        # WHAT THIS TENDER LOOKED LIKE LAST TIME, ASKED OF THE DRIVE ITSELF. Absent for one
        # nobody has fetched before, which is not an error and is the answer for every
        # tender on the first day this runs.
        home = "tenders/%s" % pid
        home_root = "%s/%s" % (base, home)
        current = changes.fingerprint(pid, load_json(pack, "procurement.json"),
                                      load_json(pack, "manifest.json"), manifest,
                                      tool=args.tool or changes.pipeline_version(),
                                      parser=changes.parser_version())
        was_seen = json_at(drive, "%s/seen.json" % home_root, tok)
        record = changes.diff(json_at(drive, "%s/state.json" % home_root, tok), current,
                             date=args.date, run_id=args.run_id or None, seen=was_seen)
        record.update({"home": home, "shard": args.shard})
        tally[record["status"]] = tally.get(record["status"], 0) + 1

        entry = index_entry(pid, pack, manifest)
        # Enough of the tender to triage the day's changes without opening a single home.
        for field in ("title", "buyer", "deadline"):
            record[field] = entry.get(field)
        # `run_file` is home-relative: this day's record for this tender lives in the
        # tender, where it is indexed beside every other day that touched it.
        entry.update({"home": home, "archive": "%s.zip" % pid, "index_file": "index.json",
                      "run_file": "runs/%s.json" % args.date})

        # THE SAME LINE AGAIN, ONE TENDER WIDE, INSIDE THE TENDER — because of who reads it.
        # A reader that judges tender by tender opens one tender at a time, and making it
        # pull the whole shard index to find one entry costs 4k-30k tokens to learn about
        # documents it will not open. Its own copy is a few hundred bytes to a few thousand.
        # The shard index stays: it is what enumerates the tenders in the first place.
        #
        # It goes after that tender's files, for the same reason the shard index goes last:
        # an index that exists was written after every document it names.
        entry_bytes = json.dumps(dict(entry, date=args.date, shard=args.shard),
                                 ensure_ascii=False).encode("utf-8")
        contents = tender_members(pack_files, pid, structures, entry_bytes)

        # THE DAY'S VERDICT RIDES IN THE SHARD INDEX AND NOT IN THE TENDER'S OWN COPY, which
        # is written above without it. A home is read months after the day that filled it,
        # and a `status: changed` frozen into it would be answering a question about a date
        # the reader never asked about — as current, and duplicating the record `runs/`
        # already keeps. What belongs to the day stays with the day.
        entry.update({"status": record["status"], "change": record})

        # ONLY WHAT IS NOT ALREADY THERE. The home accumulates: a document is uploaded the
        # day it appears and is never uploaded again, because its name is its digest and the
        # bytes at that name cannot have become different bytes. `index.json` still names
        # every document the tender has, so a reader that wants all of it never has to know
        # which day any part of it arrived on.
        wanted = {"doc/%s.md" % k for k in changes.documents_to_send(record, current)}
        to_send = [(rel, data) for rel, data in contents
                   if not rel.startswith("doc/") or rel in wanted]
        sent_docs += len(wanted)
        # A re-extraction re-sends the documents it has in common, so they were not carried.
        carried_docs += 0 if record.get("reextracted") else record.get("carried_over", 0)

        # THE ARCHIVE IS THE WHOLE TENDER AND IS REBUILT ONLY WHEN THE TENDER MOVED. It is
        # one request for a reader taking everything, so it cannot be a delta — but on a day
        # nothing changed the copy on the drive is already that same whole tender, and
        # re-sending it is the one upload big enough to undo the saving the rest of this
        # makes. A re-extraction counts as movement, because the text inside it came from an
        # extractor version this run no longer agrees with.
        # A TENDER THAT DID NOT MOVE WRITES ONLY THE TWO SMALL PER-RUN FILES. Everything
        # above this line — the archive, the documents, the manifests, `index.json`,
        # `state.json` — already describes the tender correctly, because nothing about the
        # tender is different. Rewriting them costs megabytes per tender per day to advance a
        # timestamp, and on an ordinary day almost every tender is in exactly this position.
        #
        # The archive is the largest single item and cannot be a delta: it is one request for
        # a reader taking everything. A re-extraction counts as movement, because the text
        # inside came from an extractor version this run no longer agrees with.
        #
        # THE HOME IS NEVER CLEARED. A digest name cannot be reused for different bytes, so
        # a superseded document is not litter left by a tender that shrank — it is the
        # previous version, still readable, and `runs/` says when it stopped being current.
        #
        # INDEX AFTER THE DOCUMENTS, STATE AFTER THE INDEX. `index.json` is the reader's proof
        # that the home is whole; `state.json` is the NEXT RUN's proof of what it may skip,
        # and a state file that landed before the documents it vouches for would let tomorrow
        # carry over text that is not there. `contents` ends with `index.json`, so the order
        # below is the order of the rule.
        if changes.refreshed(record):
            archive_bytes = tender_archive(contents)
            upload(drive, "%s/%s.zip" % (home_root, pid), archive_bytes, tok)
            files += 1
            bytes_sent += len(archive_bytes)
            members += len(contents)

            for rel, data in to_send:
                upload(drive, "%s/%s" % (home_root, rel), data, tok)
                files += 1
                bytes_sent += len(data)

            state_bytes = json.dumps(current, ensure_ascii=False).encode("utf-8")
            upload(drive, "%s/state.json" % home_root, state_bytes, tok)
            files += 1
            bytes_sent += len(state_bytes)

        # WHEN IT WAS LOOKED AT, AND WHAT THAT LOOK FOUND. Both are facts about the run rather
        # than about the tender, which is why they are kept out of the fingerprint and why
        # they are the only two files an unchanged tender costs. `runs/<date>.json` is one
        # file per date, so fetching a date twice rewrites that date's record and leaves every
        # other one alone; between them the runs are what `state.json` can be rebuilt from.
        seen_bytes = json.dumps(changes.seen(pid, was_seen, record, args.date),
                                ensure_ascii=False).encode("utf-8")
        record_bytes = json.dumps(record, ensure_ascii=False).encode("utf-8")
        for dest, data in (("seen.json", seen_bytes),
                           ("runs/%s.json" % args.date, record_bytes)):
            upload(drive, "%s/%s" % (home_root, dest), data, tok)
            files += 1
            bytes_sent += len(data)

        index["tenders"].append(entry)

    # THE INDEX GOES LAST, ON PURPOSE, AND IT IS THE SHARD'S ONLY OUTPUT BESIDES ITS
    # ACCOUNTING FILES. Its presence is the reader's proof that this shard arrived whole: a
    # delivery that died halfway leaves finished homes with no shard index naming them, and an
    # index that exists was written after every tender it names.
    index_bytes = json.dumps(index, ensure_ascii=False).encode("utf-8")
    upload(drive, "%s/index.json" % root, index_bytes, tok)
    files += 1
    bytes_sent += len(index_bytes)

    # Counts, never the destination. `carried` is the number this arrangement exists to make
    # large: documents that were already on the drive and did not travel again.
    print("delivered shard %s for %s: %d SharePoint files, %.1f MB, %d ZIP members "
          "(index: %d tenders, %.0f KB)"
          % (args.shard, args.date, files, bytes_sent / 1e6, members,
             len(index["tenders"]), len(index_bytes) / 1e3))
    print("  %d new, %d changed, %d unchanged · %d document(s) sent, %d carried over"
          % (tally["new"], tally["changed"], tally["unchanged"], sent_docs, carried_docs))
    # Named, never silent: a pack held back is a tender the reader will not find in this day.
    # Why it is not in `done.txt` is the shard's business to say — extraction that did not
    # finish lands in `failed.txt`, a tender EIS declines to show lands in `withdrawn.txt`,
    # and both leave a directory behind. Naming one reason here would be wrong half the time.
    if withheld:
        print("  %d pack(s) not published — done.txt does not name them: %s"
              % (len(withheld), ", ".join(withheld)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
