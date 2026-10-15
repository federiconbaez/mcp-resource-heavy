"""Runtime contracts ("formal verification hooks").

Invariants about the dispatcher are expressed as named checks that run on every
request while contracts are enabled (default). A failed check raises
``ContractViolation``; the dispatcher turns that into a JSON-RPC -32603 error,
records a ``contract_violation`` event in the audit log and bumps a counter that
is visible through the ``audit://chain`` resource and the conformance suite.

This is *runtime assertion checking*, not a machine-checked proof. See
AUDIT.md / "Limitations".
"""

import functools
import os
import threading

_lock = threading.Lock()
STATS = {"checked": 0, "violated": 0, "by_name": {}}
REGISTRY = {}  # invariant name -> human description (documentation / conformance report)


class ContractViolation(AssertionError):
    def __init__(self, name, detail=""):
        super().__init__("contract %s violated%s" % (name, ": " + detail if detail else ""))
        self.name = name
        self.detail = detail


def enabled():
    return os.environ.get("MCP_FORGE_CONTRACTS", "1") != "0"


def declare(name, description):
    REGISTRY[name] = description
    return name


def check(name, condition, detail=""):
    """Assert a named invariant. ``detail`` may be a callable (evaluated lazily on failure)."""
    if not enabled():
        return
    with _lock:
        STATS["checked"] += 1
    if not condition:
        if callable(detail):
            detail = detail()
        with _lock:
            STATS["violated"] += 1
            STATS["by_name"][name] = STATS["by_name"].get(name, 0) + 1
        raise ContractViolation(name, str(detail))


def ensures(name, predicate):
    """Decorator: ``predicate(args, kwargs, result) -> bool | str`` must hold for the return value.

    A predicate returning a string signals failure with that string as detail.
    """

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            if enabled():
                verdict = predicate(args, kwargs, result)
                ok = verdict is True
                check(name, ok, lambda: verdict if isinstance(verdict, str) else "postcondition failed")
            return result

        return wrapper

    return deco


def requires(name, predicate):
    """Decorator: ``predicate(args, kwargs) -> bool | str`` must hold on entry."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if enabled():
                verdict = predicate(args, kwargs)
                check(name, verdict is True, lambda: verdict if isinstance(verdict, str) else "precondition failed")
            return fn(*args, **kwargs)

        return wrapper

    return deco


def snapshot():
    with _lock:
        return {"checked": STATS["checked"], "violated": STATS["violated"], "by_name": dict(STATS["by_name"])}


def restore(snap):
    """Reset counters to an earlier snapshot (used by tests that provoke violations on purpose)."""
    with _lock:
        STATS["checked"] = snap["checked"]
        STATS["violated"] = snap["violated"]
        STATS["by_name"] = dict(snap["by_name"])
