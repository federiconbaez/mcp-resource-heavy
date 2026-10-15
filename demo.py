#!/usr/bin/env python3
"""End-to-end demo: mint token -> initialize -> tools/list -> store -> RESTART server -> recall ->
resources/read -> prompts/get -> auth failures -> audit verify (+ tamper) -> same over HTTP + mock OIDC.

Everything runs against real subprocesses of `python3 -m mcpres`. Exit code 0 only if every step behaves as expected.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import http.client

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
STEPS = []


def say(msg=""):
    print(msg, flush=True)


def banner(title):
    say("\n" + "=" * 78 + "\n== " + title + "\n" + "=" * 78)


def must(cond, what):
    STEPS.append((what, bool(cond)))
    say("   [%s] %s" % ("ok" if cond else "FAIL", what))
    if not cond:
        raise SystemExit("demo failed at: " + what)


def env(**extra):
    e = dict(os.environ, PYTHONPATH=ROOT, PYTHONUNBUFFERED="1")
    e.pop("MCP_TOKEN", None)
    e.update(extra)
    return e


def cli(*args):
    say("$ python3 -m mcpres " + " ".join(args))
    return subprocess.run([PY, "-m", "mcpres", *args], capture_output=True, text=True, cwd=ROOT, env=env())


class Stdio:
    def __init__(self, data_dir, issuer, token):
        self.proc = subprocess.Popen([PY, "-m", "mcpres", "serve", "--data-dir", data_dir, "--issuer", issuer],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     cwd=ROOT, env=env(MCP_TOKEN=token))
        self.n = 0
        say("   (server process started, pid %d, transport=stdio)" % self.proc.pid)

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj).encode() + b"\n")
        self.proc.stdin.flush()

    def call(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        say("-> " + json.dumps(msg)[:200])
        self.send(msg)
        out = json.loads(self.proc.stdout.readline())
        say("<- " + json.dumps(out)[:260] + (" ..." if len(json.dumps(out)) > 260 else ""))
        return out

    def notify(self, method):
        say("-> " + json.dumps({"jsonrpc": "2.0", "method": method}) + "   (notification, no reply expected)")
        self.send({"jsonrpc": "2.0", "method": method})

    def stop(self):
        self.proc.stdin.close()
        code = self.proc.wait(10)
        self.proc.stdout.close()
        say("   (server process pid %d exited with code %s)" % (self.proc.pid, code))
        return code

    def handshake(self):
        res = self.call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "demo-client", "version": "1.0"}})
        self.notify("notifications/initialized")
        return res


def text(res):
    return json.loads(res["result"]["content"][0]["text"])


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    work = tempfile.mkdtemp(prefix="mcpres-demo-")
    data = os.path.join(work, "data")
    port = free_port()
    issuer = "http://127.0.0.1:%d" % port
    say("mcp-resource-heavy demo  |  python %s  |  workdir %s" % (sys.version.split()[0], work))
    say("Simplified OIDC (HS256, mock issuer) - NOT a certified OIDC flow. See SECURITY.md.")
    try:
        banner("1. Mint a token with the CLI (simplified OIDC: iss/aud/exp/nbf/iat/scope, HS256)")
        r = cli("mint-token", "--data-dir", data, "--issuer", issuer, "--sub", "demo-user", "--show-claims")
        token = r.stdout.strip()
        say(r.stderr.rstrip())
        say("token (truncated): %s..." % token[:60])
        must(r.returncode == 0 and token.count(".") == 2, "token minted")

        banner("2. Server #1 (stdio): initialize -> tools/list -> store memories")
        s = Stdio(data, issuer, token)
        init = s.handshake()
        must(init["result"]["protocolVersion"] == "2025-03-26", "protocol version negotiated: 2025-03-26")
        caps = init["result"]["capabilities"]
        must({"tools", "resources", "prompts"} <= set(caps), "capabilities advertise tools, resources, prompts")
        tools = s.call("tools/list")["result"]["tools"]
        say("   tools: " + ", ".join(t["name"] for t in tools))
        must(len(tools) == 4, "4 memory tools listed with strict JSON schemas")
        a = text(s.call("tools/call", {"name": "memory_store", "arguments": {
            "content": "Federico prefiere caf\u00e9 sin az\u00facar por la ma\u00f1ana", "tags": ["preferencias", "cafe"], "importance": 4}}))
        b = text(s.call("tools/call", {"name": "memory_store", "arguments": {
            "content": "The MCP demo must survive a server restart", "tags": ["demo"]}}))
        must(a["stored"]["id"] == 1 and b["stored"]["id"] == 2, "two memories stored (ids 1, 2) in SQLite")
        bad = s.call("tools/call", {"name": "memory_store", "arguments": {"content": "x", "importance": 9, "bogus": True}})
        must(bad["result"]["isError"] is True, "schema violation -> isError result (not a protocol error)")
        must(s.call("tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602, "unknown tool -> JSON-RPC error -32602")

        banner("3. RESTART: stop server #1, start server #2 on the same data dir")
        must(s.stop() == 0, "server #1 stopped cleanly")
        s = Stdio(data, issuer, token)
        s.handshake()
        found = text(s.call("tools/call", {"name": "memory_recall", "arguments": {"query": "CAFE manana"}}))
        must(found["count"] == 1 and "caf\u00e9" in found["results"][0]["content"], "memory recalled after restart (case/accent-insensitive search)")

        banner("4. resources/read and prompts/get")
        idx = json.loads(s.call("resources/read", {"uri": "memory://index"})["result"]["contents"][0]["text"])
        must(idx["total"] == 2, "memory://index reports 2 entries")
        ent = json.loads(s.call("resources/read", {"uri": "memory://entry/2"})["result"]["contents"][0]["text"])
        must(ent["content"].startswith("The MCP demo"), "memory://entry/{id} template resolves")
        must(s.call("resources/read", {"uri": "memory://entry/999"})["error"]["code"] == -32002, "missing resource -> -32002")
        pr = s.call("prompts/get", {"name": "recall_and_summarize", "arguments": {"query": "restart", "style": "terse"}})
        must("survive a server restart" in pr["result"]["messages"][0]["content"]["text"], "prompts/get embeds recalled memory")
        must(s.call("prompts/get", {"name": "recall_and_summarize", "arguments": {}})["error"]["code"] == -32602, "missing prompt arg -> -32602")
        chain = json.loads(s.call("resources/read", {"uri": "audit://chain"})["result"]["contents"][0]["text"])
        must(chain["chain"]["ok"] and chain["contracts"]["violated"] == 0,
             "audit://chain: hash chain valid (%d entries), %d runtime contract checks, 0 violations" % (chain["chain"]["count"], chain["contracts"]["checked"]))
        s.stop()

        banner("5. Auth failures and scopes (stdio)")
        for label, args in (("expired token", ["--ttl=-60"]), ("wrong audience", ["--aud", "other-service"]),
                            ("missing base scope", ["--scope", "memory:read"])):
            t = cli("mint-token", "--data-dir", data, "--issuer", issuer, *args).stdout.strip()
            bad_s = Stdio(data, issuer, t)
            res = bad_s.call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "d", "version": "1"}})
            must("error" in res, "%s rejected at initialize (code %s)" % (label, res["error"]["code"]))
            bad_s.stop()
        ro = cli("mint-token", "--data-dir", data, "--issuer", issuer, "--scope", "mcp memory:read").stdout.strip()
        s = Stdio(data, issuer, ro)
        s.handshake()
        res = s.call("tools/call", {"name": "memory_store", "arguments": {"content": "should be refused"}})
        must(res["error"]["code"] == -32003, "read-only token cannot memory_store (-32003 insufficient scope)")
        s.stop()

        banner("6. Tamper-evident audit log")
        r = cli("audit-verify", "--data-dir", data)
        must(r.returncode == 0, r.stderr.strip())
        log = os.path.join(data, "audit.log")
        copy = os.path.join(work, "audit.tampered")
        with open(log, "rb") as fh:
            raw = fh.read()
        with open(copy, "wb") as fh:
            fh.write(raw.replace(b"prefiere", b"PREFIERE", 1))
        r = cli("audit-verify", "--data-dir", data, "--file", copy)
        must(r.returncode == 1, "tampered copy detected: " + r.stderr.strip())

        banner("7. Same server over Streamable HTTP + mock OIDC issuer (client_credentials)")
        p = subprocess.Popen([PY, "-m", "mcpres", "serve", "--transport", "http", "--port", str(port), "--issuer", issuer, "--data-dir", data],
                             stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, cwd=ROOT, env=env(), text=True)
        try:
            say("   " + p.stderr.readline().strip())
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

            def req(method, path, body=None, headers=None):
                c.request(method, path, body=body, headers=headers or {})
                r = c.getresponse()
                return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read().decode()

            st, _, body = req("GET", "/.well-known/openid-configuration")
            doc = json.loads(body)
            say("GET /.well-known/openid-configuration -> %d  token_endpoint=%s" % (st, doc["token_endpoint"]))
            must(st == 200 and doc["issuer"] == issuer, "discovery document served")
            secret = cli("client-secret", "--data-dir", data, "demo-client").stdout.strip()
            form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": "demo-client", "client_secret": secret, "scope": "mcp memory:read memory:write"})
            st, _, body = req("POST", "/oauth/token", form, {"Content-Type": "application/x-www-form-urlencoded"})
            tok = json.loads(body)
            say("POST /oauth/token -> %d  token_type=%s expires_in=%s scope=%r" % (st, tok["token_type"], tok["expires_in"], tok["scope"]))
            must(st == 200, "access token issued by mock issuer")
            H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            st, _, body = req("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), H)
            must(st == 401, "POST /mcp without bearer token -> 401")
            H["Authorization"] = "Bearer " + tok["access_token"]
            st, hdr, body = req("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "http-demo", "version": "1"}}}), H)
            sid = hdr["mcp-session-id"]
            say("POST initialize -> %d, Mcp-Session-Id: %s, protocolVersion=%s" % (st, sid, json.loads(body)["result"]["protocolVersion"]))
            must(st == 200 and sid, "initialize over HTTP returns a session id")
            H["Mcp-Session-Id"] = sid
            st, _, body = req("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), H)
            must(st == 202 and body == "", "notification -> 202 Accepted, empty body")
            st, _, body = req("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_recall", "arguments": {"query": "restart"}}}), H)
            res = json.loads(body)
            say("tools/call memory_recall over HTTP -> %d, structuredContent.count=%s" % (st, res["result"]["structuredContent"]["count"]))
            must(res["result"]["structuredContent"]["count"] == 1, "HTTP sees the memory stored over stdio (shared SQLite)")
            st, hdr, _ = req("GET", "/mcp", headers={"Authorization": H["Authorization"], "Accept": "text/event-stream"})
            must(st == 405, "GET /mcp -> 405 (no SSE stream offered)")
            st, _, _ = req("DELETE", "/mcp", headers={"Authorization": H["Authorization"], "Mcp-Session-Id": sid})
            must(st == 200, "DELETE /mcp terminates the session")
        finally:
            p.terminate()
            p.wait(5)
            p.stderr.close()

        banner("8. Final audit chain check across both transports")
        r = cli("audit-verify", "--data-dir", data)
        must(r.returncode == 0, r.stderr.strip())
        banner("DEMO OK - %d/%d steps passed" % (sum(1 for _, ok in STEPS if ok), len(STEPS)))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
