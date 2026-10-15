"""Seeded, property-style fuzz case generator for JSON-RPC / MCP.

Property checked by callers (see tests/test_fuzz.py and the conformance suite):

    For every generated case the server (1) does not crash, (2) answers with valid
    JSON-RPC (or stays silent exactly when the oracle says the case is only
    notifications / client responses), (3) never answers -32603 (an internal error
    would be a bug), and (4) echoes ids verbatim.

``oracle_expected_responses`` is a small independent re-statement of the JSON-RPC rules,
deliberately not importing jsonrpc.classify, so the two implementations check each other.
"""

import json
import random

KNOWN_METHODS = ["initialize", "ping", "tools/list", "tools/call", "resources/list", "resources/read",
                 "resources/templates/list", "prompts/list", "prompts/get", "notifications/initialized",
                 "notifications/cancelled", "logging/setLevel", "completion/complete", "rpc.foo", ""]
TOOLS = ["memory_store", "memory_recall", "memory_list", "memory_forget", "nope"]
JUNK = ["", " ", "{", "}", "[", "]", "null", "true", "\"", "\\", "\u0000", "\u00e9\u00f1", "\U0001F600", "1e999", "-0",
        "{\"jsonrpc\":\"2.0\"}", "[[]]", "{\"a\":", "NaN", "Infinity", "0x10", "'single'", "\t", "/*c*/"]


def _rand_scalar(r):
    return r.choice([None, True, False, 0, 1, -1, 2 ** 70, -2 ** 70, 3.14, -0.0, 1e300, "", "x", "a" * r.randint(0, 300),
                     "\u00e9\u00f1\u00fc", "\U0001F600", "memory://index", "memory://entry/1", "memory_list",
                     "recall_and_summarize", "2025-03-26"])


def rand_value(r, depth=0):
    if depth > 3 or r.random() < 0.4:
        return _rand_scalar(r)
    if r.random() < 0.5:
        return [rand_value(r, depth + 1) for _ in range(r.randint(0, 4))]
    return {r.choice(["a", "name", "arguments", "uri", "id", "limit", "query", "content", "_meta", "cursor", "tags"]):
            rand_value(r, depth + 1) for _ in range(r.randint(0, 4))}


def _valid_templates(r):
    mid = r.choice([1, "s-1", 7, 2 ** 40])
    return [
        {"jsonrpc": "2.0", "id": mid, "method": "ping"},
        {"jsonrpc": "2.0", "id": mid, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": "memory_list", "arguments": {}}},
        {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": "memory_recall", "arguments": {"query": "x"}}},
        {"jsonrpc": "2.0", "id": mid, "method": "resources/read", "params": {"uri": "memory://index"}},
        {"jsonrpc": "2.0", "id": mid, "method": "prompts/get", "params": {"name": "memory_digest", "arguments": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": mid, "result": {}},
    ]


def _mutate(r, msg):
    msg = json.loads(json.dumps(msg))
    for _ in range(r.randint(1, 3)):
        op = r.choice(["delete", "retype", "add", "idtype"])
        keys = list(msg.keys())
        if op == "delete" and keys:
            del msg[r.choice(keys)]
        elif op == "retype" and keys:
            msg[r.choice(keys)] = rand_value(r)
        elif op == "add":
            msg[r.choice(["x", "jsonrpc", "params", "method", "id", "result", "error"])] = rand_value(r)
        elif op == "idtype":
            msg["id"] = r.choice([None, True, 1.5, [], {}, "", 0, -5, "é"])
    return msg


def gen_case(r):
    """Return one case as text (str). Newlines are removed so it is a single stdio frame."""
    kind = r.randint(0, 9)
    if kind == 0:
        text = "".join(r.choice(JUNK) for _ in range(r.randint(1, 6)))
    elif kind == 1:
        full = json.dumps(r.choice(_valid_templates(r)))
        text = full[: r.randint(0, len(full) - 1)]
    elif kind == 2:
        text = json.dumps(rand_value(r))
    elif kind == 3:
        text = json.dumps(_mutate(r, r.choice(_valid_templates(r))))
    elif kind == 4:
        text = json.dumps([_mutate(r, r.choice(_valid_templates(r))) if r.random() < 0.6 else rand_value(r)
                           for _ in range(r.randint(0, 6))])
    elif kind == 5:
        depth = r.choice([50, 500, 5000, 100000])
        text = "[" * depth + "]" * (depth if r.random() < 0.5 else 0)
    elif kind == 6:
        text = json.dumps({"jsonrpc": "2.0", "id": r.choice([1, "a", 2 ** 63]), "method": r.choice(KNOWN_METHODS),
                           "params": rand_value(r)})
    elif kind == 7:
        text = json.dumps({"jsonrpc": "2.0", "id": r.randint(1, 9), "method": "tools/call",
                           "params": {"name": r.choice(TOOLS), "arguments": rand_value(r)}})
    elif kind == 8:
        text = json.dumps({"jsonrpc": "2.0", "id": r.randint(1, 9), "method": r.choice(["resources/read", "prompts/get"]),
                           "params": rand_value(r)})
    else:
        text = json.dumps(r.choice(_valid_templates(r)))
    return text.replace("\n", " ").replace("\r", " ")


def gen_cases(seed, n):
    r = random.Random(seed)
    return [gen_case(r) for _ in range(n)]


# ---- independent oracle --------------------------------------------------------
def _is_int_id(v):
    return isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool))


def _element_kind(el):
    """'req' (needs a response), 'silent' or 'bad' (needs -32600) -- independent of jsonrpc.classify."""
    if not isinstance(el, dict) or el.get("jsonrpc") != "2.0":
        return "bad"
    if "method" in el:
        m = el["method"]
        if not isinstance(m, str) or m == "" or "result" in el or "error" in el:
            return "bad"
        if "params" in el and not isinstance(el["params"], (dict, list)):
            return "bad"
        if "id" in el:
            return "req" if _is_int_id(el["id"]) else "bad"
        return "silent"
    has_r, has_e = "result" in el, "error" in el
    if has_r != has_e and "id" in el:
        if has_e:
            e = el["error"]
            if not (isinstance(e, dict) and isinstance(e.get("code"), int) and not isinstance(e.get("code"), bool)
                    and isinstance(e.get("message"), str)):
                return "bad"
            return "silent" if (el["id"] is None or _is_int_id(el["id"])) else "bad"
        return "silent" if _is_int_id(el["id"]) else "bad"
    return "bad"


def oracle_expected_responses(text):
    """Return ('parse_error', 1) | ('responses', n) for the given raw text."""
    try:
        def boom(c):
            raise ValueError(c)
        obj = json.loads(text, parse_constant=boom)
    except (ValueError, RecursionError):
        return "parse_error", 1
    if isinstance(obj, list):
        if not obj:
            return "responses", 1
        return "responses", sum(1 for el in obj if _element_kind(el) != "silent")
    return "responses", 0 if _element_kind(obj) == "silent" else 1


ALLOWED_ERROR_CODES = {-32700, -32600, -32601, -32602, -32001, -32002, -32003}


def check_output(case, out):
    """Return None if ``out`` (response text or None) satisfies the properties, else a failure string."""
    kind, want = oracle_expected_responses(case)
    if out is None:
        return None if want == 0 else "no response but %d expected" % want
    try:
        back = json.loads(out)
    except ValueError:
        return "response is not JSON: %r" % out[:80]
    items = back if isinstance(back, list) else [back]
    if isinstance(back, list) and not back:
        return "empty array response"
    if kind == "parse_error":
        if not (isinstance(back, dict) and back.get("error", {}).get("code") == -32700 and back.get("id") is None):
            return "expected single -32700 with null id"
        return None
    if want == 0:
        return "unexpected response to silent case"
    if len(items) != want:
        return "expected %d responses, got %d" % (want, len(items))
    for it in items:
        if not (isinstance(it, dict) and it.get("jsonrpc") == "2.0" and ("result" in it) != ("error" in it) and "id" in it):
            return "malformed response %r" % (it,)
        if "error" in it:
            e = it["error"]
            if e.get("code") not in ALLOWED_ERROR_CODES:
                return "disallowed error code %r (%s)" % (e.get("code"), e.get("message"))
            if not isinstance(e.get("message"), str):
                return "error without message"
    # id echo for single object cases
    if isinstance(back, dict):
        try:
            sent = json.loads(case)
        except (ValueError, RecursionError):
            sent = None
        if isinstance(sent, dict) and "id" in sent and _is_int_id(sent["id"]) and sent.get("jsonrpc") == "2.0" \
                and (type(back["id"]) is not type(sent["id"]) or back["id"] != sent["id"]):
            return "id not echoed: %r vs %r" % (sent["id"], back["id"])
    return None
