"""Streamable-HTTP-style transport on http.server (stdlib).

* POST /mcp            JSON-RPC request/notification/batch -> 200 application/json, or 202 (nothing to say)
* GET  /mcp            405 (no server-initiated SSE stream is offered) with ``Allow: POST, DELETE``
* DELETE /mcp          terminates the session named by ``Mcp-Session-Id``
* GET  /.well-known/openid-configuration   mock issuer discovery document (auth enabled only)
* POST /oauth/token    mock issuer, client_credentials grant (auth enabled only)
* GET  /healthz        unauthenticated liveness probe

Not offered: SSE streaming responses, resumability, server->client requests. Sessions are bound to
the token subject that created them.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from . import SUPPORTED_PROTOCOL_VERSIONS
from .auth import BASE_SCOPE, AuthError, OidcIssuer
from .jsonrpc import INVALID_REQUEST, PARSE_ERROR, dumps

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


class HttpTransport:
    def __init__(self, server, host="127.0.0.1", port=8765, max_body=1 << 20, max_sessions=256,
                 session_ttl=3600, allowed_origins=()):
        self.server = server
        self.auth = server.auth
        self.max_body = max_body
        self.max_sessions = max_sessions
        self.session_ttl = session_ttl
        self.allowed_origins = set(allowed_origins)
        self.sessions = {}
        self._lock = threading.Lock()
        self.check_host = host in LOOPBACK
        transport = self

        class Handler(_Handler):
            app = transport

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.host, self.port = self.httpd.server_address[0], self.httpd.server_address[1]
        self.base_url = "http://%s:%d" % (self.host if ":" not in self.host else "[%s]" % self.host, self.port)
        self.issuer = None
        if self.auth is not None:
            if self.auth.issuer is None:
                self.auth.issuer = self.base_url
            self.issuer = OidcIssuer(self.auth)

    def serve_forever(self):
        self.httpd.serve_forever(poll_interval=0.1)

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- session registry -------------------------------------------------------
    def register(self, session):
        with self._lock:
            now = time.time()
            for sid in [k for k, s in self.sessions.items() if now - s.last_used > self.session_ttl]:
                del self.sessions[sid]
            while len(self.sessions) >= self.max_sessions:
                oldest = min(self.sessions, key=lambda k: self.sessions[k].last_used)
                del self.sessions[oldest]
            self.sessions[session.id] = session

    def lookup(self, sid):
        with self._lock:
            s = self.sessions.get(sid)
            if s and time.time() - s.last_used > self.session_ttl:
                del self.sessions[sid]
                return None
            return s

    def drop(self, sid):
        with self._lock:
            return self.sessions.pop(sid, None) is not None


class _Handler(BaseHTTPRequestHandler):
    app = None
    protocol_version = "HTTP/1.1"
    server_version = "mcp-resource-heavy"
    sys_version = ""
    timeout = 30

    def log_message(self, fmt, *args):  # audit log is the record; keep stderr clean
        pass

    # ---- plumbing --------------------------------------------------------------
    def _send(self, status, body=b"", content_type=None, headers=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj, headers=None):
        self._send(status, dumps(obj).encode("ascii"), "application/json", headers)

    def _event(self, kind, detail, session="", sub=""):
        self.app.server.log_event(kind, detail, "http", session, sub)

    def _path(self):
        return urlsplit(self.path).path

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if origin is not None:
            host = urlsplit(origin).hostname or ""
            if host not in LOOPBACK and origin not in self.app.allowed_origins:
                self._event("origin_rejected", {"origin": origin[:100]})
                self._json(403, {"error": "forbidden_origin"})
                return False
        if self.app.check_host:
            host_header = self.headers.get("Host", "")
            host = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else host_header.split("]")[0] + "]"
            if host not in LOOPBACK:
                self._event("host_rejected", {"host": host_header[:100]})
                self._json(403, {"error": "forbidden_host"})
                return False
        return True

    def _authenticate(self):
        """Return (ok, claims). On failure the HTTP response has already been sent."""
        auth = self.app.auth
        if auth is None:
            return True, None
        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            self._event("auth_failure", {"reason": "missing bearer token"})
            self._json(401, {"error": "invalid_request", "error_description": "Bearer token required"},
                       {"WWW-Authenticate": 'Bearer realm="mcp-resource-heavy"'})
            return False, None
        try:
            return True, auth.verify(header[7:].strip(), [BASE_SCOPE])
        except AuthError as exc:
            self._event("auth_failure", {"error": exc.error, "reason": exc.description})
            hdr = 'Bearer realm="mcp-resource-heavy", error="%s", error_description="%s"' % (exc.error, exc.description.replace('"', "'"))
            if exc.error == "insufficient_scope":
                hdr += ', scope="%s"' % BASE_SCOPE
            self._json(exc.status, {"error": exc.error, "error_description": exc.description}, {"WWW-Authenticate": hdr})
            return False, None

    def _read_body(self, limit):
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            self._json(411, {"error": "Content-Length required"})
            return None
        if int(length) > limit:
            self._event("request_too_large", {"limit": limit})
            self.close_connection = True
            self._json(413, {"error": "payload_too_large"})
            return None
        return self.rfile.read(int(length))

    # ---- verbs -------------------------------------------------------------------
    def do_GET(self):
        path = self._path()
        if path == "/healthz":
            return self._json(200, {"status": "ok"})
        if path == "/.well-known/openid-configuration" and self.app.issuer:
            return self._json(200, self.app.issuer.discovery_document())
        if path == "/mcp":
            return self._json(405, {"error": "method_not_allowed", "detail": "no SSE stream offered; use POST"},
                              {"Allow": "POST, DELETE"})
        self._json(404, {"error": "not_found"})

    def do_POST(self):
        path = self._path()
        if path == "/mcp":
            return self._post_mcp()
        if path == "/oauth/token" and self.app.issuer:
            return self._post_token()
        self._json(404, {"error": "not_found"})

    def do_DELETE(self):
        if self._path() != "/mcp":
            return self._json(404, {"error": "not_found"})
        if not self._origin_ok():
            return
        ok, claims = self._authenticate()
        if not ok:
            return
        sid = self.headers.get("Mcp-Session-Id")
        if not sid:
            return self._json(400, {"error": "Mcp-Session-Id header required"})
        session = self.app.lookup(sid)
        if session is None:
            return self._json(404, {"error": "unknown_session"})
        if self.app.auth and (claims or {}).get("sub") != session.claims.get("sub"):
            return self._json(403, {"error": "session_belongs_to_another_subject"})
        self.app.drop(sid)
        self._event("session_closed", {}, sid, (claims or {}).get("sub", ""))
        self._send(200, b"", None)

    def do_PUT(self):
        self._json(405, {"error": "method_not_allowed"}, {"Allow": "POST, DELETE"})

    do_PATCH = do_PUT

    # ---- /oauth/token ------------------------------------------------------------------
    def _post_token(self):
        body = self._read_body(8192)
        if body is None:
            return
        if not (self.headers.get("Content-Type", "").lower().startswith("application/x-www-form-urlencoded")):
            return self._json(400, {"error": "invalid_request", "error_description": "form-encoded body required"})
        try:
            form = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        except UnicodeDecodeError:
            return self._json(400, {"error": "invalid_request"})
        status, payload = self.app.issuer.token_request(self.headers.get("Authorization"), form)
        self._event("token_issued" if status == 200 else "token_denied",
                    {"client": form.get("client_id", "(basic)")[:64], "status": status})
        self._json(status, payload, {"Pragma": "no-cache"})

    # ---- POST /mcp ---------------------------------------------------------------------
    def _post_mcp(self):
        app = self.app
        # Always consume the body first: answering early (401/403/415...) with an unread body would
        # desynchronise a keep-alive connection. Size is bounded by max_body (413 closes the socket).
        body = self._read_body(app.max_body)
        if body is None:
            return
        if not self._origin_ok():
            return
        ok, claims = self._authenticate()
        if not ok:
            return
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(415, {"error": "Content-Type must be application/json"})
        accept = self.headers.get("Accept")
        if accept is not None and not any(a in accept for a in ("application/json", "*/*", "application/*")):
            return self._json(406, {"error": "Accept must allow application/json"})
        proto = self.headers.get("MCP-Protocol-Version")
        if proto is not None and proto not in SUPPORTED_PROTOCOL_VERSIONS:
            return self._json(400, {"error": "unsupported MCP-Protocol-Version", "supported": list(SUPPORTED_PROTOCOL_VERSIONS)})
        sid = self.headers.get("Mcp-Session-Id")
        sub = (claims or {}).get("sub", "")
        if sid:
            session = app.lookup(sid)
            if session is None:
                return self._json(404, {"error": "unknown_session"})
            if app.auth and sub != (session.claims or {}).get("sub"):
                self._event("session_hijack_attempt", {"sub": sub}, sid, sub)
                return self._json(403, {"error": "session_belongs_to_another_subject"})
            if proto is not None and session.protocol_version and proto != session.protocol_version:
                return self._json(400, {"error": "MCP-Protocol-Version differs from negotiated version"})
            fresh = False
        else:
            session = app.server.new_session("http")
            fresh = True
        out = session.dispatch_http(body, claims)
        headers = {}
        if fresh and session.initialized:
            app.register(session)
            headers["Mcp-Session-Id"] = session.id
        if out is None:
            return self._send(202, b"", None, headers)
        status = 200
        if out.startswith("{"):  # single response object: structural failures map to HTTP 400
            code = json.loads(out).get("error", {}).get("code")
            if code in (PARSE_ERROR, INVALID_REQUEST):
                status = 400
        self._send(status, out.encode("ascii"), "application/json", headers)
