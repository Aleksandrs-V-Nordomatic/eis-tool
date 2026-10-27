"""Reads one Latvian EIS procurement page and downloads its files, one request at a time.

No login and no browser: the flow is the one EIS offers a guest.

A row of `ActualDocuments_items` is a document record, not a file. Its files are listed by
`ViewDocument`; `DownloadDocumentFile` then takes the record id as `Id` and the file id as
`FileId`. `ProcurementIdentifier` is required on every call.
"""

import hashlib
import json
import os
import re
import subprocess
import time
import zipfile
from urllib.parse import urlencode, urlparse

import eis_page

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BASE = "https://www.eis.gov.lv"
ALLOWED_HOSTS = {"eis.gov.lv", "www.eis.gov.lv"}
PROC_RE = re.compile(r"^/EKEIS/Supplier/Procurement/(\d+)/?$", re.IGNORECASE)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
SECTIONS = (("actual", "ActualDocuments_items"), ("archive", "HistoricalDocuments_items"))
TOKEN_RE = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')

# Pause between requests to the portal, scaled by what the previous request took.
PAUSE_MAX = float(os.environ.get("EIS_PAUSE", "5"))
PAUSE_FLOOR = float(os.environ.get("EIS_PAUSE_FLOOR", "0.75"))
PAUSE_PER_MB = float(os.environ.get("EIS_PAUSE_PER_MB", "0.5"))


def pause_for(bytes_taken):
    """Seconds to wait after taking `bytes_taken`, between the floor and the maximum."""
    if PAUSE_MAX <= 0:
        return 0.0
    earned = PAUSE_FLOOR + (max(0, bytes_taken) / 1048576.0) * PAUSE_PER_MB
    return max(PAUSE_FLOOR, min(PAUSE_MAX, earned))


class Fail(Exception):
    """The portal did not give what was asked for."""


class Withdrawn(Fail):
    """EIS answers that the procurement has nothing a guest may see."""


class Offline(Exception):
    """The portal could not be reached at all: no network, a laptop waking up, a dropped line.
    Not a Fail, so nothing that falls back on a refusal falls back on a lost network."""


# curl exit codes for a portal that was never reached or a line that broke mid-answer:
# host not resolved, no connection, timed out, TLS handshake, empty reply, send, receive.
OFFLINE_CODES = {6, 7, 28, 35, 52, 55, 56}


def canonical_url(raw):
    """(page URL, procurement id) for an EIS procurement URL or a bare id."""
    raw = (raw or "").strip()
    if raw.isdigit():
        return "%s/EKEIS/Supplier/Procurement/%s" % (BASE, raw), raw
    p = urlparse(raw)
    if p.scheme.lower() != "https" or (p.hostname or "").lower() not in ALLOWED_HOSTS:
        raise Fail("only https://www.eis.gov.lv procurement URLs are accepted, got %r" % raw)
    m = PROC_RE.match(p.path)
    if not m:
        raise Fail("expected /EKEIS/Supplier/Procurement/<numeric-id>, got %r" % p.path)
    return "%s/EKEIS/Supplier/Procurement/%s" % (BASE, m.group(1)), m.group(1)


class Curl:
    """System curl with one cookie jar per tender."""

    def __init__(self, workdir, referer):
        os.makedirs(workdir, exist_ok=True)
        self.jar = os.path.join(workdir, "cookies.txt")
        self.referer = referer

    def _run(self, args, dest=None, timeout=1800):
        cmd = ["curl", "-sS", "--fail-with-body", "--location", "--compressed",
               "--connect-timeout", "45", "--max-time", str(timeout),
               "--retry", "5", "--retry-delay", "3", "--retry-all-errors",
               "--retry-max-time", "300",
               "--cookie", self.jar, "--cookie-jar", self.jar,
               "-A", UA, "-H", "Accept-Language: lv,en;q=0.8",
               "-H", "Referer: " + self.referer, "-D", "-"] + args
        if dest:
            cmd += ["-o", dest]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 120,
                              creationflags=NO_WINDOW)
        if proc.returncode != 0:
            error = Offline if proc.returncode in OFFLINE_CODES else Fail
            raise error("curl failed (%d): %s" % (proc.returncode,
                                                  proc.stderr.decode("utf-8", "replace")[:300]))
        return proc.stdout

    @staticmethod
    def _split_headers(blob):
        text = blob.decode("utf-8", "replace")
        head, _, body = text.partition("\r\n\r\n")
        while body.startswith("HTTP/"):
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
        return self._split_headers(self._run([url], dest=dest))[0]

    def post_json(self, url, payload, token):
        args = ["-X", "POST", url,
                "-H", "Content-Type: application/json; charset=utf-8",
                "-H", "Accept: text/html, */*; q=0.01",
                "-H", "Origin: " + BASE,
                "-H", "X-Requested-With: XMLHttpRequest",
                "-H", "__RequestVerificationToken: " + token,
                "--data-binary", json.dumps(payload)]
        return self._split_headers(self._run(args))[1]


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
        "downloadable": title.get("ShowDownloadIcon") is not False,
    }


def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def safe_member(name):
    """False for an archive member that would escape the folder it is unpacked into."""
    if not name or name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
        return False
    return not os.path.isabs(name) and ":" not in name.split("/")[0][1:2]


class Tender:
    """One procurement page, read once, with the calls that need its token."""

    def __init__(self, url, workdir, register_uuid=None, attempts=6, wait=20):
        self.url, self.pid = canonical_url(url)
        self.curl = Curl(workdir, self.url)
        self.workdir = workdir
        self.html = self._read_page(attempts, wait)
        found = TOKEN_RE.search(self.html)
        if not found:
            raise Fail("no __RequestVerificationToken on %s" % self.url)
        self.token = found.group(1)
        self.facts = eis_page.parse_notice(self.html, self.pid, register_uuid=register_uuid) or {}

    def _read_page(self, attempts, wait):
        last = None
        for attempt in range(attempts):
            html = ""
            try:
                html = self.curl.get_text(self.url)
            except Fail as exc:
                last = str(exc)
            if eis_page.is_published(html):
                return html
            if html and eis_page.is_access_denied(html):
                raise Withdrawn("%s: EIS shows nothing a guest may see" % self.url)
            if html:
                last = "%d bytes, not a procurement page" % len(html)
            if attempt < attempts - 1:
                time.sleep(wait * (attempt + 1))
        raise Fail("%s: no procurement page after %d attempts (%s)" % (self.url, attempts, last))

    def records(self):
        """Every document record of the page, actual section first."""
        out = []
        for section, var in SECTIONS:
            try:
                rows = eis_page.embedded_array(self.html, var, strict=True)
            except eis_page.MalformedArray as exc:
                raise Fail(str(exc))
            out += [describe(row, section) for row in rows]
        return out

    def list_files(self, record):
        """[{file_id, name, type_code}] of one record."""
        payload = {"Id": record["id"], "FileId": None,
                   "DocumentLinkTypeCode": record["document_link_type_code"],
                   "ParentObjectTypeCode": "", "ParentId": None, "ParentIdentifier": None,
                   "ProcurementIdentifier": self.pid, "StageIdentifier": None}
        body = self.curl.post_json(BASE + "/EKEIS/Document/ViewDocument", payload, self.token)
        if body.lstrip().startswith("{"):
            raise Fail("ViewDocument refused record %s: %s" % (record["id"], body[:200]))
        try:
            files = eis_page.embedded_array(body, "ViewDocumentModel_Files_items", strict=True)
        except eis_page.MalformedArray as exc:
            raise Fail(str(exc))
        return [{"file_id": f.get("Id"), "name": (f.get("Title") or "").strip(),
                 "type_code": f.get("TypeCode")} for f in files]

    def download_file(self, record, file_id, dest):
        """Download one file to `dest`. Returns the Content-Length the portal announced."""
        url = "%s/EKEIS/Document/DownloadDocumentFile?%s" % (BASE, urlencode({
            "Id": record["id"], "FileId": file_id,
            "DocumentLinkTypeCode": record["document_link_type_code"],
            "ProcurementIdentifier": self.pid}))
        headers = self.curl.get_file(url, dest)
        if "text/html" in (headers.get("content-type") or "").lower():
            raise Fail("EIS returned a page instead of file %s of record %s"
                       % (file_id, record["id"]))
        if not os.path.exists(dest) or os.path.getsize(dest) == 0:
            raise Fail("file %s of record %s came back empty" % (file_id, record["id"]))
        announced = headers.get("content-length")
        return int(announced) if announced and announced.isdigit() else None

    def download_record_zip(self, record, dest):
        """The whole record as one ZIP, for a record whose files cannot be listed."""
        url = "%s/EKEIS/Document/DownloadDocumentFilesInZip?%s" % (BASE, urlencode({
            "Id": record["id"], "DocumentLinkTypeCode": record["document_link_type_code"],
            "ProcurementIdentifier": self.pid}))
        headers = self.curl.get_file(url, dest)
        if "text/html" in (headers.get("content-type") or "").lower():
            raise Fail("EIS returned a page instead of the archive of record %s" % record["id"])
        try:
            with zipfile.ZipFile(dest) as z:
                if z.testzip() is not None or not z.namelist():
                    raise Fail("the archive of record %s is damaged or empty" % record["id"])
        except zipfile.BadZipFile:
            raise Fail("the archive of record %s is not a ZIP" % record["id"])
