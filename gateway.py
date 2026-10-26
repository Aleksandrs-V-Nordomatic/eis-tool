"""Client for the SharePoint gateway: an MCP server behind OAuth 2.1.

The downloader reads the radar kit and uploads a batch through it, in the name of the person
who signed in. The first sign-in opens a browser; after that the refresh token, kept in the
Windows Credential Manager, renews access without a window.

    python gateway.py sign-in
    python gateway.py status
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

DEFAULT_URL = "https://sharepoint-automations.nordomatic.workers.dev/mcp"
CREDENTIAL_SERVICE = "eis-tool gateway"
CALLBACK_PORT = 47613
PROTOCOL_VERSION = "2025-06-18"


class GatewayError(Exception):
    """The gateway refused a call or could not be reached."""


class SignInNeeded(GatewayError):
    """No usable sign-in is stored; run `python gateway.py sign-in`."""


class Store:
    """Token storage. Windows Credential Manager through keyring; a dict in tests."""

    def __init__(self, backend=None):
        if backend is None:
            import keyring
            backend = keyring
        self.backend = backend

    def get(self, name):
        raw = self.backend.get_password(CREDENTIAL_SERVICE, name)
        return json.loads(raw) if raw else None

    def put(self, name, value):
        self.backend.set_password(CREDENTIAL_SERVICE, name, json.dumps(value))


def _request(url, data=None, headers=None, method=None, timeout=60):
    body = None
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
        headers = dict(headers or {}, **{"Content-Type": "application/x-www-form-urlencoded"})
    elif data is not None:
        body = data
    headers = dict(headers or {}, **{"User-Agent": "eis-tool/1"})
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class Gateway:
    """One signed-in connection to the gateway's MCP endpoint."""

    def __init__(self, url=None, store=None, opener=None):
        self.url = url or os.environ.get("GATEWAY_URL") or DEFAULT_URL
        parts = urllib.parse.urlsplit(self.url)
        self.origin = "%s://%s" % (parts.scheme, parts.netloc)
        self.store = store or Store()
        self.request = opener or _request
        self.session_id = None
        self.next_id = 1

    # ---------------------------------------------------------------- OAuth
    def _metadata(self):
        status, _, body = self.request(self.origin + "/.well-known/oauth-authorization-server")
        if status != 200:
            raise GatewayError("no OAuth metadata at the gateway (HTTP %s)" % status)
        return json.loads(body)

    def sign_in(self, open_browser=webbrowser.open):
        """Register this client, open the browser, take the code and store the tokens."""
        meta = self._metadata()
        redirect = "http://127.0.0.1:%d/callback" % CALLBACK_PORT
        status, _, body = self.request(
            meta["registration_endpoint"],
            data=json.dumps({"client_name": "eis-tool downloader",
                             "redirect_uris": [redirect],
                             "grant_types": ["authorization_code", "refresh_token"],
                             "response_types": ["code"],
                             "token_endpoint_auth_method": "none"}).encode(),
            headers={"Content-Type": "application/json"})
        if status not in (200, 201):
            raise GatewayError("client registration refused (HTTP %s)" % status)
        client_id = json.loads(body)["client_id"]

        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(16)
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state, "resource": self.url})
        code = _wait_for_code(meta["authorization_endpoint"] + "?" + query, state, open_browser)
        tokens = self._token({"grant_type": "authorization_code", "code": code,
                              "redirect_uri": redirect, "client_id": client_id,
                              "code_verifier": verifier, "resource": self.url}, meta)
        self.store.put("client", {"client_id": client_id})
        self._keep(tokens)

    def _token(self, form, meta=None):
        meta = meta or self._metadata()
        status, _, body = self.request(meta["token_endpoint"], data=form)
        if status != 200:
            raise SignInNeeded("the gateway refused the token request (HTTP %s)" % status)
        return json.loads(body)

    def _keep(self, tokens):
        self.store.put("tokens", {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": time.time() + int(tokens.get("expires_in") or 3600) - 60})

    def _access_token(self):
        tokens = self.store.get("tokens")
        client = self.store.get("client")
        if not tokens or not client:
            raise SignInNeeded("not signed in to the gateway")
        if tokens["expires_at"] > time.time():
            return tokens["access_token"]
        if not tokens.get("refresh_token"):
            raise SignInNeeded("the gateway sign-in expired")
        fresh = self._token({"grant_type": "refresh_token",
                             "refresh_token": tokens["refresh_token"],
                             "client_id": client["client_id"], "resource": self.url})
        fresh.setdefault("refresh_token", tokens["refresh_token"])
        self._keep(fresh)
        return fresh["access_token"]

    # ------------------------------------------------------------------ MCP
    def _post(self, message):
        headers = {"Authorization": "Bearer " + self._access_token(),
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        status, resp_headers, body = self.request(
            self.url, data=json.dumps(message).encode(), headers=headers, method="POST",
            timeout=180)
        if status == 401:
            raise SignInNeeded("the gateway rejected the access token")
        if status >= 400:
            raise GatewayError("gateway HTTP %s: %s" % (status, body[:200].decode("utf-8", "replace")))
        lowered = {k.lower(): v for k, v in resp_headers.items()}
        if lowered.get("mcp-session-id"):
            self.session_id = lowered["mcp-session-id"]
        if "id" not in message:
            return None
        return _rpc_result(body, lowered.get("content-type", ""), message["id"])

    def _call(self, method, params):
        if self.session_id is None and method != "initialize":
            self._open()
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        self.next_id += 1
        return self._post(message)

    def _open(self):
        self._call("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                  "clientInfo": {"name": "eis-tool", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool(self, name, arguments, tries=4):
        """Call one tool and return its JSON answer. Retries what a retry can fix."""
        for attempt in range(tries):
            try:
                result = self._call("tools/call", {"name": name, "arguments": arguments})
                break
            except SignInNeeded:
                raise
            except (GatewayError, OSError) as exc:
                if attempt == tries - 1:
                    raise GatewayError("%s failed: %s" % (name, exc))
                self.session_id = None
                time.sleep(3 * (attempt + 1))
        text = "".join(part.get("text", "") for part in result.get("content", [])
                       if part.get("type") == "text")
        if result.get("isError"):
            raise GatewayError("%s: %s" % (name, text[:300]))
        structured = result.get("structuredContent")
        if structured is not None:
            # The gateway sends every answer as {"result": <answer>}; the text part is the bare answer.
            if isinstance(structured, dict) and set(structured) == {"result"}:
                return structured["result"]
            return structured
        try:
            return json.loads(text)
        except ValueError:
            return text

    # -------------------------------------------------------- library calls
    def read_text(self, path, limit=4 * 1024 * 1024):
        """A whole UTF-8 file of the library, or None when it is not there."""
        try:
            info = self.tool("file_info", {"path": path})
        except GatewayError as exc:
            if "not found" in str(exc).lower() or "404" in str(exc):
                return None
            raise
        size = int(info.get("size") or 0)
        if size > limit:
            raise GatewayError("%s is %d bytes, more than %d" % (path, size, limit))
        link = self.tool("create_download_link", {"path": path})
        status, _, body = self.request(link["url"], timeout=300)
        if status != 200:
            raise GatewayError("download of %s failed (HTTP %s)" % (path, status))
        return body.decode("utf-8")

    def list_names(self, path):
        """Names of the items in one library folder; empty when it does not exist."""
        names, cursor = [], None
        while True:
            args = {"path": path}
            if cursor:
                args["cursor"] = cursor
            try:
                page = self.tool("list_directory", args)
            except GatewayError as exc:
                if "not found" in str(exc).lower() or "404" in str(exc):
                    return []
                raise
            names += [item.get("name") for item in page.get("items", [])]
            cursor = page.get("nextCursor") or page.get("cursor")
            if not cursor:
                return names

    def upload(self, path, local_file, replace=False):
        """Upload one file in one request; returns its eTag once the library confirms it."""
        size = os.path.getsize(local_file)
        args = {"path": path, "totalSize": size}
        if replace:
            try:
                info = self.tool("file_info", {"path": path})
                args.update(conflictBehavior="replace", ifMatch=info.get("eTag"))
            except GatewayError:
                pass
        link = self.tool("create_upload_link", args)
        with open(local_file, "rb") as fh:
            status, _, body = self.request(link["url"], data=fh, method="PUT",
                                           headers={"Content-Length": str(size)}, timeout=3600)
        if status not in (200, 201):
            raise GatewayError("upload of %s failed (HTTP %s): %s"
                               % (path, status, body[:200].decode("utf-8", "replace")))
        answer = json.loads(body)
        if not answer.get("eTag"):
            raise GatewayError("upload of %s came back without an eTag" % path)
        return answer["eTag"]


def _rpc_result(body, content_type, wanted):
    text = body.decode("utf-8", "replace")
    messages = []
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    messages.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
    elif text.strip():
        parsed = json.loads(text)
        messages = parsed if isinstance(parsed, list) else [parsed]
    for message in messages:
        if message.get("id") == wanted:
            if "error" in message:
                raise GatewayError(message["error"].get("message", "error"))
            return message.get("result") or {}
    raise GatewayError("no answer to request %s" % wanted)


def _wait_for_code(authorize_url, state, open_browser, timeout=300):
    """Open the browser and serve the one redirect that carries the code."""
    found = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if query.get("state", [None])[0] == state and query.get("code"):
                found["code"] = query["code"][0]
                message = "Signed in. This window can be closed."
            else:
                found.setdefault("error", query.get("error", ["no code"])[0])
                message = "Sign-in failed: %s" % found["error"]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message.encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)
    server.timeout = 1
    deadline = time.time() + timeout

    def serve():
        while not found and time.time() < deadline:
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    open_browser(authorize_url)
    thread.join(timeout + 5)
    server.server_close()
    if "code" not in found:
        raise SignInNeeded("sign-in did not finish: %s" % found.get("error", "timed out"))
    return found["code"]


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    gateway = Gateway()
    if args[:1] == ["sign-in"]:
        print("A browser window opens; sign in and approve the access.")
        gateway.sign_in()
        print("signed in")
    status = gateway.tool("sharepoint_status", {})
    print(json.dumps(status, ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
