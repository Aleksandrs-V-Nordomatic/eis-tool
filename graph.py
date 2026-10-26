"""Microsoft Graph access to one drive, as an app with client credentials.

Environment: GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_DRIVE_ID.
Paths are drive-relative and are never printed.
"""

import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import net

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com/%s/oauth2/v2.0/token"
RETRY = (409, 429, 500, 502, 503, 504)
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
UPLOAD_CHUNK = 32 * 320 * 1024          # a multiple of 320 KiB, as Graph requires
TOKEN_LIFETIME = 45 * 60
TENANT = re.compile(r"^[0-9a-fA-F-]{36}$")


class GraphError(Exception):
    pass


def env(name):
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise GraphError("missing environment: %s" % name)
    if name != "GRAPH_DEST_ROOT" and any(c.isspace() for c in value):
        raise GraphError("%s contains whitespace" % name)
    return value


def escaped(path):
    return "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))


class Drive:
    def __init__(self, drive=None, credentials=None):
        self.drive = drive or env("GRAPH_DRIVE_ID")
        self.credentials = credentials or (env("GRAPH_TENANT_ID"), env("GRAPH_CLIENT_ID"),
                                           env("GRAPH_CLIENT_SECRET"))
        if not TENANT.match(self.credentials[0]):
            raise GraphError("GRAPH_TENANT_ID is not a tenant id")
        self._token, self._since = None, 0

    def token(self):
        if self._token and time.time() - self._since < TOKEN_LIFETIME:
            return self._token
        tenant, client_id, client_secret = self.credentials
        body = urllib.parse.urlencode({
            "client_id": client_id, "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials"}).encode()
        payload, _ = net.open_url(urllib.request.Request(LOGIN % tenant, data=body),
                                  timeout=60, parse=json.loads)
        self._token, self._since = payload["access_token"], time.time()
        return self._token

    def _send(self, url, data=None, method="GET", headers=None, tries=5, timeout=300):
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, method=method)
            if urllib.parse.urlsplit(url).netloc == "graph.microsoft.com":
                req.add_header("Authorization", "Bearer " + self.token())
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return 404, None
                if exc.code in RETRY and attempt < tries - 1:
                    time.sleep(min(int(exc.headers.get("Retry-After") or 2 ** attempt), 60))
                    continue
                raise GraphError("%s failed: HTTP %d" % (method, exc.code))
            except net.TRANSPORT_ERRORS as exc:
                if attempt < tries - 1:
                    time.sleep(net.delay(attempt, exc))
                    continue
                raise GraphError("%s failed: %s" % (method, type(exc).__name__))
        raise GraphError("%s failed: retries exhausted" % method)

    def _item(self, path, suffix=""):
        return "%s/drives/%s/root:/%s%s" % (GRAPH, self.drive, escaped(path), suffix)

    def item(self, path):
        status, body = self._send(self._item(path))
        return json.loads(body) if status != 404 else None

    def read(self, path):
        """Bytes at `path`, or None when nothing is there."""
        status, body = self._send(self._item(path, ":/content"))
        return body if status != 404 else None

    def read_json(self, path):
        body = self.read(path)
        if body is None:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    def download(self, path, dest):
        """Write the file at `path` to `dest`, streaming. False when it is not there."""
        req = urllib.request.Request(self._item(path, ":/content"))
        req.add_header("Authorization", "Bearer " + self.token())
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=1800) as resp, open(dest, "wb") as fh:
                    while True:
                        block = resp.read(1024 * 1024)
                        if not block:
                            return True
                        fh.write(block)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return False
                if exc.code in RETRY and attempt < 4:
                    time.sleep(min(int(exc.headers.get("Retry-After") or 2 ** attempt), 60))
                    continue
                raise GraphError("download failed: HTTP %d" % exc.code)
            except net.TRANSPORT_ERRORS as exc:
                if attempt < 4:
                    time.sleep(net.delay(attempt, exc))
                    continue
                raise GraphError("download failed: %s" % type(exc).__name__)
        return False

    def upload(self, path, data):
        self.upload_stream(path, io.BytesIO(data), len(data))

    def upload_file(self, path, local):
        with open(local, "rb") as fh:
            self.upload_stream(path, fh, os.path.getsize(local))

    def upload_stream(self, path, stream, size):
        if size < SIMPLE_UPLOAD_LIMIT:
            self._send(self._item(path, ":/content"), data=stream.read(), method="PUT",
                       headers={"Content-Type": "application/octet-stream"})
            return
        status, body = self._send(
            self._item(path, ":/createUploadSession"), method="POST",
            data=json.dumps({"item": {"@microsoft.graph.conflictBehavior": "replace"}}).encode(),
            headers={"Content-Type": "application/json"})
        url = json.loads(body)["uploadUrl"]
        offset = 0
        while offset < size:
            data = stream.read(min(UPLOAD_CHUNK, size - offset))
            if not data:
                raise GraphError("the source ended at %d of %d bytes" % (offset, size))
            end = offset + len(data) - 1
            self._send(url, data=data, method="PUT", timeout=600,
                       headers={"Content-Range": "bytes %d-%d/%d" % (offset, end, size)})
            offset = end + 1

    def delete(self, path):
        """Remove a file or a folder. Absent is fine."""
        self._send(self._item(path), method="DELETE")
