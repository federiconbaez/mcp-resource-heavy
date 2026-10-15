import base64
import http.client
import json
import os
import subprocess
import sys
import unittest
import urllib.parse

from tests import helpers
from tests.helpers import ROOT, TempDirTestCase, init_msg, make_server, rpc, start_http


class Http:
    registry = []

    def __init__(self, port, tok=None):
        self.c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        Http.registry.append(self.c)
        self.tok = tok
        self.sid = None

    def req(self, method, path, body=None, headers=None, session=True):
        h = {}
        if self.tok:
            h["Authorization"] = "Bearer " + self.tok
        if self.sid and session:
            h["Mcp-Session-Id"] = self.sid
        h.update(headers or {})
        if body is not None and not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.c.request(method, path, body=body, headers=h)
        r = self.c.getresponse()
        data = r.read().decode()
        self.headers = {k.lower(): v for k, v in r.getheaders()}
        return r.status, data

    def rpc(self, obj, headers=None):
        st, data = self.req("POST", "/mcp", obj, dict({"Content-Type": "application/json"}, **(headers or {})))
        if "mcp-session-id" in self.headers and not self.sid:
            self.sid = self.headers["mcp-session-id"]
        return st, (json.loads(data) if data else None)


class Base(TempDirTestCase):
    def tearDown(self):
        for conn in Http.registry:
            conn.close()
        Http.registry.clear()


class HttpE2E(Base):
    def setUp(self):
        super().setUp()
        from mcpres.auth import Authenticator
        self.server = helpers.McpServer(self.data_dir, Authenticator(helpers.SECRET, None), audit_fsync=False)
        self.t = start_http(self.server)
        self.addCleanup(self.t.shutdown)
        self.issuer = self.t.auth.issuer  # derived from the bound ephemeral port
        self.assertEqual(self.issuer, "http://127.0.0.1:%d" % self.t.port)

    def tok(self, scopes="mcp memory:read memory:write audit:read", **kw):
        a = self.t.auth
        return a.mint(scopes=scopes.split(), **kw)

    def client(self, tok=None, init=True):
        c = Http(self.t.port, tok or self.tok())
        if init:
            st, out = c.rpc(init_msg())
            assert st == 200, (st, out)
            c.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return c

    def test_happy_path_with_session_header(self):
        c = Http(self.t.port, self.tok())
        st, out = c.rpc(init_msg())
        self.assertEqual(st, 200)
        self.assertEqual(c.headers["content-type"], "application/json")
        self.assertRegex(c.headers["mcp-session-id"], r"^[0-9a-f]{32}$")
        self.assertEqual(out["result"]["protocolVersion"], "2025-03-26")
        st, out = c.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((st, out), (202, None))
        st, out = c.rpc(rpc("tools/call", {"name": "memory_store", "arguments": {"content": "over http"}}, 2))
        self.assertEqual(st, 200)
        self.assertFalse(out["result"]["isError"])
        st, out = c.rpc([rpc("ping", mid=3), rpc("tools/list", mid=4)])
        self.assertEqual((st, len(out)), (200, 2))

    def test_get_is_405_and_unknown_paths_404(self):
        c = Http(self.t.port, self.tok())
        st, _ = c.req("GET", "/mcp", headers={"Accept": "text/event-stream"})
        self.assertEqual(st, 405)
        self.assertEqual(c.headers["allow"], "POST, DELETE")
        self.assertEqual(c.req("GET", "/nope")[0], 404)
        self.assertEqual(c.req("PUT", "/mcp")[0], 405)
        self.assertEqual(c.req("GET", "/healthz")[0], 200)

    def test_auth_required_and_www_authenticate(self):
        c = Http(self.t.port)
        st, out = c.rpc(init_msg())
        self.assertEqual(st, 401)
        self.assertIn("Bearer", c.headers["www-authenticate"])
        for bad in (self.tok(ttl=-5), self.tok(audience="other"), self.tok(issuer="http://evil"), self.tok()[:-4] + "AAAA", "junk"):
            c = Http(self.t.port, bad)
            st, out = c.rpc(init_msg())
            self.assertEqual(st, 401, bad)
            self.assertIn('error="invalid_token"', c.headers["www-authenticate"])
        c = Http(self.t.port, self.tok("memory:read"))
        st, _ = c.rpc(init_msg())
        self.assertEqual(st, 403)
        self.assertIn("insufficient_scope", c.headers["www-authenticate"])
        c = Http(self.t.port, "x")
        c.tok = None
        self.assertEqual(c.req("DELETE", "/mcp")[0], 401)

    def test_scope_enforced_per_tool_over_http(self):
        c = self.client(self.tok("mcp memory:read"))
        st, out = c.rpc(rpc("tools/call", {"name": "memory_store", "arguments": {"content": "x"}}, 2))
        self.assertEqual((st, out["error"]["code"]), (200, -32003))

    def test_session_rules(self):
        c = self.client()
        st, out = c.rpc(rpc("tools/list", mid=2))
        self.assertEqual(st, 200)
        st, out = c.rpc(rpc("tools/list", mid=3), headers={"Mcp-Session-Id": "0" * 32})
        self.assertEqual(st, 404)
        c.sid, saved = None, c.sid
        st, out = c.rpc(rpc("tools/list", mid=4))
        self.assertEqual((st, out["error"]["code"]), (400, -32600))
        c.sid = saved
        self.assertEqual(c.req("DELETE", "/mcp")[0], 200)
        self.assertEqual(c.rpc(rpc("ping", mid=5))[0], 404)
        self.assertEqual(c.req("DELETE", "/mcp")[0], 404)

    def test_session_not_shared_between_subjects(self):
        a = self.client(self.tok(subject="alice"))
        b = Http(self.t.port, self.tok(subject="bob"))
        b.sid = a.sid
        st, _ = b.rpc(rpc("ping", mid=9))
        self.assertEqual(st, 403)
        self.assertEqual(b.req("DELETE", "/mcp")[0], 403)
        self.assertEqual(a.rpc(rpc("ping", mid=9))[0], 200)

    def test_header_validation(self):
        c = Http(self.t.port, self.tok())
        self.assertEqual(c.req("POST", "/mcp", init_msg(), {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(c.req("POST", "/mcp", init_msg(), {"Content-Type": "application/json", "Accept": "text/html"})[0], 406)
        self.assertEqual(c.req("POST", "/mcp", init_msg(), {"Content-Type": "application/json", "MCP-Protocol-Version": "1999-01-01"})[0], 400)
        self.assertEqual(c.req("POST", "/mcp", init_msg(), {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json, text/event-stream"})[0], 200)
        self.assertEqual(c.req("POST", "/mcp", init_msg(), {"Content-Type": "application/json", "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(c.req("POST", "/mcp", init_msg(), {"Content-Type": "application/json", "Host": "evil.example"})[0], 403)

    def test_protocol_version_header_must_match_session(self):
        c = Http(self.t.port, self.tok())
        c.rpc(init_msg("2025-06-18"))
        st, _ = c.rpc(rpc("ping", mid=2), headers={"MCP-Protocol-Version": "2025-03-26"})
        self.assertEqual(st, 400)
        st, _ = c.rpc(rpc("ping", mid=2), headers={"MCP-Protocol-Version": "2025-06-18"})
        self.assertEqual(st, 200)

    def test_parse_error_is_400_with_jsonrpc_body(self):
        c = self.client()
        st, out = c.rpc("{broken")
        self.assertEqual((st, out["error"]["code"], out["id"]), (400, -32700, None))
        st, data = c.req("POST", "/mcp", b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"a":"\xff"}}', {"Content-Type": "application/json"})
        self.assertEqual((st, json.loads(data)["error"]["code"]), (400, -32700))

    def test_body_limits_and_keepalive_desync_protection(self):
        c = self.client()
        c.c.putrequest("POST", "/mcp")
        for k, v in {"Content-Type": "application/json", "Content-Length": str(10 ** 8), "Authorization": "Bearer " + c.tok}.items():
            c.c.putheader(k, v)
        c.c.endheaders()
        r = c.c.getresponse()
        r.read()
        self.assertEqual(r.status, 413)
        # early rejections on a reused keep-alive connection must not corrupt the next request
        c2 = Http(self.t.port, self.tok())
        self.assertEqual(c2.req("POST", "/mcp", init_msg(), {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(c2.req("POST", "/mcp", init_msg(), {"Content-Type": "application/json"})[0], 200)
        c3 = Http(self.t.port)
        self.assertEqual(c3.req("POST", "/mcp", init_msg(), {"Content-Type": "application/json"})[0], 401)
        c3.tok = self.tok()
        self.assertEqual(c3.req("POST", "/mcp", init_msg(), {"Content-Type": "application/json"})[0], 200)

    def test_oidc_discovery_and_token_endpoint(self):
        c = Http(self.t.port)
        st, data = c.req("GET", "/.well-known/openid-configuration")
        doc = json.loads(data)
        self.assertEqual((st, doc["issuer"], doc["token_endpoint"]), (200, self.issuer, self.issuer + "/oauth/token"))
        secret = self.t.issuer.client_secret("demo-client")
        form = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "mcp memory:read"})
        basic = "Basic " + base64.b64encode(("demo-client:" + secret).encode()).decode()
        st, data = c.req("POST", "/oauth/token", form, {"Content-Type": "application/x-www-form-urlencoded", "Authorization": basic})
        tok = json.loads(data)
        self.assertEqual(st, 200)
        self.assertEqual(c.headers["cache-control"], "no-store")
        self.assertEqual((tok["token_type"], tok["scope"]), ("Bearer", "mcp memory:read"))
        mcp = Http(self.t.port, tok["access_token"])
        self.assertEqual(mcp.rpc(init_msg())[0], 200)
        bad = "Basic " + base64.b64encode(b"demo-client:wrong").decode()
        st, data = c.req("POST", "/oauth/token", form, {"Content-Type": "application/x-www-form-urlencoded", "Authorization": bad})
        self.assertEqual((st, json.loads(data)["error"]), (401, "invalid_client"))
        st, data = c.req("POST", "/oauth/token", form, {"Content-Type": "application/json", "Authorization": basic})
        self.assertEqual(st, 400)

    def test_http_requests_are_audited_with_subject_and_chain_ok(self):
        c = self.client(self.tok(subject="auditee"))
        c.rpc(rpc("ping", mid=7))
        Http(self.t.port, "junk").rpc(init_msg())
        entries = self.server.audit.tail(50)
        self.assertTrue(any(e["sub"] == "auditee" and e["transport"] == "http" for e in entries))
        self.assertTrue(any(e["dir"] == "event" and "auth_failure" in e["payload"] for e in entries))
        self.assertTrue(self.server.audit.verify()["ok"])


class NoAuthHttp(Base):
    def test_no_auth_mode_needs_no_token_and_has_no_issuer(self):
        server = make_server(self.data_dir, auth=False)
        t = start_http(server)
        self.addCleanup(t.shutdown)
        c = Http(t.port)
        self.assertEqual(c.rpc(init_msg())[0], 200)
        self.assertEqual(c.req("GET", "/.well-known/openid-configuration")[0], 404)


class HttpSubprocess(Base):
    def test_cli_http_server_end_to_end(self):
        env = dict(os.environ, PYTHONPATH=ROOT, MCP_FORGE_SECRET=helpers.SECRET.decode())
        p = subprocess.Popen([sys.executable, "-m", "mcpres", "serve", "--transport", "http", "--port", "0", "--data-dir", self.data_dir],
                             stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, env=env, cwd=ROOT, text=True)
        self.addCleanup(p.stderr.close)
        self.addCleanup(p.wait)
        self.addCleanup(p.terminate)
        ready = json.loads(p.stderr.readline())
        self.assertEqual(ready["event"], "ready")
        mint = subprocess.run([sys.executable, "-m", "mcpres", "mint-token", "--issuer", ready["issuer"]], capture_output=True, text=True, env=env, cwd=ROOT)
        c = Http(ready["port"], mint.stdout.strip())
        st, out = c.rpc(init_msg())
        self.assertEqual(st, 200, out)
        st, out = c.rpc(rpc("tools/call", {"name": "memory_store", "arguments": {"content": "subprocess http"}}, 2))
        self.assertFalse(out["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
