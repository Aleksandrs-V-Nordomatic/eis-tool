import json
import time
import unittest

import gateway

META = {"authorization_endpoint": "https://gw/authorize", "token_endpoint": "https://gw/token",
        "registration_endpoint": "https://gw/register"}


class Memory:
    def __init__(self):
        self.data = {}

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        self.data[(service, name)] = value


class FakeServer:
    """Answers the OAuth and MCP calls the client makes, and records them."""

    def __init__(self):
        self.calls = []
        self.tool_answers = {}

    def __call__(self, url, data=None, headers=None, method=None, timeout=60):
        self.calls.append((url, data, headers or {}, method))
        if url.endswith("/.well-known/oauth-authorization-server"):
            return 200, {}, json.dumps(META).encode()
        if url == META["token_endpoint"]:
            return 200, {}, json.dumps({"access_token": "fresh", "refresh_token": "r2",
                                        "expires_in": 3600}).encode()
        if url.endswith("/mcp"):
            message = json.loads(data)
            if "id" not in message:
                return 202, {}, b""
            if message["method"] == "initialize":
                return 200, {"Mcp-Session-Id": "s1", "Content-Type": "application/json"}, \
                    json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}).encode()
            name = message["params"]["name"]
            answer = self.tool_answers[name]
            body = "event: message\ndata: %s\n\n" % json.dumps(
                {"jsonrpc": "2.0", "id": message["id"],
                 "result": {"content": [{"type": "text", "text": json.dumps(answer)}]}})
            return 200, {"Content-Type": "text/event-stream"}, body.encode()
        raise AssertionError(url)


class Calls(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.store = gateway.Store(Memory())
        self.store.put("client", {"client_id": "c1"})
        self.gw = gateway.Gateway("https://gw/mcp", store=self.store, opener=self.server)

    def test_without_a_sign_in_it_says_so(self):
        empty = gateway.Gateway("https://gw/mcp", store=gateway.Store(Memory()),
                                opener=self.server)
        with self.assertRaises(gateway.SignInNeeded):
            empty.tool("sharepoint_status", {})

    def test_an_expired_token_is_renewed_with_the_refresh_token(self):
        self.store.put("tokens", {"access_token": "old", "refresh_token": "r1",
                                  "expires_at": time.time() - 10})
        self.server.tool_answers["sharepoint_status"] = {"ok": True}
        self.assertEqual(self.gw.tool("sharepoint_status", {}), {"ok": True})
        token_call = next(c for c in self.server.calls if c[0] == META["token_endpoint"])
        self.assertEqual(token_call[1]["grant_type"], "refresh_token")
        self.assertEqual(self.store.get("tokens")["access_token"], "fresh")
        mcp = [c for c in self.server.calls if c[0].endswith("/mcp")]
        self.assertEqual(mcp[-1][2]["Authorization"], "Bearer fresh")
        self.assertEqual(mcp[-1][2]["Mcp-Session-Id"], "s1")

    def test_a_tool_error_is_raised_with_its_text(self):
        self.store.put("tokens", {"access_token": "a", "refresh_token": "r",
                                  "expires_at": time.time() + 600})

        def failing(url, data=None, headers=None, method=None, timeout=60):
            if url.endswith("/mcp") and "tools/call" in (data or b"").decode():
                message = json.loads(data)
                return 200, {"Content-Type": "application/json"}, json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"], "result": {
                        "isError": True, "content": [{"type": "text", "text": "refused"}]}}).encode()
            return self.server(url, data, headers, method, timeout)
        gw = gateway.Gateway("https://gw/mcp", store=self.store, opener=failing)
        with self.assertRaisesRegex(gateway.GatewayError, "refused"):
            gw.tool("write_file", {})

    def test_an_upload_counts_only_with_an_etag(self):
        import os
        import tempfile
        self.store.put("tokens", {"access_token": "a", "refresh_token": "r",
                                  "expires_at": time.time() + 600})
        self.server.tool_answers["create_upload_link"] = {"url": "https://gw/upload/t"}
        sent = {}

        def opener(url, data=None, headers=None, method=None, timeout=60):
            if url == "https://gw/upload/t":
                sent["body"] = data.read()
                return 200, {}, json.dumps({"ok": True, "eTag": "e1"}).encode()
            return self.server(url, data, headers, method, timeout)
        gw = gateway.Gateway("https://gw/mcp", store=self.store, opener=opener)
        path = os.path.join(tempfile.mkdtemp(), "f.txt")
        with open(path, "wb") as fh:
            fh.write(b"abc")
        self.assertEqual(gw.upload("x/work/f.txt", path), "e1")
        self.assertEqual(sent["body"], b"abc")


if __name__ == "__main__":
    unittest.main()
