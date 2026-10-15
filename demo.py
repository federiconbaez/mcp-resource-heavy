#!/usr/bin/env python3
"""mcp-resource-heavy end-to-end demo: pagination, static/templated/dynamic/binary resources, completion, rich prompts, subscriptions (stdio + HTTP SSE).

Everything runs against real subprocesses of `python3 -m mcpres` (no mocks). Exit code 0 only if every step behaves as expected.
Uses only the standard library; no network beyond 127.0.0.1.
"""

import base64
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
PKG = "mcpres"
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


def cli(*args, **envx):
    say("$ python3 -m %s %s" % (PKG, " ".join(args)))
    return subprocess.run([PY, "-m", PKG, *args], capture_output=True, text=True, cwd=ROOT, env=env(**envx))


def short(obj, n=240):
    t = json.dumps(obj) if not isinstance(obj, str) else obj
    return t[:n] + (" ..." if len(t) > n else "")


class Stdio:
    """One `serve` subprocess speaking newline-delimited JSON-RPC; server notifications are collected in ``self.notes``."""

    def __init__(self, data_dir, issuer, token, extra=(), **envx):
        e = env(**envx)
        if token:
            e["MCP_TOKEN"] = token
        cmd = [PY, "-m", PKG, "serve", "--data-dir", data_dir, *(["--issuer", issuer] if issuer else []), *extra]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=ROOT, env=e)
        self.n = 0
        self.notes = []
        say("   (server process started, pid %d, transport=stdio)" % self.proc.pid)

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj).encode() + b"\n")
        self.proc.stdin.flush()

    def read(self):
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def call(self, method, params=None, quiet=False):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        if not quiet:
            say("-> " + short(msg, 200))
        self.send(msg)
        while True:
            out = self.read()
            if out is None:
                return None
            if "method" in out and "id" not in out:
                self.notes.append(out)
                say("<= (notification) " + short(out))
                continue
            if not quiet:
                say("<- " + short(out, 260))
            return out

    def next_note(self):
        """Read the next server notification line (blocks); used right after a response that must be followed by one."""
        out = self.read()
        say("<= (notification) " + short(out))
        return out

    def notify(self, method):
        say("-> " + json.dumps({"jsonrpc": "2.0", "method": method}) + "   (notification, no reply expected)")
        self.send({"jsonrpc": "2.0", "method": method})

    def tool(self, name, args, quiet=False):
        out = self.call("tools/call", {"name": name, "arguments": args}, quiet)
        return out

    def stop(self, kill=False):
        if kill:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(10)
            self.proc.stdin.close()
            self.proc.stdout.close()
            say("   (server process pid %d killed with SIGKILL)" % self.proc.pid)
            return -9
        self.proc.stdin.close()
        code = self.proc.wait(10)
        self.proc.stdout.close()
        say("   (server process pid %d exited with code %s)" % (self.proc.pid, code))
        return code

    def handshake(self, version="2025-03-26"):
        res = self.call("initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "demo-client", "version": "1.0"}})
        if "result" in res:
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


class Http:
    def __init__(self, port, token=None):
        self.port, self.token, self.sid = port, token, None
        self.c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)

    def req(self, method, path, body=None, headers=None, session=True):
        h = {}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.sid and session:
            h["Mcp-Session-Id"] = self.sid
        h.update(headers or {})
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body)
        self.c.request(method, path, body=body, headers=h)
        r = self.c.getresponse()
        data = r.read().decode()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    def rpc(self, method, params=None, mid=1, quiet=False):
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        if not quiet:
            say("POST /mcp -> " + short(msg, 200))
        st, hdr, data = self.req("POST", "/mcp", msg, {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        if "mcp-session-id" in hdr and not self.sid:
            self.sid = hdr["mcp-session-id"]
        out = json.loads(data) if data else None
        if not quiet:
            say("<- HTTP %d %s" % (st, short(out, 240)))
        return st, out

    def notify(self, method):
        st, _, _ = self.req("POST", "/mcp", {"jsonrpc": "2.0", "method": method}, {"Content-Type": "application/json"})
        return st


def start_http(data, issuer, port, extra=(), **envx):
    p = subprocess.Popen([PY, "-m", PKG, "serve", "--transport", "http", "--port", str(port), *(["--issuer", issuer] if issuer else []),
                          "--data-dir", data, *extra], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, cwd=ROOT, env=env(**envx), text=True)
    say("   " + p.stderr.readline().strip())
    return p


def stop_http(p):
    p.terminate()
    p.wait(5)
    p.stderr.close()


def init_params(version="2025-03-26"):
    return {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "demo", "version": "1"}}


def walk(call, method, key):
    items, cursor, pages = [], None, 0
    while True:
        out = call(method, {"cursor": cursor} if cursor else {}, quiet=True)
        items += out["result"][key]
        pages += 1
        cursor = out["result"].get("nextCursor")
        if not cursor:
            return items, pages


def main():
    work = tempfile.mkdtemp(prefix="mcpres-demo-")
    data = os.path.join(work, "data")
    port = free_port()
    issuer = "http://127.0.0.1:%d" % port
    say("mcp-resource-heavy demo  |  python %s  |  workdir %s" % (sys.version.split()[0], work))
    try:
        banner("1. Token (HS256 simplified OIDC) and a stdio session")
        r = cli("mint-token", "--data-dir", data, "--issuer", issuer, "--sub", "demo-user")
        token = r.stdout.strip()
        must(r.returncode == 0 and token.count(".") == 2, "token minted with all scopes (mcp data:read data:write memory:* audit:read)")
        s = Stdio(data, issuer, token)
        init = s.handshake("2025-06-18")
        caps = init["result"]["capabilities"]
        must(caps["resources"] == {"subscribe": True, "listChanged": True} and caps["completions"] == {}, "capabilities: resources.subscribe/listChanged and completions advertised")

        banner("2. Cursor pagination over ~145 resources")
        items, pages = walk(s.call, "resources/list", "resources")
        uris = [i["uri"] for i in items]
        say("   %d resources in %d pages of <= 25; schemes: %s" % (len(uris), pages, ", ".join(sorted({u.split('://')[0] for u in uris}))))
        must(len(uris) == len(set(uris)) and uris == sorted(uris) and pages >= 5, "every page walked, no duplicates, stable order")
        must(s.call("resources/list", {"cursor": "not-a-cursor"})["error"]["code"] == -32602, "invalid cursor -> -32602")

        banner("3. Static, templated, dynamic and binary resources")
        about = s.call("resources/read", {"uri": "static://about"})["result"]["contents"][0]
        must(about["mimeType"] == "text/markdown", "static://about (text/markdown)")
        ar = json.loads(s.call("resources/read", {"uri": "country://AR"})["result"]["contents"][0]["text"])
        must(ar["capital"] == "Buenos Aires", "templated country://{iso2} -> %s, capital %s (approximate dataset)" % (ar["name"], ar["capital"]))
        fe = json.loads(s.call("resources/read", {"uri": "element://Fe"})["result"]["contents"][0]["text"])
        must(fe["number"] == 26, "templated element://{symbol} -> Z=%d" % fe["number"])
        must(s.call("resources/read", {"uri": "country://ZZ"})["error"]["code"] == -32002, "unknown templated resource -> -32002")
        v0 = json.loads(s.call("resources/read", {"uri": "dyn://counter"})["result"]["contents"][0]["text"])["value"]
        bump = s.tool("counter_bump", {"by": 5})
        v1 = json.loads(s.call("resources/read", {"uri": "dyn://counter"})["result"]["contents"][0]["text"])["value"]
        must(v1 == v0 + 5, "dynamic dyn://counter changed %d -> %d after counter_bump" % (v0, v1))
        c1 = json.loads(s.call("resources/read", {"uri": "dyn://clock"}, )["result"]["contents"][0]["text"])["utc"]
        time.sleep(0.02)
        c2 = json.loads(s.call("resources/read", {"uri": "dyn://clock"})["result"]["contents"][0]["text"])["utc"]
        must(c1 < c2, "dynamic dyn://clock differs between reads (%s < %s)" % (c1, c2))
        blob = s.call("resources/read", {"uri": "blob://sample.png"})["result"]["contents"][0]
        raw = base64.b64decode(blob["blob"], validate=True)
        must(blob["mimeType"] == "image/png" and raw[:8] == b"\x89PNG\r\n\x1a\n", "binary blob://sample.png: %d bytes, PNG signature" % len(raw))
        digest = text(s.tool("resource_digest", {"uri": "blob://sample.png"}, quiet=True))
        must(digest["sha256"] == hashlib.sha256(raw).hexdigest(), "server-side SHA-256 equals independent SHA-256 of the decoded blob")

        banner("4. completion/complete and a multi-message prompt with embedded resources")
        comp = s.call("completion/complete", {"ref": {"type": "ref/prompt", "name": "country_briefing"}, "argument": {"name": "country", "value": "ar"}})
        must(comp["result"]["completion"]["values"] == ["AR", "Argentina"], "completion for prompt argument 'country' = 'ar'")
        comp = s.call("completion/complete", {"ref": {"type": "ref/resource", "uri": "element://{symbol}"}, "argument": {"name": "symbol", "value": "C"}})
        must("Co" in comp["result"]["completion"]["values"], "completion for resource template variable")
        pr = s.call("prompts/get", {"name": "country_briefing", "arguments": {"country": "Argentina", "audience": "traveler"}}, )["result"]
        roles = [m["role"] for m in pr["messages"]]
        kinds = [m["content"]["type"] for m in pr["messages"]]
        must(roles == ["user", "user", "user", "assistant", "user"] and kinds.count("resource") == 2, "prompt has %d messages (%s), 2 embedded resources" % (len(roles), "/".join(roles)))
        must(s.call("prompts/get", {"name": "country_briefing", "arguments": {"country": "Atlantis"}})["error"]["code"] == -32602, "prompt argument validation -> -32602")

        banner("5. Subscriptions over stdio: notification follows the response that caused it")
        must(s.call("resources/subscribe", {"uri": "dyn://counter"})["result"] == {}, "subscribed to dyn://counter")
        out = s.tool("counter_bump", {})
        note = s.next_note()
        must(out["result"]["isError"] is False and note["method"] == "notifications/resources/updated" and note["params"]["uri"] == "dyn://counter", "counter_bump -> response, then notifications/resources/updated")
        out = s.tool("glossary_add", {"term": "demo term", "definition": "A term added during the demo."})
        note = s.next_note()
        must(note["method"] == "notifications/resources/list_changed", "glossary_add of a new term -> notifications/resources/list_changed")
        must(s.call("resources/read", {"uri": "glossary://term/demo%20term"})["result"]["contents"][0]["uri"] == "glossary://term/demo%20term", "the new term is a readable resource")
        s.call("resources/unsubscribe", {"uri": "dyn://counter"})
        s.stop()

        banner("6. RESTART: mutable resources persist (counter, user glossary); --page-size 5 changes paging")
        s = Stdio(data, issuer, token, extra=("--page-size", "5"))
        s.handshake()
        must(json.loads(s.call("resources/read", {"uri": "dyn://counter"})["result"]["contents"][0]["text"])["value"] == v1 + 1, "counter survived the restart (%d)" % (v1 + 1))
        items, pages = walk(s.call, "resources/list", "resources")
        must(pages > 25 and "glossary://term/demo%20term" in [i["uri"] for i in items], "page size 5 -> %d pages, user term included" % pages)
        s.stop()

        banner("7. HTTP: OIDC token, session, subscription and an SSE notification stream")
        p = start_http(data, issuer, port)
        try:
            c = Http(port)
            secret = cli("client-secret", "--data-dir", data, "demo-client").stdout.strip()
            form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": "demo-client", "client_secret": secret,
                                           "scope": "mcp data:read data:write memory:read memory:write"})
            st, _, body = c.req("POST", "/oauth/token", form, {"Content-Type": "application/x-www-form-urlencoded"})
            tok = json.loads(body)
            must(st == 200, "access token from the mock issuer (scope %r)" % tok["scope"])
            c.token = tok["access_token"]
            st, out = c.rpc("initialize", init_params("2025-06-18"))
            must(st == 200 and c.sid, "initialize -> session id")
            c.notify("notifications/initialized")
            st, out = c.rpc("resources/subscribe", {"uri": "dyn://counter"}, 2)
            must(out["result"] == {}, "subscribed over HTTP")
            events = []
            ready = threading.Event()

            def reader():
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                conn.request("GET", "/mcp", headers={"Authorization": "Bearer " + c.token, "Accept": "text/event-stream", "Mcp-Session-Id": c.sid})
                resp = conn.getresponse()
                ready.set()
                data_lines = []
                for raw_line in iter(resp.fp.readline, b""):
                    line = raw_line.decode().rstrip("\r\n")
                    if line.startswith("data: "):
                        data_lines.append(line[6:])
                    elif line == "" and data_lines:
                        events.append(json.loads("".join(data_lines)))
                        say("   SSE event: " + short(events[-1]))
                        data_lines = []

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            ready.wait(5)
            time.sleep(0.2)
            c.rpc("tools/call", {"name": "counter_bump", "arguments": {}}, 3)
            for _ in range(50):
                if events:
                    break
                time.sleep(0.1)
            must(events and events[0]["method"] == "notifications/resources/updated" and events[0]["params"]["uri"] == "dyn://counter", "GET /mcp SSE stream delivered notifications/resources/updated")
            st, _, _ = c.req("DELETE", "/mcp")
            must(st == 200, "DELETE /mcp ends the session")
            t.join(5)
            must(not t.is_alive(), "the SSE stream was closed by the server")
        finally:
            stop_http(p)

        banner("8. Audit chain across everything above")
        r = cli("audit-verify", "--data-dir", data)
        must(r.returncode == 0, r.stderr.strip())
        banner("DEMO OK - %d/%d steps passed" % (sum(1 for _, ok in STEPS if ok), len(STEPS)))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
