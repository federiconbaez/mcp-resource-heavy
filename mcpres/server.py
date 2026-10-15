"""MCP semantics on top of the JSON-RPC dispatcher: lifecycle, capability negotiation,
tools / resources / prompts, scope enforcement, audit logging."""

import json
import os
import secrets
import threading
import time
from urllib.parse import quote

from . import PREFERRED_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, __version__, contracts, schema
from .audit import AuditLog
from .auth import BASE_SCOPE, AuthError
from .catalog import Catalog, ToolError, json_text
from .hub import MAX_SUBSCRIPTIONS, Hub, SubscriptionLimit
from .jsonrpc import (INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, Dispatcher, RpcError, dumps, make_error)
from .memory import MemoryStore
from .paging import DEFAULT_PAGE_SIZE, CursorError, paginate
from .prompts import Completer, CompletionError, PromptBook, PromptError
from .resources import SCOPE_DATA, SCOPE_MEMORY, TEMPLATES, ResourceSpace, term_uri
from .userdata import UserData, UserDataError, normalise_term

# Implementation-defined error codes (JSON-RPC reserves -32000..-32099 for the server).
UNAUTHORIZED = -32001
RESOURCE_NOT_FOUND = -32002  # the code MCP specifies for "resource not found"
INSUFFICIENT_SCOPE = -32003

PRE_INIT_METHODS = ("initialize", "ping")

contracts.declare("MCP-1", "no non-error response is produced before a successful initialize, except for initialize/ping")
contracts.declare("MCP-2", "the negotiated protocolVersion in an initialize result is one this server supports")
contracts.declare("MCP-3", "every advertised capability has a routable <capability>/list method")
contracts.declare("MCP-4", "capabilities.resources.subscribe/listChanged are advertised only because the matching methods and notifications exist")
contracts.declare("PAGE-1", "a list page never exceeds the page size and its cursor is present only when more items remain")

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
SUBSCRIBE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["uri"], "properties": {
    "uri": {"type": "string", "minLength": 1, "maxLength": 2048}, "_meta": _META}}
COMPLETE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ref", "argument"], "properties": {
    "ref": {"type": "object", "required": ["type"], "properties": {
        "type": {"type": "string", "enum": ["ref/prompt", "ref/resource"]}, "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "uri": {"type": "string", "minLength": 1, "maxLength": 2048}}, "additionalProperties": False},
    "argument": {"type": "object", "additionalProperties": False, "required": ["name", "value"], "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 64}, "value": {"type": "string", "maxLength": 256}}},
    "context": {"type": "object", "additionalProperties": False, "properties": {"arguments": {"type": "object"}}},
    "_meta": _META}}
TOOLS_CALL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name"], "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 128}, "arguments": {"type": "object"}, "_meta": _META}}
RESOURCES_READ_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["uri"], "properties": {
    "uri": {"type": "string", "minLength": 1, "maxLength": 2048}, "_meta": _META}}
PROMPTS_GET_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name"], "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 128}, "arguments": {"type": "object"}, "_meta": _META}}


class McpServer:
    """State shared by all sessions of one server process."""

    def __init__(self, data_dir, authenticator=None, llm=None, audit_fsync=True, page_size=DEFAULT_PAGE_SIZE, clock=time.time):
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        self.data_dir = data_dir
        self.auth = authenticator  # None => authentication disabled (--no-auth)
        self.page_size = int(page_size)
        if not 1 <= self.page_size <= 500:
            raise ValueError("page_size must be 1..500")
        self.memory = MemoryStore(os.path.join(data_dir, "memory.sqlite3"))
        self.user = UserData(os.path.join(data_dir, "userdata.sqlite3"), clock=clock)
        self.audit = AuditLog(os.path.join(data_dir, "audit.log"), fsync=audit_fsync)
        self.space = ResourceSpace(self.memory, self.user, self.audit_view, clock=clock)
        self.catalog = Catalog(self.memory, self.user, llm)
        self.book = PromptBook(self.space, self.memory)
        self.completer = Completer(self.book, self.space, self.memory)
        self.hub = Hub()

    def new_session(self, transport, token=None):
        return Session(self, transport, token)

    def log_event(self, kind, detail, transport="", session="", sub=""):
        self.audit.append("event", dumps({"event": kind, **detail}), transport, session, sub)

    def audit_view(self):
        tail = self.audit.tail(8)
        for e in tail:
            if isinstance(e.get("payload"), str) and len(e["payload"]) > 160:
                e["payload"] = e["payload"][:160] + "...(truncated)"
        return {"chain": self.audit.verify(), "contracts": contracts.snapshot(), "invariants": contracts.REGISTRY, "tail": tail}


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
        self.outbox = None  # set at initialize: the Hub queue that SSE / the stdio loop drain
        self.stream_lock = threading.Lock()  # HTTP: at most one GET /mcp stream per session
        self.dispatcher = Dispatcher(self._handle, on_event=self._on_event)
        self._methods = {
            "ping": self._ping,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "resources/list": self._resources_list,
            "resources/read": self._resources_read,
            "resources/templates/list": self._templates_list,
            "resources/subscribe": self._subscribe,
            "resources/unsubscribe": self._unsubscribe,
            "completion/complete": self._complete,
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

    def _page(self, req, kind, items, key):
        params = self._params(req, LIST_SCHEMA)
        size = self.server.page_size
        try:
            page, nxt = paginate(items, key, params.get("cursor"), size, kind)
        except CursorError as exc:
            raise RpcError(INVALID_PARAMS, "Invalid cursor", {"errors": [str(exc)]})
        contracts.check("PAGE-1", len(page) <= size and (nxt is None or len(page) == size), "page size / cursor invariant")
        return page, nxt

    @staticmethod
    def _with_cursor(result, nxt):
        if nxt is not None:
            result["nextCursor"] = nxt
        return result

    # ---- notification plumbing -------------------------------------------------------
    def drain_notifications(self):
        """Notifications that may be sent now: none until the client has sent notifications/initialized."""
        if self.outbox is None or not self.ready:
            return []
        return self.outbox.pop_all()

    def close(self):
        if self.outbox is not None:
            self.server.hub.detach(self.id)
            self.outbox = None

    def _publish(self, updated=(), list_changed=False):
        hub = self.server.hub
        hit = []
        for uri in updated:
            hit += hub.publish_updated(uri)
        if list_changed:
            hit += hub.publish_list_changed()
        if updated or list_changed:
            self.server.log_event("resource_change", {"updated": list(updated), "list_changed": bool(list_changed), "notified": len(set(hit))},
                                  self.transport, self.id, self.subject)

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
            "resources": {"subscribe": True, "listChanged": True},
            "prompts": {"listChanged": False},
            "experimental": {"mcpres/auditChain": {"algorithm": "sha256", "resource": "audit://chain"}},
        }
        if chosen != "2024-11-05":  # the `completions` capability object exists from 2025-03-26 on
            capabilities["completions"] = {}
        result = {
            "protocolVersion": chosen,
            "capabilities": capabilities,
            "serverInfo": {"name": "mcp-resource-heavy", "title": "MCP Resource-Heavy (stdlib-only)", "version": __version__},
            "instructions": "Resource-centric server: static/templated/dynamic/binary resources (country://, element://, unit://, glossary://, blob://, dyn://, memory://), "
                            "subscriptions (resources/subscribe; counter_bump and glossary_add trigger updates), cursor pagination on every list, "
                            "completion/complete for prompt and template arguments, and prompts that embed resources.",
        }
        contracts.check("MCP-2", chosen in SUPPORTED_PROTOCOL_VERSIONS, "unsupported version negotiated")
        for cap in ("tools", "resources", "prompts"):
            contracts.check("MCP-3", cap + "/list" in self._methods, lambda: "capability %s lacks list method" % cap)
        contracts.check("MCP-4", capabilities["resources"]["subscribe"] is True and "resources/subscribe" in self._methods
                        and "resources/unsubscribe" in self._methods and capabilities["resources"]["listChanged"] is True,
                        "advertised resource capability without implementation")
        self.initialized = True
        self.outbox = self.server.hub.attach(self.id)
        self.protocol_version = chosen
        self.client_info = params["clientInfo"]
        return result

    def _ping(self, req):
        return {}

    def _tools_list(self, req):
        page, nxt = self._page(req, "tools", list(self.server.catalog.tools.values()), lambda t: t.name)
        return self._with_cursor({"tools": [t.descriptor(self.protocol_version) for t in page]}, nxt)

    def _tools_call(self, req):
        params = self._params(req, TOOLS_CALL_SCHEMA)
        tool = self.server.catalog.tools.get(params["name"])
        if tool is None:
            raise RpcError(INVALID_PARAMS, "Unknown tool: %s" % params["name"], {"errors": ["unknown tool"]})
        self._require_scope(tool.scope)
        ctx = {"sub": self.subject, "publish": self._publish, "read_resource": self._read_item}
        try:
            out = self.server.catalog.call_tool(tool, params.get("arguments", {}), ctx)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        links = out.pop("_links", [])
        result = {"content": [{"type": "text", "text": json_text(out)}], "isError": False}
        if self.protocol_version == "2025-06-18":
            result["structuredContent"] = out
            for uri in links[:10]:
                found = self.server.space.classify(uri)
                result["content"].append({"type": "resource_link", "uri": uri, "name": uri.split("://", 1)[1][:64],
                                          "mimeType": "application/json" if found and found[0] != "blob" else "application/octet-stream"})
        return result

    def _resources_list(self, req):
        items = self.server.space.list_all(self._has_scope)
        page, nxt = self._page(req, "resources", items, lambda r: r["uri"])
        return self._with_cursor({"resources": page}, nxt)

    def _templates_list(self, req):
        allowed = [t for t in TEMPLATES if self._has_scope(SCOPE_MEMORY if t["uriTemplate"].startswith("memory://") else SCOPE_DATA)]
        page, nxt = self._page(req, "templates", allowed, lambda t: t["uriTemplate"])
        return self._with_cursor({"resourceTemplates": page}, nxt)

    def _read_item(self, uri):
        """Read one resource with scope enforcement; returns the contents item. Raises RpcError."""
        space = self.server.space
        found = space.classify(uri)
        if found is None:
            raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})
        kind, scope, arg = found
        self._require_scope(scope)
        item = space.read(kind, arg, uri)
        if item is None:
            raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})
        return item

    def _resources_read(self, req):
        params = self._params(req, RESOURCES_READ_SCHEMA)
        return {"contents": [self._read_item(params["uri"])]}

    def _canonical(self, uri):
        """Subscriptions are keyed by canonical URIs so that an update for X reaches everyone subscribed to X."""
        kind, _, arg = self.server.space.classify(uri)
        space = self.server.space
        if kind == "country":
            return "country://" + arg
        if kind == "element":
            e = space.read(kind, arg, uri)
            return "element://" + json.loads(e["text"])["symbol"]
        if kind == "term":
            try:
                return term_uri(normalise_term(arg))
            except UserDataError:
                return uri
        if kind == "entry":
            return "memory://entry/%d" % arg
        if kind == "tag":
            return "memory://tag/" + quote(arg, safe="")
        return uri

    def _subscribe(self, req):
        params = self._params(req, SUBSCRIBE_SCHEMA)
        uri = params["uri"]
        self._read_item(uri)  # must exist and be readable by this token (-32002 / -32003)
        canon = self._canonical(uri)
        if canon != uri:
            raise RpcError(INVALID_PARAMS, "Subscribe to the canonical URI", {"errors": ["uri is not canonical"], "canonical": canon})
        try:
            self.server.hub.subscribe(self.id, uri)
        except SubscriptionLimit as exc:
            raise RpcError(INVALID_PARAMS, "Too many subscriptions", {"errors": [str(exc)], "max": MAX_SUBSCRIPTIONS})
        self.server.log_event("subscribe", {"uri": uri}, self.transport, self.id, self.subject)
        return {}

    def _unsubscribe(self, req):
        params = self._params(req, SUBSCRIBE_SCHEMA)
        found = self.server.space.classify(params["uri"])
        if found is None:
            raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": params["uri"]})
        self._require_scope(found[1])
        self.server.hub.unsubscribe(self.id, params["uri"])  # idempotent
        self.server.log_event("unsubscribe", {"uri": params["uri"]}, self.transport, self.id, self.subject)
        return {}

    def _complete(self, req):
        params = self._params(req, COMPLETE_SCHEMA)
        ref = params["ref"]
        if ref["type"] == "ref/prompt":
            if "name" not in ref or "uri" in ref:
                raise RpcError(INVALID_PARAMS, "Invalid params", {"errors": ["ref/prompt needs exactly `name`"]})
            if ref["name"] in self.server.book.specs:
                self._require_scope(self.server.book.scope_of(ref["name"]))
        else:
            if "uri" not in ref or "name" in ref:
                raise RpcError(INVALID_PARAMS, "Invalid params", {"errors": ["ref/resource needs exactly `uri`"]})
            self._require_scope(SCOPE_MEMORY if ref["uri"].startswith("memory://") else SCOPE_DATA)
        try:
            return {"completion": self.server.completer.complete(ref, params["argument"], params.get("context"))}
        except CompletionError as exc:
            raise RpcError(INVALID_PARAMS, "Invalid completion request", {"errors": [str(exc)]})

    def _prompts_list(self, req):
        items = self.server.book.descriptors(self._has_scope)
        page, nxt = self._page(req, "prompts", items, lambda p: p["name"])
        return self._with_cursor({"prompts": page}, nxt)

    def _prompts_get(self, req):
        params = self._params(req, PROMPTS_GET_SCHEMA)
        name = params["name"]
        book = self.server.book
        if name not in book.specs:
            raise RpcError(INVALID_PARAMS, "Unknown prompt: %s" % name, {"errors": ["unknown prompt"]})
        self._require_scope(book.scope_of(name))
        args = params.get("arguments", {})
        try:
            return book.build(name, args)
        except PromptError as exc:
            raise RpcError(INVALID_PARAMS, "Invalid prompt arguments", {"errors": str(exc).split("; ")})
