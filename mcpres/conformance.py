"""Black-box protocol compliance suite.

Runs the server as a real subprocess (stdio) and as a real HTTP server, speaks raw
JSON-RPC to it from scratch, and prints a human report plus a JSON report. It shares no
code path with the server except ``fuzz.py`` (generators + independent oracle).

    python3 -m mcpres conformance [--transport stdio|http|both] [--json report.json]
"""

import http.client
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from . import __version__, fuzz
from .audit import verify_file

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS = ("2025-03-26", "2024-11-05", "2025-06-18")
CHECKS = []


class CheckFailed(AssertionError):
    pass


def expect(cond, msg="assertion failed"):
    if not cond:
        raise CheckFailed(msg)


def check(cid, title, spec, transports=("stdio", "http")):
    def deco(fn):
        CHECKS.append({"id": cid, "title": title, "spec": spec, "transports": transports, "fn": fn})
        return fn
    return deco


# ---------------------------------------------------------------------------------
# Environment + connections
# ---------------------------------------------------------------------------------
class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="mcpres-conf-")
        self.data_dir = os.path.join(self.tmp, "data")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.issuer = "http://127.0.0.1:%d" % self.port
        self.http_proc = None
        self._tokens = {}

    def pyenv(self, **extra):
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONUNBUFFERED="1")
        env.pop("MCP_TOKEN", None)
        env.update(extra)
        return env

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "mcpres", *args], capture_output=True, text=True,
                              env=self.pyenv(), cwd=ROOT, timeout=30)

    def mint(self, scope="mcp memory:read memory:write audit:read", ttl=3600, aud=None, issuer=None, sub="conformance"):
        key = (scope, ttl, aud, issuer, sub)
        if key not in self._tokens:
            args = ["mint-token", "--data-dir", self.data_dir, "--issuer", issuer or self.issuer, "--sub", sub,
                    "--scope", scope, "--ttl=%d" % ttl]
            if aud:
                args += ["--aud", aud]
            r = self.cli(*args)
            expect(r.returncode == 0, "mint-token failed: " + r.stderr)
            self._tokens[key] = r.stdout.strip()
        return self._tokens[key]

    def start_http(self):
        self.http_proc = subprocess.Popen(
            [sys.executable, "-m", "mcpres", "serve", "--transport", "http", "--port", str(self.port),
             "--issuer", self.issuer, "--data-dir", self.data_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=self.pyenv(), cwd=ROOT, text=True)
        line = self.http_proc.stderr.readline()
        expect('"ready"' in line, "http server did not start: " + line)

    def stop_http(self):
        if self.http_proc:
            self.http_proc.terminate()
            try:
                self.http_proc.wait(5)
            except subprocess.TimeoutExpired:
                self.http_proc.kill()

    def cleanup(self):
        self.stop_http()
        shutil.rmtree(self.tmp, ignore_errors=True)


class StdioConn:
    SENTINEL = "__conformance_sentinel__"

    def __init__(self, env, token=None, no_auth=False):
        cmd = [sys.executable, "-m", "mcpres", "serve", "--data-dir", env.data_dir, "--issuer", env.issuer]
        if no_auth:
            cmd.append("--no-auth")
        extra = {"MCP_TOKEN": token} if token else {}
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env.pyenv(**extra), cwd=ROOT)
        self.lines = queue.Queue()
        self.stderr = []
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines), daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()
        self.status = None
        self.headers = {}

    def _pump(self, stream, q):
        for line in iter(stream.readline, b""):
            q.put(line)
        q.put(None)

    def _pump_err(self):
        for line in iter(self.proc.stderr.readline, b""):
            self.stderr.append(line.decode("utf-8", "replace"))

    def _write(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def raw(self, data):
        """Send raw text/bytes as one frame; return response text, or None if the server stayed silent."""
        data = data.encode("utf-8") if isinstance(data, str) else data
        sentinel = json.dumps({"jsonrpc": "2.0", "id": self.SENTINEL, "method": "ping"}).encode()
        self._write(data + b"\n" + sentinel + b"\n")
        got = []
        while True:
            try:
                line = self.lines.get(timeout=15)
            except queue.Empty:
                raise CheckFailed("timeout waiting for server output")
            expect(line is not None, "server closed stdout; stderr=%r" % self.stderr[-3:])
            text = line.decode("utf-8").rstrip("\n")
            try:
                msg = json.loads(text)
            except ValueError:
                raise CheckFailed("non-JSON line on stdout: %r" % text[:100])
            if isinstance(msg, dict) and msg.get("id") == self.SENTINEL:
                break
            got.append(text)
        expect(len(got) <= 1, "server emitted %d outputs for one frame" % len(got))
        return got[0] if got else None

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return self.proc.returncode


class HttpConn:
    def __init__(self, env, token=None, session=None, headers=None):
        self.env = env
        self.token = token
        self.session = session
        self.extra = headers or {}
        self.http = http.client.HTTPConnection("127.0.0.1", env.port, timeout=15)
        self.status = None
        self.headers = {}
        self.body = ""

    def request(self, method, path, body=None, headers=None, use_session=True):
        h = {}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.session and use_session:
            h["Mcp-Session-Id"] = self.session
        h.update(self.extra)
        h.update(headers or {})
        self.http.request(method, path, body=body, headers=h)
        resp = self.http.getresponse()
        data = resp.read().decode("utf-8", "replace")
        self.status, self.headers, self.body = resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        return resp.status, data

    def raw(self, data):
        body = data.encode("utf-8") if isinstance(data, str) else data
        status, text = self.request("POST", "/mcp", body, {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        if "mcp-session-id" in self.headers and not self.session:
            self.session = self.headers["mcp-session-id"]
        if status == 202:
            expect(text == "", "202 must have empty body")
            return None
        return text

    def alive(self):
        return True

    def close(self):
        self.http.close()


class Client:
    def __init__(self, conn):
        self.conn = conn
        self._id = 0

    def send(self, obj):
        text = self.conn.raw(json.dumps(obj))
        return None if text is None else json.loads(text)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        out = self.send(msg)
        expect(out is None, "notification produced a response: %r" % (out,))

    def call(self, method, params=None, mid=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id if mid is None else mid, "method": method}
        if params is not None:
            msg["params"] = params
        out = self.send(msg)
        expect(isinstance(out, dict), "no response to request %s" % method)
        expect(out.get("id") == msg["id"], "id not echoed for %s" % method)
        return out

    def result(self, method, params=None):
        out = self.call(method, params)
        expect("result" in out, "%s failed: %r" % (method, out.get("error")))
        return out["result"]

    def error(self, method, params=None, code=None):
        out = self.call(method, params)
        expect("error" in out, "%s unexpectedly succeeded" % method)
        if code is not None:
            expect(out["error"]["code"] == code, "%s: expected %d got %r" % (method, code, out["error"]))
        return out["error"]

    def initialize(self, version="2025-03-26"):
        res = self.result("initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "conformance", "version": "1"}})
        self.notify("notifications/initialized")
        return res

    def tool(self, name, args):
        return self.result("tools/call", {"name": name, "arguments": args})

    def close(self):
        return self.conn.close()


class Ctx:
    def __init__(self, env, transport):
        self.env, self.transport = env, transport
        self.opened = []

    def conn(self, token="full", no_auth=False, **kw):
        if token == "full":
            token = self.env.mint()
        if self.transport == "stdio":
            c = StdioConn(self.env, token, no_auth)
        else:
            c = HttpConn(self.env, token, **kw)
        self.opened.append(c)
        return c

    def client(self, init=True, version="2025-03-26", token="full", no_auth=False):
        cl = Client(self.conn(token, no_auth))
        if init:
            cl.initialize(version)
        return cl

    def close_all(self):
        for c in self.opened:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def mk(method, mid=1, params=None):
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return m


# ---------------------------------------------------------------------------------
# JSON-RPC 2.0 checks
# ---------------------------------------------------------------------------------
@check("RPC-001", "Malformed JSON -> -32700 with id null", "JSON-RPC 2.0 s5.1")
def _(c):
    out = json.loads(c.client(init=False).conn.raw('{"jsonrpc":"2.0","method":'))
    expect(out["error"]["code"] == -32700 and out["id"] is None, out)


@check("RPC-002", "Wrong structure -> -32600 with id null", "JSON-RPC 2.0 s5.1")
def _(c):
    out = json.loads(c.client(init=False).conn.raw('{"jsonrpc":"2.0","method":1,"params":"bar"}'))
    expect(out["error"]["code"] == -32600 and out["id"] is None, out)


@check("RPC-003", "Missing/incorrect jsonrpc member -> -32600, valid id echoed", "JSON-RPC 2.0 s4")
def _(c):
    cl = c.client()
    for bad in ({"id": 5, "method": "ping"}, {"jsonrpc": "1.0", "id": 5, "method": "ping"}, {"jsonrpc": 2.0, "id": 5, "method": "ping"}):
        out = cl.send(bad)
        expect(out["error"]["code"] == -32600 and out["id"] == 5, out)


@check("RPC-004", "Empty batch -> single -32600 object", "JSON-RPC 2.0 s6")
def _(c):
    out = json.loads(c.client().conn.raw("[]"))
    expect(isinstance(out, dict) and out["error"]["code"] == -32600, out)


@check("RPC-005", "Batch of non-requests -> array of -32600, one per element", "JSON-RPC 2.0 s6")
def _(c):
    out = json.loads(c.client().conn.raw("[1,2,3]"))
    expect(isinstance(out, list) and len(out) == 3 and all(o["error"]["code"] == -32600 for o in out), out)


@check("RPC-006", "id must be string/integer: bool, float, null, array rejected with id null", "JSON-RPC 2.0 s4; MCP basic")
def _(c):
    cl = c.client()
    for bad in (True, 1.5, None, [], {}):
        out = cl.send({"jsonrpc": "2.0", "id": bad, "method": "ping"})
        expect(out["error"]["code"] == -32600 and out["id"] is None, (bad, out))


@check("RPC-007", "ids echoed verbatim (unicode string, huge int, negative, empty string)", "JSON-RPC 2.0 s5")
def _(c):
    cl = c.client()
    for mid in ("abc-\u00e9\u00f1-\U0001F600", 2 ** 62, -7, "", 0):
        out = cl.call("ping", mid=mid)
        expect(out["id"] == mid and type(out["id"]) is type(mid) and out["result"] == {}, out)


@check("RPC-008", "Notification (no id) yields no response", "JSON-RPC 2.0 s4.1")
def _(c):
    cl = c.client()
    cl.notify("ping")
    cl.notify("notifications/cancelled", {"requestId": 1})
    cl.notify("some/unknown/notification")


@check("RPC-009", "Batch: one response per request, none for notifications", "JSON-RPC 2.0 s6")
def _(c):
    cl = c.client()
    out = json.loads(cl.conn.raw(json.dumps([mk("ping", 1), {"jsonrpc": "2.0", "method": "ping"}, mk("ping", "b"), mk("nope", 3)])))
    expect(isinstance(out, list) and len(out) == 3, out)
    by_id = {o["id"]: o for o in out}
    expect(set(by_id) == {1, "b", 3} and by_id[3]["error"]["code"] == -32601 and by_id[1]["result"] == {}, out)


@check("RPC-010", "Batch of only notifications yields no response", "JSON-RPC 2.0 s6")
def _(c):
    cl = c.client()
    expect(cl.conn.raw(json.dumps([{"jsonrpc": "2.0", "method": "ping"}, {"jsonrpc": "2.0", "method": "x"}])) is None)


@check("RPC-011", "Unknown method -> -32601 (after initialize)", "JSON-RPC 2.0 s5.1")
def _(c):
    cl = c.client()
    e = cl.error("does/not/exist", code=-32601)
    expect(e["message"])


@check("RPC-012", "params of wrong type -> -32602 / -32600", "JSON-RPC 2.0 s5.1")
def _(c):
    cl = c.client()
    cl.error("tools/call", [1, 2], code=-32602)
    out = cl.send({"jsonrpc": "2.0", "id": 9, "method": "ping", "params": 5})
    expect(out["error"]["code"] == -32600, out)


@check("RPC-013", "Non-standard JSON constants (NaN) -> -32700", "JSON-RPC 2.0 s5.1 / RFC 8259")
def _(c):
    out = json.loads(c.client().conn.raw('{"jsonrpc":"2.0","id":1,"method":"ping","params":{"a":NaN}}'))
    expect(out["error"]["code"] == -32700, out)


@check("RPC-014", "Invalid UTF-8 -> -32700 and server survives", "JSON-RPC 2.0 s5.1 / RFC 8259 s8.1")
def _(c):
    cl = c.client()
    out = json.loads(cl.conn.raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"a":"\xff\xfe"}}'))
    expect(out["error"]["code"] == -32700, out)
    cl.result("ping")


@check("RPC-015", "Deeply nested JSON does not crash the server (-32700)", "robustness")
def _(c):
    cl = c.client()
    out = json.loads(cl.conn.raw("[" * 100000))
    expect(out["error"]["code"] == -32700, out)
    cl.result("ping")


# ---------------------------------------------------------------------------------
# Lifecycle / capability negotiation
# ---------------------------------------------------------------------------------
@check("LIF-001", "Requests before initialize are rejected (tools, resources, prompts)", "MCP lifecycle: initialization")
def _(c):
    cl = c.client(init=False)
    for m in ("tools/list", "tools/call", "resources/list", "resources/read", "resources/templates/list", "prompts/list", "prompts/get"):
        e = cl.error(m, {"name": "x", "uri": "memory://index"})
        expect(e["code"] == -32600 and e["data"]["reason"] == "not_initialized", (m, e))


@check("LIF-002", "ping is allowed before initialize", "MCP utilities: ping")
def _(c):
    expect(c.client(init=False).result("ping") == {})


@check("LIF-003", "Notifications before initialize are ignored silently", "MCP lifecycle")
def _(c):
    cl = c.client(init=False)
    cl.notify("notifications/initialized")
    cl.error("tools/list", code=-32600)


@check("LIF-004", "initialize negotiates each supported protocolVersion (echo)", "MCP lifecycle: version negotiation")
def _(c):
    for v in VERSIONS:
        res = c.client(init=False).result("initialize", {"protocolVersion": v, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        expect(res["protocolVersion"] == v, (v, res["protocolVersion"]))


@check("LIF-005", "Unsupported protocolVersion -> server answers with 2025-03-26", "MCP lifecycle: version negotiation")
def _(c):
    res = c.client(init=False).result("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    expect(res["protocolVersion"] == "2025-03-26", res)


@check("LIF-006", "initialize result: serverInfo + capabilities for tools/resources/prompts", "MCP lifecycle: capability negotiation")
def _(c):
    res = c.client(init=False).result("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    expect(isinstance(res["serverInfo"]["name"], str) and isinstance(res["serverInfo"]["version"], str), res)
    caps = res["capabilities"]
    for cap in ("tools", "resources", "prompts"):
        expect(isinstance(caps.get(cap), dict), cap)
    expect(caps["tools"].get("listChanged") is False and caps["resources"].get("subscribe") is False, caps)


@check("LIF-007", "initialize with invalid params -> -32602", "MCP lifecycle; JSON-RPC 2.0 s5.1")
def _(c):
    cl = c.client(init=False)
    cl.error("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}, code=-32602)
    cl.error("initialize", {"protocolVersion": 5, "capabilities": {}, "clientInfo": {"name": "a", "version": "1"}}, code=-32602)
    cl.error("initialize", None, code=-32602)


@check("LIF-008", "Second initialize on the same session is rejected", "MCP lifecycle")
def _(c):
    cl = c.client()
    e = cl.error("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, code=-32600)
    expect(e["data"]["reason"] == "already_initialized", e)


@check("LIF-009", "initialize inside a batch is rejected", "MCP 2025-03-26 basic: batching rules")
def _(c):
    cl = c.client(init=False)
    out = json.loads(cl.conn.raw(json.dumps([mk("initialize", 1, {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})])))
    expect(out[0]["error"]["code"] == -32600, out)
    cl.error("tools/list", code=-32600)


@check("LIF-010", "Batching allowed on 2025-03-26, rejected on negotiated 2025-06-18", "MCP changelog 2025-06-18 (batching removed)")
def _(c):
    a = c.client(version="2025-03-26")
    out = json.loads(a.conn.raw(json.dumps([mk("ping", 1)])))
    expect(out[0].get("result") == {}, out)
    b = c.client(version="2025-06-18")
    out = json.loads(b.conn.raw(json.dumps([mk("ping", 1), mk("tools/list", 2)])))
    expect(out[1]["error"]["code"] == -32600 and out[1]["error"]["data"]["reason"] == "batch_not_supported", out)


# ---------------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------------
@check("TLS-001", "tools/list returns strict object schemas (additionalProperties:false)", "MCP server/tools: listing")
def _(c):
    tools = c.client().result("tools/list")["tools"]
    names = {t["name"] for t in tools}
    expect({"memory_store", "memory_recall", "memory_list", "memory_forget"} <= names, names)
    for t in tools:
        expect(t["description"] and t["inputSchema"]["type"] == "object" and t["inputSchema"]["additionalProperties"] is False, t["name"])


@check("TLS-002", "Unknown tool -> -32602; missing name -> -32602", "MCP server/tools: error handling")
def _(c):
    cl = c.client()
    cl.error("tools/call", {"name": "nope", "arguments": {}}, code=-32602)
    cl.error("tools/call", {"arguments": {}}, code=-32602)
    cl.error("tools/call", {"name": 5}, code=-32602)


@check("TLS-003", "Schema violations are tool errors (isError:true), not protocol errors", "MCP server/tools: tool execution errors")
def _(c):
    cl = c.client()
    cases = [("memory_store", {}), ("memory_store", {"content": "x", "extra": 1}), ("memory_store", {"content": "x", "importance": 9}),
             ("memory_store", {"content": 5}), ("memory_store", {"content": "x", "tags": ["ok", 3]}),
             ("memory_recall", {"query": "x", "limit": 0}), ("memory_list", {"offset": -1}), ("memory_forget", {"id": "1"}),
             ("memory_store", {"content": "x", "importance": True})]
    for name, args in cases:
        res = cl.tool(name, args)
        expect(res["isError"] is True and res["content"][0]["type"] == "text", (name, args, res))


@check("TLS-004", "memory_store -> memory_recall round trip (case/accent-insensitive)", "MCP server/tools: calling")
def _(c):
    cl = c.client()
    marker = "zq%d" % int(time.time() * 1000)
    stored = cl.tool("memory_store", {"content": "La canci\u00f3n de %s" % marker, "tags": ["m\u00fasica"], "importance": 5})
    expect(stored["isError"] is False)
    found = json.loads(cl.tool("memory_recall", {"query": "CANCION " + marker.upper()})["content"][0]["text"])
    expect(found["count"] == 1 and found["results"][0]["tags"] == ["m\u00fasica"], found)


@check("TLS-005", "memory_forget removes an entry; unknown id is a tool error", "MCP server/tools")
def _(c):
    cl = c.client()
    marker = "fg%d" % int(time.time() * 1000)
    stored = json.loads(cl.tool("memory_store", {"content": marker})["content"][0]["text"])["stored"]
    expect(cl.tool("memory_forget", {"id": stored["id"]})["isError"] is False)
    expect(cl.tool("memory_forget", {"id": stored["id"]})["isError"] is True)
    expect(json.loads(cl.tool("memory_recall", {"query": marker})["content"][0]["text"])["count"] == 0)


@check("TLS-006", "structuredContent only on negotiated 2025-06-18", "MCP 2025-06-18 server/tools: structured content")
def _(c):
    old = c.client(version="2025-03-26").tool("memory_list", {"limit": 1})
    new = c.client(version="2025-06-18").tool("memory_list", {"limit": 1})
    expect("structuredContent" not in old and "structuredContent" in new and "total" in new["structuredContent"], (old, new))


@check("TLS-007", "tools/list rejects unsupported cursor; accepts _meta", "MCP utilities: pagination")
def _(c):
    cl = c.client()
    cl.error("tools/list", {"cursor": "abc"}, code=-32602)
    expect("tools" in cl.result("tools/list", {"_meta": {"progressToken": 1}}))


# ---------------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------------
@check("RES-001", "resources/list exposes memory://index and audit://chain", "MCP server/resources: listing")
def _(c):
    uris = {r["uri"] for r in c.client().result("resources/list")["resources"]}
    expect({"memory://index", "audit://chain"} <= uris, uris)


@check("RES-002", "resources/read memory://index returns JSON contents", "MCP server/resources: reading")
def _(c):
    res = c.client().result("resources/read", {"uri": "memory://index"})
    item = res["contents"][0]
    expect(item["uri"] == "memory://index" and item["mimeType"] == "application/json" and "total" in json.loads(item["text"]), res)


@check("RES-003", "resources/templates/list includes memory://entry/{id}", "MCP server/resources: templates")
def _(c):
    tpl = c.client().result("resources/templates/list")["resourceTemplates"]
    expect("memory://entry/{id}" in {t["uriTemplate"] for t in tpl}, tpl)


@check("RES-004", "Read of a stored entry via template URI", "MCP server/resources")
def _(c):
    cl = c.client()
    stored = json.loads(cl.tool("memory_store", {"content": "resource-read check"})["content"][0]["text"])["stored"]
    res = cl.result("resources/read", {"uri": "memory://entry/%d" % stored["id"]})
    expect(json.loads(res["contents"][0]["text"])["content"] == "resource-read check", res)


@check("RES-005", "Unknown resource -> -32002; missing uri -> -32602", "MCP server/resources: error handling")
def _(c):
    cl = c.client()
    cl.error("resources/read", {"uri": "memory://entry/99999999"}, code=-32002)
    cl.error("resources/read", {"uri": "file:///etc/passwd"}, code=-32002)
    cl.error("resources/read", {"uri": "memory://entry/abc"}, code=-32002)
    cl.error("resources/read", {}, code=-32602)


@check("RES-006", "audit://chain reports a valid chain and zero contract violations", "formal-verification hooks")
def _(c):
    body = json.loads(c.client().result("resources/read", {"uri": "audit://chain"})["contents"][0]["text"])
    expect(body["chain"]["ok"] is True and body["contracts"]["violated"] == 0 and body["contracts"]["checked"] > 0, body["contracts"])


# ---------------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------------
@check("PRM-001", "prompts/list declares arguments", "MCP server/prompts: listing")
def _(c):
    prompts = {p["name"]: p for p in c.client().result("prompts/list")["prompts"]}
    args = {a["name"]: a for a in prompts["recall_and_summarize"]["arguments"]}
    expect(args["query"]["required"] is True and "limit" in args, prompts)


@check("PRM-002", "prompts/get embeds recalled memories as user message", "MCP server/prompts: getting")
def _(c):
    cl = c.client()
    marker = "pr%d" % int(time.time() * 1000)
    cl.tool("memory_store", {"content": "prompt marker " + marker})
    res = cl.result("prompts/get", {"name": "recall_and_summarize", "arguments": {"query": marker, "limit": "3", "style": "terse"}})
    msg = res["messages"][0]
    expect(msg["role"] == "user" and msg["content"]["type"] == "text" and marker in msg["content"]["text"], res)


@check("PRM-003", "prompts/get validates arguments (-32602)", "MCP server/prompts: error handling")
def _(c):
    cl = c.client()
    cl.error("prompts/get", {"name": "recall_and_summarize", "arguments": {}}, code=-32602)
    cl.error("prompts/get", {"name": "recall_and_summarize", "arguments": {"query": "x", "style": "poem"}}, code=-32602)
    cl.error("prompts/get", {"name": "recall_and_summarize", "arguments": {"query": "x", "limit": "99"}}, code=-32602)
    cl.error("prompts/get", {"name": "recall_and_summarize", "arguments": {"query": "x", "bogus": "1"}}, code=-32602)
    cl.error("prompts/get", {"name": "recall_and_summarize", "arguments": {"query": 5}}, code=-32602)
    cl.error("prompts/get", {"name": "nope"}, code=-32602)


# ---------------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------------
def _auth_rejected(c, token, label):
    cl = c.client(init=False, token=token)
    if c.transport == "stdio":
        e = cl.error("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        expect(e["code"] in (-32001, -32003), (label, e))
        cl.error("tools/list", code=-32600)  # still not initialized
    else:
        status, _ = cl.conn.request("POST", "/mcp", json.dumps(mk("initialize", 1, {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})),
                                    {"Content-Type": "application/json"})
        expect(status in (401, 403), (label, status))
        expect("www-authenticate" in cl.conn.headers or status == 403, "401 must carry WWW-Authenticate")


@check("AUT-001", "Missing token rejected", "RFC 6750 s3 (simplified)")
def _(c):
    _auth_rejected(c, None, "no token")


@check("AUT-002", "Expired token rejected (exp)", "RFC 7519 s4.1.4")
def _(c):
    _auth_rejected(c, c.env.mint(ttl=-120), "expired")


@check("AUT-003", "Tampered signature rejected (constant-time HMAC compare)", "RFC 7515 s5.2")
def _(c):
    t = c.env.mint()
    head, body, sig = t.split(".")
    flipped = sig[:-3] + ("AAA" if not sig.endswith("AAA") else "BBB")
    _auth_rejected(c, ".".join([head, body, flipped]), "bad signature")
    other = c.env.mint(sub="someone-else")
    _auth_rejected(c, ".".join([head, other.split(".")[1], sig]), "payload swapped under old signature")


@check("AUT-004", "Wrong audience rejected (aud)", "RFC 7519 s4.1.3")
def _(c):
    _auth_rejected(c, c.env.mint(aud="some-other-service"), "wrong aud")


@check("AUT-005", "Wrong issuer rejected (iss)", "RFC 7519 s4.1.1")
def _(c):
    _auth_rejected(c, c.env.mint(issuer="http://evil.example"), "wrong iss")


@check("AUT-006", "alg=none and garbage tokens rejected", "RFC 8725 s3.1")
def _(c):
    import base64
    def b(x):
        return base64.urlsafe_b64encode(json.dumps(x).encode()).rstrip(b"=").decode()
    now = int(time.time())
    none_tok = b({"alg": "none", "typ": "JWT"}) + "." + b({"iss": c.env.issuer, "sub": "x", "aud": "mcp-resource-heavy", "iat": now, "exp": now + 99, "scope": "mcp"}) + "."
    _auth_rejected(c, none_tok, "alg none")
    _auth_rejected(c, "not.a.token", "garbage")
    _auth_rejected(c, "x" * 20000, "oversized")


@check("AUT-007", "Per-tool scope enforced (-32003): read-only token cannot store/forget", "scope claims")
def _(c):
    cl = c.client(token=c.env.mint(scope="mcp memory:read"))
    cl.error("tools/call", {"name": "memory_store", "arguments": {"content": "x"}}, code=-32003)
    cl.error("tools/call", {"name": "memory_forget", "arguments": {"id": 1}}, code=-32003)
    cl.error("resources/read", {"uri": "audit://chain"}, code=-32003)
    expect(cl.result("tools/call", {"name": "memory_list", "arguments": {}})["isError"] is False)
    uris = {r["uri"] for r in cl.result("resources/list")["resources"]}
    expect("audit://chain" not in uris and "memory://index" in uris, uris)


@check("AUT-008", "Token lacking base 'mcp' scope rejected at initialize", "scope claims")
def _(c):
    _auth_rejected(c, c.env.mint(scope="memory:read"), "no base scope")


@check("AUT-009", "Mock issuer: discovery document + client_credentials token works end to end", "OIDC Discovery 1.0 s3 (simplified); RFC 6749 s4.4", ("http",))
def _(c):
    conn = HttpConn(c.env)
    c.opened.append(conn)
    st, body = conn.request("GET", "/.well-known/openid-configuration")
    doc = json.loads(body)
    expect(st == 200 and doc["issuer"] == c.env.issuer and doc["token_endpoint"].endswith("/oauth/token") and "client_credentials" in doc["grant_types_supported"], doc)
    secret = c.env.cli("client-secret", "--data-dir", c.env.data_dir, "demo-client").stdout.strip()
    form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": "demo-client", "client_secret": secret, "scope": "mcp memory:read"})
    st, body = conn.request("POST", "/oauth/token", form, {"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.loads(body)
    expect(st == 200 and tok["token_type"] == "Bearer" and tok["scope"] == "mcp memory:read", body)
    cl = Client(HttpConn(c.env, tok["access_token"]))
    c.opened.append(cl.conn)
    cl.initialize()
    st, body = conn.request("POST", "/oauth/token", form.replace(secret, "0" * 32), {"Content-Type": "application/x-www-form-urlencoded"})
    expect(st == 401 and json.loads(body)["error"] == "invalid_client", (st, body))
    bad_scope = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": "readonly-client",
                                        "client_secret": c.env.cli("client-secret", "--data-dir", c.env.data_dir, "readonly-client").stdout.strip(),
                                        "scope": "mcp memory:write"})
    st, body = conn.request("POST", "/oauth/token", bad_scope, {"Content-Type": "application/x-www-form-urlencoded"})
    expect(st == 400 and json.loads(body)["error"] == "invalid_scope", (st, body))


@check("AUT-010", "--no-auth is an explicit opt-in and then needs no token", "design requirement", ("stdio",))
def _(c):
    cl = c.client(token=None, no_auth=True)
    expect(cl.result("tools/list")["tools"])


# ---------------------------------------------------------------------------------
# HTTP transport specifics
# ---------------------------------------------------------------------------------
def _init_msg(v="2025-03-26"):
    return json.dumps(mk("initialize", 1, {"protocolVersion": v, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}))


@check("HTP-001", "GET /mcp -> 405 with Allow header", "MCP 2025-03-26 transports: Streamable HTTP", ("http",))
def _(c):
    conn = c.conn()
    st, _ = conn.request("GET", "/mcp", headers={"Accept": "text/event-stream"})
    expect(st == 405 and "POST" in conn.headers.get("allow", ""), (st, conn.headers))


@check("HTP-002", "Notification-only POST -> 202 Accepted, empty body", "MCP 2025-03-26 transports", ("http",))
def _(c):
    cl = c.client()
    st, body = cl.conn.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), {"Content-Type": "application/json"})
    expect(st == 202 and body == "", (st, body))


@check("HTP-003", "Mcp-Session-Id issued at initialize, required afterwards, 404 when unknown, DELETE ends it", "MCP 2025-03-26 transports: session management", ("http",))
def _(c):
    conn = c.conn()
    st, body = conn.request("POST", "/mcp", _init_msg(), {"Content-Type": "application/json"})
    sid = conn.headers.get("mcp-session-id")
    expect(st == 200 and sid and len(sid) >= 16, (st, conn.headers))
    conn.session = sid
    st, body = conn.request("POST", "/mcp", json.dumps(mk("tools/list", 2)), {"Content-Type": "application/json"})
    expect(st == 200 and "tools" in json.loads(body)["result"])
    st, body = conn.request("POST", "/mcp", json.dumps(mk("tools/list", 2)), {"Content-Type": "application/json"}, use_session=False)
    expect(st == 400 and json.loads(body)["error"]["code"] == -32600, (st, body))
    st, _ = conn.request("POST", "/mcp", json.dumps(mk("ping", 3)), {"Content-Type": "application/json", "Mcp-Session-Id": "deadbeef" * 4})
    expect(st == 404, st)
    st, _ = conn.request("DELETE", "/mcp")
    expect(st == 200, st)
    st, _ = conn.request("POST", "/mcp", json.dumps(mk("ping", 4)), {"Content-Type": "application/json"})
    expect(st == 404, st)


@check("HTP-004", "Wrong Content-Type -> 415; Accept without JSON -> 406", "HTTP semantics", ("http",))
def _(c):
    conn = c.conn()
    st, _ = conn.request("POST", "/mcp", _init_msg(), {"Content-Type": "text/plain"})
    expect(st == 415, st)
    st, _ = conn.request("POST", "/mcp", _init_msg(), {"Content-Type": "application/json", "Accept": "text/html"})
    expect(st == 406, st)


@check("HTP-005", "Session bound to subject: another token cannot reuse the session id", "security design", ("http",))
def _(c):
    a = c.conn(token=c.env.mint(sub="alice"))
    st, _ = a.request("POST", "/mcp", _init_msg(), {"Content-Type": "application/json"})
    sid = a.headers["mcp-session-id"]
    b = c.conn(token=c.env.mint(sub="bob"))
    st, _ = b.request("POST", "/mcp", json.dumps(mk("ping", 2)), {"Content-Type": "application/json", "Mcp-Session-Id": sid})
    expect(st == 403, st)


@check("HTP-006", "Foreign Origin header rejected (DNS-rebinding defence)", "MCP transports: security warning", ("http",))
def _(c):
    conn = c.conn()
    st, _ = conn.request("POST", "/mcp", _init_msg(), {"Content-Type": "application/json", "Origin": "http://evil.example"})
    expect(st == 403, st)
    st, _ = conn.request("POST", "/mcp", _init_msg(), {"Content-Type": "application/json", "Origin": "http://localhost:3000"})
    expect(st == 200, st)


@check("HTP-007", "Oversized body -> 413", "robustness", ("http",))
def _(c):
    conn = c.conn()
    conn.http.putrequest("POST", "/mcp")
    for k, v in {"Content-Type": "application/json", "Content-Length": str(50_000_000), "Authorization": "Bearer " + conn.token}.items():
        conn.http.putheader(k, v)
    conn.http.endheaders()
    resp = conn.http.getresponse()
    resp.read()
    expect(resp.status == 413, resp.status)


@check("HTP-008", "Unsupported MCP-Protocol-Version header -> 400", "MCP 2025-06-18 transports: protocol version header", ("http",))
def _(c):
    conn = c.conn()
    st, _ = conn.request("POST", "/mcp", _init_msg(), {"Content-Type": "application/json", "MCP-Protocol-Version": "1999-01-01"})
    expect(st == 400, st)


@check("HTP-009", "Batch over HTTP returns a JSON array (application/json)", "JSON-RPC 2.0 s6", ("http",))
def _(c):
    cl = c.client()
    st, body = cl.conn.request("POST", "/mcp", json.dumps([mk("ping", 1), mk("ping", 2)]), {"Content-Type": "application/json"})
    expect(st == 200 and cl.conn.headers["content-type"].startswith("application/json") and len(json.loads(body)) == 2, (st, body))


@check("HTP-010", "Parse error over HTTP -> 400 with JSON-RPC body", "JSON-RPC 2.0 over HTTP", ("http",))
def _(c):
    cl = c.client()
    st, body = cl.conn.request("POST", "/mcp", "{nope", {"Content-Type": "application/json"})
    expect(st == 400 and json.loads(body)["error"]["code"] == -32700, (st, body))


# ---------------------------------------------------------------------------------
# stdio specifics
# ---------------------------------------------------------------------------------
@check("STD-001", "Memory persists across server restarts (new process, same data dir)", "design requirement", ("stdio",))
def _(c):
    marker = "persist%d" % int(time.time() * 1000)
    a = c.client()
    a.tool("memory_store", {"content": "survives restart " + marker, "tags": ["restart"]})
    expect(a.close() == 0, "server should exit 0 on EOF")
    b = c.client()
    found = json.loads(b.tool("memory_recall", {"query": marker})["content"][0]["text"])
    expect(found["count"] == 1, found)


@check("STD-002", "CRLF frames accepted, blank lines ignored, stdout carries only JSON", "MCP 2025-03-26 transports: stdio", ("stdio",))
def _(c):
    cl = c.client()
    out = cl.conn.raw(json.dumps(mk("ping", 1)) + "\r")
    expect(json.loads(out)["result"] == {}, out)
    cl.conn._write(b"\n\n   \n")
    cl.result("ping")


@check("STD-003", "Oversized line (>1 MiB) -> -32600 and the server keeps serving", "robustness", ("stdio",))
def _(c):
    cl = c.client()
    out = json.loads(cl.conn.raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"a":"' + b"x" * (1 << 21) + b'"}}'))
    expect(out["error"]["code"] == -32600, out)
    cl.result("ping")


@check("STD-004", "Server exits cleanly (0) on stdin EOF", "MCP 2025-03-26 transports: stdio shutdown", ("stdio",))
def _(c):
    cl = c.client()
    expect(cl.close() == 0)


# ---------------------------------------------------------------------------------
# Fuzz (seeded) + audit
# ---------------------------------------------------------------------------------
@check("FUZ-001", "Seeded fuzz (200 cases): never crashes, valid JSON-RPC, no -32603, ids echoed", "property-based robustness")
def _(c):
    cl = c.client()
    failures = []
    cases = fuzz.gen_cases(20261014, 200)
    for case in cases:
        if c.transport == "stdio" and not cl.conn.alive():
            raise CheckFailed("server died during fuzz")
        if c.transport == "stdio" and not case.strip():
            continue  # blank lines are ignored by the stdio framing, by design (STD-002)
        out = cl.conn.raw(case)
        problem = fuzz.check_output(case, out)
        if problem:
            failures.append((case[:80], problem))
    expect(not failures, failures[:3])
    cl.result("ping")


@check("AUD-001", "Audit log chain verifies via CLI; flipping one byte is detected; truncation caught with anchor", "hash-chained audit")
def _(c):
    path = os.path.join(c.env.data_dir, "audit.log")
    res = c.env.cli("audit-verify", "--data-dir", c.env.data_dir)
    expect(res.returncode == 0, res.stderr)
    info = verify_file(path)
    expect(info["ok"] and info["count"] > 10, info)
    copy = os.path.join(c.env.tmp, "audit-%s.copy" % c.transport)
    data = bytearray(open(path, "rb").read())
    idx = data.find(b'"payload"', len(data) // 2) + 20
    data[idx] = ord("X") if data[idx] != ord("X") else ord("Y")
    open(copy, "wb").write(bytes(data))
    bad = c.env.cli("audit-verify", "--file", copy, "--data-dir", c.env.data_dir)
    expect(bad.returncode == 1, "tampered log not detected")
    lines = open(path, "rb").read().splitlines(True)
    open(copy, "wb").write(b"".join(lines[:-1]))
    expect(verify_file(copy)["ok"], "dropping the last line is invisible without an anchor (documented limitation)")
    expect(not verify_file(copy, expect_head=info["head"])["ok"], "anchor must catch tail truncation")
    open(copy, "wb").write(b"".join(lines[:5] + lines[6:]))
    expect(not verify_file(copy)["ok"], "mid-chain deletion not detected")


# ---------------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------------
def run_suite(transports=("stdio", "http")):
    env = Env()
    results = []
    started = time.time()
    try:
        if "http" in transports:
            env.start_http()
        for transport in transports:
            ctx = Ctx(env, transport)
            for chk in CHECKS:
                if transport not in chk["transports"]:
                    continue
                t0 = time.time()
                try:
                    chk["fn"](ctx)
                    status, detail = "pass", ""
                except CheckFailed as exc:
                    status, detail = "fail", str(exc)[:400]
                except Exception as exc:  # noqa: BLE001
                    status, detail = "error", "%s: %s" % (type(exc).__name__, str(exc)[:400])
                finally:
                    ctx.close_all()
                    ctx.opened = []
                results.append({"id": chk["id"], "title": chk["title"], "spec": chk["spec"], "transport": transport,
                                "status": status, "detail": detail, "ms": int((time.time() - t0) * 1000)})
        # final global invariants, read through the protocol itself
        final = []
        for transport in transports:
            ctx = Ctx(env, transport)
            try:
                body = json.loads(ctx.client().result("resources/read", {"uri": "audit://chain"})["contents"][0]["text"])
                ok = body["chain"]["ok"] and body["contracts"]["violated"] == 0
                final.append({"transport": transport, "chain_ok": body["chain"]["ok"], "chain_entries": body["chain"]["count"],
                              "chain_head": body["chain"]["head"], "contracts": body["contracts"], "ok": ok})
            finally:
                ctx.close_all()
        env.stop_http()
        chain = verify_file(os.path.join(env.data_dir, "audit.log"))
    finally:
        env.cleanup()
    passed = sum(1 for r in results if r["status"] == "pass")
    report = {
        "suite": "mcp-resource-heavy conformance", "server_version": __version__, "python": sys.version.split()[0],
        "started_unix": int(started), "duration_s": round(time.time() - started, 2),
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
        "final_invariants": final, "final_audit_chain": chain, "results": results,
        "note": "Black-box suite written by the same author as the server; it demonstrates self-consistency and "
                "coverage of the listed spec clauses, not independent certification.",
    }
    return report


def format_report(report, quiet=False):
    lines = []
    if not quiet:
        lines.append("%-8s %-6s %-5s %s" % ("ID", "TRANSP", "RES", "TITLE"))
        for r in report["results"]:
            mark = "PASS" if r["status"] == "pass" else "FAIL"
            lines.append("%-8s %-6s %-5s %s" % (r["id"], r["transport"], mark, r["title"]))
            if r["status"] != "pass":
                lines.append("         -> %s" % r["detail"])
        for f in report["final_invariants"]:
            lines.append("final[%s]: chain_ok=%s entries=%s contract_checks=%s violations=%s" % (
                f["transport"], f["chain_ok"], f["chain_entries"], f["contracts"]["checked"], f["contracts"]["violated"]))
    s = report["summary"]
    lines.append("CONFORMANCE: %d/%d checks passed, %d failed (%.1fs)" % (s["passed"], s["total"], s["failed"], report["duration_s"]))
    return "\n".join(lines)


def run_cli(args):
    transports = ("stdio", "http") if args.transport == "both" else (args.transport,)
    report = run_suite(transports)
    print(format_report(report, args.quiet))
    if args.json_path:
        with open(args.json_path, "w") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        print("JSON report written to %s" % args.json_path)
    ok = report["summary"]["failed"] == 0 and all(f["ok"] for f in report["final_invariants"])
    return 0 if ok else 1
