"""Shared test helpers (stdlib only)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcpres.auth import Authenticator  # noqa: E402
from mcpres.server import McpServer  # noqa: E402
from mcpres.transport_http import HttpTransport  # noqa: E402

SECRET = b"unit-test-secret-0123456789abcdef"
ISSUER = "http://issuer.test"


def init_msg(version="2025-03-26", mid=1):
    return {"jsonrpc": "2.0", "id": mid, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "unit", "version": "0"}}}


def rpc(method, params=None, mid=1):
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return m


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcpres-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_dir = os.path.join(self.tmp, "data")


class InProc:
    """Drive a Session in-process with dicts."""

    def __init__(self, session):
        self.session = session

    def send(self, obj):
        out = self.session.handle_text(obj if isinstance(obj, str) else json.dumps(obj))
        return None if out is None else json.loads(out)

    def init(self, version="2025-03-26"):
        res = self.send(init_msg(version))
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    def call(self, method, params=None, mid=1):
        return self.send(rpc(method, params, mid))

    def tool(self, name, args, mid=1):
        return self.send(rpc("tools/call", {"name": name, "arguments": args}, mid))


def make_server(data_dir, auth=True, llm=None, fsync=False):
    authenticator = Authenticator(SECRET, ISSUER) if auth else None
    return McpServer(data_dir, authenticator, llm, audit_fsync=fsync)


def token(scopes="mcp memory:read memory:write audit:read", **kw):
    a = Authenticator(SECRET, ISSUER)
    ttl = kw.pop("ttl", 3600)
    return a.mint(scopes=scopes.split(), ttl=ttl, **kw)


def run_cli(*args, env=None, input_text=None, timeout=60):
    e = dict(os.environ, PYTHONPATH=ROOT)
    e.pop("MCP_TOKEN", None)
    e.update(env or {})
    return subprocess.run([sys.executable, "-m", "mcpres", *args], capture_output=True, text=True, env=e,
                          cwd=ROOT, input=input_text, timeout=timeout)


class StdioServer:
    """Run `mcpres serve` as a subprocess; talk newline-delimited JSON."""

    def __init__(self, data_dir, token=None, extra=(), env=None):
        e = dict(os.environ, PYTHONPATH=ROOT)
        e.pop("MCP_TOKEN", None)
        e["MCP_FORGE_SECRET"] = SECRET.decode()
        if token:
            e["MCP_TOKEN"] = token
        e.update(env or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcpres", "serve", "--data-dir", data_dir, "--issuer", ISSUER, *extra],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, cwd=ROOT)

    def send(self, obj):
        self.proc.stdin.write((obj if isinstance(obj, str) else json.dumps(obj)).encode() + b"\n")
        self.proc.stdin.flush()

    def recv(self):
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def call(self, method, params=None, mid=1):
        self.send(rpc(method, params, mid))
        return self.recv()

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(10)
        err = self.proc.stderr.read().decode()
        self.proc.stdout.close()
        self.proc.stderr.close()
        return self.proc.returncode, err


def start_http(server):
    t = HttpTransport(server, "127.0.0.1", 0)
    th = threading.Thread(target=t.serve_forever, daemon=True)
    th.start()
    return t
