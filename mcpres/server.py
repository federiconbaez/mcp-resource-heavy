"""MCP semantics on top of the JSON-RPC dispatcher: lifecycle, capability negotiation,
tools / resources / prompts, scope enforcement, audit logging."""

import json
import os
import secrets
import threading
import time

from . import PREFERRED_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, __version__, contracts, schema
from .audit import AuditLog
from .auth import BASE_SCOPE, AuthError
from .catalog import Catalog, ToolError, json_text
from .jsonrpc import (INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, Dispatcher, RpcError, dumps, make_error)
from .memory import MemoryStore

# Implementation-defined error codes (JSON-RPC reserves -32000..-32099 for the server).
UNAUTHORIZED = -32001
RESOURCE_NOT_FOUND = -32002  # the code MCP specifies for "resource not found"
INSUFFICIENT_SCOPE = -32003

PRE_INIT_METHODS = ("initialize", "ping")

contracts.declare("MCP-1", "no non-error response is produced before a successful initialize, except for initialize/ping")
contracts.declare("MCP-2", "the negotiated protocolVersion in an initialize result is one this server supports")
contracts.declare("MCP-3", "every advertised capability has a routable <capability>/list method")

_META = {"type": "object"}
INITIALIZE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["protocolVersion", "capabilities", "clientInfo"],
                     "properties": {
                         "protocolVersion": {"type": "string", "minLength": 1, "maxLength": 64},
                         "capabilities": {"type": "object"},
                         "clientInfo": {"type": "object", "required": ["name", "version"], "properties": {
                             "name": {"type": "string", "minLength": 1, "maxLength": 256},
                             "title": {"type": "string", "maxLength": 256},
                             "version": {"type": "string", "minLength": 1, "maxLength": 64}}},
                         "_meta": _META}}
LIST_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {"cursor": {"type": "string"}, "_meta": _META}}
TOOLS_CALL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name"], "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 128}, "arguments": {"type": "object"}, "_meta": _META}}
RESOURCES_READ_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["uri"], "properties": {
    "uri": {"type": "string", "minLength": 1, "maxLength": 2048}, "_meta": _META}}
PROMPTS_GET_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name"], "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 128}, "arguments": {"type": "object"}, "_meta": _META}}


class McpServer:
    """State shared by all sessions of one server process."""

    def __init__(self, data_dir, authenticator=None, llm=None, audit_fsync=True):
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        self.data_dir = data_dir
        self.auth = authenticator  # None => authentication disabled (--no-auth)
        self.memory = MemoryStore(os.path.join(data_dir, "memory.sqlite3"))
        self.audit = AuditLog(os.path.join(data_dir, "audit.log"), fsync=audit_fsync)
        self.catalog = Catalog(self.memory, llm)

    def new_session(self, transport, token=None):
        return Session(self, transport, token)

    def log_event(self, kind, detail, transport="", session="", sub=""):
        self.audit.append("event", dumps({"event": kind, **detail}), transport, session, sub)


class Session:
    def __init__(self, server, transport, token=None):
        self.server = server
        self.transport = transport
        self.id = secrets.token_hex(16)
        self.token = token  # stdio: raw token from $MCP_TOKEN, validated lazily at initialize
        self.claims = None  # HTTP: set by the transport before each dispatch
        self.initialized = False
        self.ready = False  # saw notifications/initialized
        self.protocol_version = None
        self.client_info = None
        self.created = self.last_used = time.time()
        self._lock = threading.RLock()
        self.dispatcher = Dispatcher(self._handle, on_event=self._on_event)
        self._methods = {
            "ping": self._ping,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "resources/list": self._resources_list,
            "resources/read": self._resources_read,
            "resources/templates/list": self._templates_list,
            "prompts/list": self._prompts_list,
            "prompts/get": self._prompts_get,
        }

    # ---- text entry points ------------------------------------------------------
    @property
    def subject(self):
        return (self.claims or {}).get("sub", "") if self.server.auth else "anonymous"

    def handle_bytes(self, raw):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._audit("request", raw.decode("utf-8", "replace"))
            out = dumps(make_error(None, -32700, "Parse error", {"reason": "invalid UTF-8"}))
            self._audit("response", out)
            return out
        return self.handle_text(text)

    def dispatch_http(self, raw, claims):
        """HTTP entry: bind this request's verified claims, then dispatch (atomically per session)."""
        with self._lock:
            self.claims = claims
            return self.handle_bytes(raw)

    def handle_text(self, text):
        with self._lock:
            self.last_used = time.time()
            self._audit("request", text)
            out = self.dispatcher.handle_text(text)
            if out is not None:
                self._audit("response", out)
            return out

    def _audit(self, direction, text):
        self.server.audit.append(direction, text, self.transport, self.id, self.subject)

    def _on_event(self, kind, detail):
        self.server.log_event(kind, detail, self.transport, self.id, self.subject)

    # ---- the one handler the dispatcher calls -------------------------------------
    def _handle(self, req):
        was_initialized = self.initialized
        result = self._route(req, was_initialized)
        contracts.check("MCP-1", was_initialized or req.is_notification or req.method in PRE_INIT_METHODS,
                        lambda: "result for %r before initialize" % req.method)
        return result

    def _route(self, req, was_initialized):
        method = req.method
        if req.is_notification:
            if method == "notifications/initialized" and was_initialized:
                self.ready = True
            return None  # cancelled, unknown etc.: notifications never get a reply
        if method == "initialize":
            return self._initialize(req)
        if method not in PRE_INIT_METHODS and not was_initialized:
            raise RpcError(INVALID_REQUEST, "Server not initialized: send initialize first",
                           {"reason": "not_initialized"})
        if req.in_batch and self.protocol_version == "2025-06-18":
            raise RpcError(INVALID_REQUEST, "JSON-RPC batching is not allowed in protocol 2025-06-18",
                           {"reason": "batch_not_supported"})
        if was_initialized:
            self._check_token_still_valid()
        fn = self._methods.get(method)
        if fn is None:
            raise RpcError(METHOD_NOT_FOUND, "Method not found", {"method": method})
        return fn(req)

    # ---- auth helpers --------------------------------------------------------------
    def _has_scope(self, scope):
        if self.server.auth is None:
            return True
        return bool(self.claims) and scope in self.claims.get("_scopes", ())

    def _require_scope(self, scope):
        if not self._has_scope(scope):
            raise RpcError(INSUFFICIENT_SCOPE, "Insufficient scope", {
                "required": [scope], "granted": (self.claims or {}).get("_scopes", [])})

    def _check_token_still_valid(self):
        auth = self.server.auth
        if auth is not None and self.claims is not None and not auth.still_valid(self.claims):
            raise RpcError(UNAUTHORIZED, "Unauthorized: token expired", {"error": "invalid_token"})

    def _authenticate_at_initialize(self):
        auth = self.server.auth
        if auth is None:
            return
        if self.claims is None:
            if not self.token:
                raise RpcError(UNAUTHORIZED, "Unauthorized: no token provided (stdio: set MCP_TOKEN)",
                               {"error": "invalid_request"})
            try:
                self.claims = auth.verify(self.token, [BASE_SCOPE])
            except AuthError as exc:
                code = INSUFFICIENT_SCOPE if exc.error == "insufficient_scope" else UNAUTHORIZED
                self.server.log_event("auth_failure", {"error": exc.error, "reason": exc.description}, self.transport, self.id)
                raise RpcError(code, "Unauthorized: " + exc.description, {"error": exc.error})
        self._require_scope(BASE_SCOPE)

    # ---- params ------------------------------------------------------------------------
    @staticmethod
    def _params(req, spec):
        params = {} if req.params is None else req.params
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "Invalid params", {"errors": ["params must be an object"]})
        problems = schema.validate(params, spec)
        if problems:
            raise RpcError(INVALID_PARAMS, "Invalid params", {"errors": [str(p) for p in problems]})
        return params

    def _list_params(self, req):
        params = self._params(req, LIST_SCHEMA)
        if "cursor" in params:
            raise RpcError(INVALID_PARAMS, "Invalid cursor", {"errors": ["this server never issues cursors"]})

    # ---- methods ----------------------------------------------------------------------------
    def _initialize(self, req):
        if req.in_batch:
            raise RpcError(INVALID_REQUEST, "initialize must not be part of a JSON-RPC batch", {"reason": "initialize_in_batch"})
        if self.initialized:
            raise RpcError(INVALID_REQUEST, "Already initialized", {"reason": "already_initialized"})
        params = self._params(req, INITIALIZE_SCHEMA)
        self._authenticate_at_initialize()
        requested = params["protocolVersion"]
        chosen = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PREFERRED_PROTOCOL_VERSION
        capabilities = {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
            "experimental": {"mcpres/auditChain": {"algorithm": "sha256", "resource": "audit://chain"}},
        }
        result = {
            "protocolVersion": chosen,
            "capabilities": capabilities,
            "serverInfo": {"name": "mcp-resource-heavy", "title": "MCP Forge (stdlib-only reference server)", "version": __version__},
            "instructions": "Persistent memory server. Use memory_store / memory_recall / memory_list / memory_forget; "
                            "read memory://index and audit://chain; prompts: recall_and_summarize, memory_digest.",
        }
        contracts.check("MCP-2", chosen in SUPPORTED_PROTOCOL_VERSIONS, "unsupported version negotiated")
        for cap in ("tools", "resources", "prompts"):
            contracts.check("MCP-3", cap + "/list" in self._methods, lambda: "capability %s lacks list method" % cap)
        self.initialized = True
        self.protocol_version = chosen
        self.client_info = params["clientInfo"]
        return result

    def _ping(self, req):
        return {}

    def _tools_list(self, req):
        self._list_params(req)
        return {"tools": [t.descriptor(self.protocol_version) for t in self.server.catalog.tools.values()]}

    def _tools_call(self, req):
        params = self._params(req, TOOLS_CALL_SCHEMA)
        tool = self.server.catalog.tools.get(params["name"])
        if tool is None:
            raise RpcError(INVALID_PARAMS, "Unknown tool: %s" % params["name"], {"errors": ["unknown tool"]})
        self._require_scope(tool.scope)
        try:
            out = self.server.catalog.call_tool(tool, params.get("arguments", {}), {"sub": self.subject})
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        result = {"content": [{"type": "text", "text": json_text(out)}], "isError": False}
        if self.protocol_version == "2025-06-18":
            result["structuredContent"] = out
        return result

    def _resources_list(self, req):
        self._list_params(req)
        return {"resources": self.server.catalog.list_resources(
            self._has_scope("memory:read"), self._has_scope("audit:read"))}

    def _templates_list(self, req):
        self._list_params(req)
        return {"resourceTemplates": self.server.catalog.TEMPLATES}

    def _resources_read(self, req):
        params = self._params(req, RESOURCES_READ_SCHEMA)
        uri = params["uri"]
        cat = self.server.catalog
        found = cat.classify_uri(uri)
        if found is None:
            raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})
        kind, scope, arg = found
        self._require_scope(scope)
        if kind == "index":
            body = cat.memory.index()
        elif kind == "entry":
            body = cat.memory.get(arg)
            if body is None:
                raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})
        elif kind == "tag":
            _, entries = cat.memory.list(limit=50, tag=arg)
            body = {"tag": arg, "entries": entries}
        else:
            body = self._audit_snapshot()
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json_text(body)}]}

    def _audit_snapshot(self):
        tail = self.server.audit.tail(8)
        for e in tail:
            if isinstance(e.get("payload"), str) and len(e["payload"]) > 160:
                e["payload"] = e["payload"][:160] + "...(truncated)"
        return {"chain": self.server.audit.verify(), "contracts": contracts.snapshot(),
                "invariants": contracts.REGISTRY, "tail": tail}

    def _prompts_list(self, req):
        self._list_params(req)
        return {"prompts": self.server.catalog.prompt_descriptors()}

    def _prompts_get(self, req):
        params = self._params(req, PROMPTS_GET_SCHEMA)
        name = params["name"]
        if name not in self.server.catalog.PROMPTS:
            raise RpcError(INVALID_PARAMS, "Unknown prompt: %s" % name, {"errors": ["unknown prompt"]})
        self._require_scope("memory:read")
        try:
            return self.server.catalog.build_prompt(name, params.get("arguments", {}))
        except ToolError as exc:
            raise RpcError(INVALID_PARAMS, "Invalid prompt arguments", {"errors": str(exc).split("; ")})
