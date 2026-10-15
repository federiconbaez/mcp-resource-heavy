"""JSON-RPC 2.0, written by hand (https://www.jsonrpc.org/specification).

Responsibilities of this module (and *only* these - MCP semantics live in
``server.py``):

* parse text -> value (-32700 on malformed JSON, NaN/Infinity, or absurd nesting)
* classify each message: request / notification / client-response / invalid
* strict id validation (string or integer; booleans, floats, null are rejected)
* batch handling (empty batch is a single -32600; notifications yield nothing;
  an all-notification batch yields no response at all)
* turn handler exceptions into -32602 / -32603 etc. and *never* let one escape
* runtime contracts on the "exactly one response per request, none per
  notification, ids echoed verbatim" invariants (see contracts.py)
"""

import json
from collections import namedtuple

from . import contracts

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

STANDARD_MESSAGES = {
    PARSE_ERROR: "Parse error",
    INVALID_REQUEST: "Invalid Request",
    METHOD_NOT_FOUND: "Method not found",
    INVALID_PARAMS: "Invalid params",
    INTERNAL_ERROR: "Internal error",
}

REQUEST, NOTIFICATION, RESPONSE, INVALID = "request", "notification", "response", "invalid"

Request = namedtuple("Request", "method params id is_notification in_batch")

INV_ONE = contracts.declare(
    "RPC-1", "each request object yields exactly one response object with the request id echoed verbatim; "
             "each notification and each client response yields none; each invalid element yields one -32600 error")
INV_BATCH = contracts.declare(
    "RPC-2", "a batch yields no response iff every element was a notification/response, otherwise an array with "
             "exactly one response per request/invalid element; an empty batch yields a single -32600 object")
INV_TEXT = contracts.declare(
    "RPC-3", "handle_text returns None or a string holding valid JSON of a response object/array (never raises)")


class RpcError(Exception):
    """Raise from a handler to produce a JSON-RPC error response."""

    def __init__(self, code, message=None, data=None):
        super().__init__(message or STANDARD_MESSAGES.get(code, "Error"))
        self.code = code
        self.message = message or STANDARD_MESSAGES.get(code, "Error")
        self.data = data


def make_result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def make_error(msg_id, code, message=None, data=None):
    err = {"code": code, "message": message or STANDARD_MESSAGES.get(code, "Error")}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def dumps(obj):
    """Compact, ASCII-only, single-line JSON (safe for newline-delimited framing)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _reject_constant(name):
    raise ValueError("non-standard JSON constant %s" % name)


def parse_text(text):
    """Parse JSON text or raise RpcError(-32700)."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except RecursionError:
        raise RpcError(PARSE_ERROR, "Parse error", {"reason": "nesting too deep"})
    except (ValueError, TypeError) as exc:
        raise RpcError(PARSE_ERROR, "Parse error", {"reason": str(exc)[:120]})


def valid_id(value):
    """JSON-RPC allows string/number/null; MCP narrows to string or integer, never null."""
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def same_id(a, b):
    return type(a) is type(b) and a == b


def classify(obj):
    """Return (kind, id_for_error_reply, reason)."""
    if not isinstance(obj, dict):
        return INVALID, None, "message is not an object"
    has_id = "id" in obj
    raw_id = obj.get("id")
    echo = raw_id if has_id and valid_id(raw_id) else None
    if obj.get("jsonrpc") != "2.0":
        return INVALID, echo, "jsonrpc member must be exactly \"2.0\""
    if "method" in obj:
        if "result" in obj or "error" in obj:
            return INVALID, echo, "request must not contain result/error"
        if not isinstance(obj["method"], str) or not obj["method"]:
            return INVALID, echo, "method must be a non-empty string"
        if "params" in obj and not isinstance(obj["params"], (dict, list)):
            return INVALID, echo, "params must be an object or array"
        if has_id and not valid_id(raw_id):
            return INVALID, None, "id must be a string or an integer"
        return (REQUEST if has_id else NOTIFICATION), echo, ""
    # no method: only legal as a client->server *response*
    if ("result" in obj) != ("error" in obj):
        if "error" in obj:
            e = obj["error"]
            if not (isinstance(e, dict) and isinstance(e.get("code"), int)
                    and not isinstance(e.get("code"), bool) and isinstance(e.get("message"), str)):
                return INVALID, echo, "malformed error object"
        if has_id and (valid_id(raw_id) or (raw_id is None and "error" in obj)):
            return RESPONSE, echo, ""
    return INVALID, echo, "no method member and not a well-formed response"


def _post_one(args, kwargs, result):
    obj = args[1]
    kind, rid, _ = classify(obj)
    if kind in (NOTIFICATION, RESPONSE):
        return True if result is None else "%s produced a response" % kind
    if not isinstance(result, dict) or result.get("jsonrpc") != "2.0":
        return "response is not a JSON-RPC 2.0 object"
    if not same_id(result.get("id"), rid):
        return "id not echoed: sent %r got %r" % (rid, result.get("id"))
    if ("result" in result) == ("error" in result):
        return "response must contain exactly one of result/error"
    if kind == INVALID:
        if "error" not in result or result["error"].get("code") != INVALID_REQUEST:
            return "invalid element must yield -32600"
    return True


def _expected_count(batch):
    return sum(1 for el in batch if classify(el)[0] in (REQUEST, INVALID))


def _post_parsed(args, kwargs, result):
    obj = args[1]
    if isinstance(obj, list):
        if not obj:
            ok = isinstance(result, dict) and result.get("error", {}).get("code") == INVALID_REQUEST
            return True if ok else "empty batch must yield one -32600 object"
        want = _expected_count(obj)
        if want == 0:
            return True if result is None else "all-notification batch must yield nothing"
        if not isinstance(result, list) or len(result) != want:
            return "batch cardinality: want %d responses" % want
        return True
    return True  # single messages are covered by RPC-1


def _post_text(args, kwargs, result):
    if result is None:
        return True
    if not isinstance(result, str) or "\n" in result:
        return "response must be a single-line string"
    try:
        back = json.loads(result)
    except ValueError:
        return "response is not valid JSON"
    items = back if isinstance(back, list) else [back]
    if not items:
        return "empty response array"
    for it in items:
        if not (isinstance(it, dict) and it.get("jsonrpc") == "2.0" and ("result" in it) != ("error" in it) and "id" in it):
            return "malformed response member"
    return True


class Dispatcher:
    """Routes parsed JSON-RPC messages to ``handler(Request) -> result``.

    ``handler`` may raise ``RpcError``. Any other exception becomes -32603 with
    a generic message (details go to ``on_event`` only, never to the client).
    ``on_event(kind, detail_dict)`` is an optional observer (audit/logging).
    """

    def __init__(self, handler, on_event=None):
        self.handler = handler
        self.on_event = on_event or (lambda kind, detail: None)

    # -- public entry points -------------------------------------------------
    @contracts.ensures(INV_TEXT, _post_text)
    def handle_text(self, text):
        try:
            obj = parse_text(text)
        except RpcError as exc:
            return dumps(make_error(None, exc.code, exc.message, exc.data))
        out = self.handle_parsed(obj)
        if out is None:
            return None
        try:
            return dumps(out)
        except (ValueError, TypeError, RecursionError) as exc:
            self.on_event("serialization_error", {"error": repr(exc)})
            return dumps(make_error(None, INTERNAL_ERROR, "Internal error"))

    @contracts.ensures(INV_BATCH, _post_parsed)
    def handle_parsed(self, obj):
        if isinstance(obj, list):
            if not obj:
                return make_error(None, INVALID_REQUEST, "Invalid Request", {"reason": "empty batch"})
            responses = []
            for el in obj:
                r = self._guarded_one(el, True)
                if r is not None:
                    responses.append(r)
            return responses or None
        return self._guarded_one(obj, False)

    # -- internals -----------------------------------------------------------
    def _guarded_one(self, obj, in_batch):
        try:
            return self._one(obj, in_batch)
        except contracts.ContractViolation as cv:
            self.on_event("contract_violation", {"name": cv.name, "detail": cv.detail})
            kind, rid, _ = classify(obj)
            if kind in (NOTIFICATION, RESPONSE):
                return None
            return make_error(rid, INTERNAL_ERROR, "Internal error", {"reason": "contract violation", "contract": cv.name})

    @contracts.ensures(INV_ONE, _post_one)
    def _one(self, obj, in_batch):
        kind, rid, why = classify(obj)
        if kind == INVALID:
            return make_error(rid, INVALID_REQUEST, "Invalid Request", {"reason": why})
        if kind == RESPONSE:
            return None  # we never send server->client requests; stray responses are dropped
        is_note = kind == NOTIFICATION
        req = Request(obj["method"], obj.get("params"), rid, is_note, in_batch)
        try:
            result = self.handler(req)
        except RpcError as exc:
            return None if is_note else make_error(rid, exc.code, exc.message, exc.data)
        except contracts.ContractViolation:
            raise
        except Exception as exc:  # noqa: BLE001 - last line of defence, by design
            self.on_event("internal_error", {"method": req.method, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
            return None if is_note else make_error(rid, INTERNAL_ERROR, "Internal error")
        if is_note:
            return None
        return make_result(rid, result)
