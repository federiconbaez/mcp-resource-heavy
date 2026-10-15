"""Bounded exhaustive model check of the subscription / notification state machine (formal-verification hook).

Two sessions (A, B) and two URIs (X, Y). Every operation sequence up to ``depth`` over this alphabet is executed on the REAL
``Hub`` and on a tiny independent reference model, and compared after every step (subscriptions, pending queues, what a drain returns):

    sub_AX sub_AY sub_BX      subscribe          unsub_AX unsub_AY unsub_BX    unsubscribe
    upd_X upd_Y               publish "updated"  lc                           publish "list_changed"
    drain_A drain_B           take the queue     close_A reopen_A              detach / attach session A

Properties established for all sequences within the bound (checked by comparison with the model AND by independent assertions):
    S1  updated(u) is queued only for subscribers of u                          (SUB-1)
    S2  a closed session has no subscriptions, no queue; later publishes never reach it       (SUB-2)
    S3  no duplicate pending notification per (session, uri) / list_changed                 (SUB-3)
    S4  after unsubscribe, nothing for that uri is pending or delivered later                (SUB-4)
    S5  subscribing on a detached session is an error and changes nothing
It is bounded model checking: it says nothing about depth beyond the bound or about more than two sessions/URIs.
"""

import copy

from .contracts import ContractViolation
from .hub import LIST_CHANGED, UPDATED, Hub, Outbox, list_changed_message, updated_message

ALPHABET = ("sub_AX", "sub_AY", "sub_BX", "unsub_AX", "unsub_AY", "unsub_BX", "upd_X", "upd_Y", "lc", "drain_A", "drain_B", "close_A", "reopen_A")
URIS = {"X": "res://x", "Y": "res://y"}


class Model:
    def __init__(self):
        self.attached = {"A", "B"}
        self.subs = {"A": set(), "B": set()}
        self.pending = {"A": [], "B": []}

    def apply(self, op):
        kind, _, rest = op.partition("_")
        if kind in ("sub", "unsub"):
            s, u = rest[0], URIS[rest[1]]
            if s not in self.attached:
                return "error"
            if kind == "sub":
                self.subs[s].add(u)
            else:
                self.subs[s].discard(u)
                self.pending[s] = [p for p in self.pending[s] if p != (UPDATED, u)]
            return "ok"
        if kind == "upd":
            u = URIS[rest]
            hit = []
            for s in sorted(self.attached):
                if u in self.subs[s] and (UPDATED, u) not in self.pending[s]:
                    self.pending[s].append((UPDATED, u))
                    hit.append(s)
            return hit
        if op == "lc":
            hit = []
            for s in sorted(self.attached):
                if (LIST_CHANGED, None) not in self.pending[s]:
                    self.pending[s].append((LIST_CHANGED, None))
                    hit.append(s)
            return hit
        if kind == "drain":
            if rest not in self.attached:
                return []
            out, self.pending[rest] = self.pending[rest], []
            return out
        if op == "close_A":
            self.attached.discard("A")
            self.subs["A"], self.pending["A"] = set(), []
            return "ok"
        if op == "reopen_A":
            self.attached.add("A")
            return "ok"
        raise AssertionError(op)

    def state(self):
        return {"subs": {s: sorted(self.subs[s]) for s in sorted(self.attached)}, "pending": {s: list(self.pending[s]) for s in sorted(self.attached)}}


def _fresh_hub():
    h = Hub()
    h.attach("A")
    h.attach("B")
    return h


def clone_hub(h):
    n = Hub()
    for sid in h.sinks:
        n.attach(sid)
        n.subs[sid] = set(h.subs[sid])
        for kind, uri in h.sinks[sid].pending():
            n.sinks[sid].put_coalesced(updated_message(uri) if kind == UPDATED else list_changed_message())
    return n


def apply_real(h, op):
    kind, _, rest = op.partition("_")
    try:
        if kind == "sub":
            h.subscribe(rest[0], URIS[rest[1]])
            return "ok"
        if kind == "unsub":
            if not h.attached(rest[0]):
                return "error"
            h.unsubscribe(rest[0], URIS[rest[1]])
            return "ok"
        if kind == "upd":
            return sorted(h.publish_updated(URIS[rest]))
        if op == "lc":
            return sorted(h.publish_list_changed())
        if kind == "drain":
            if not h.attached(rest):
                return []
            return [(m["method"], (m.get("params") or {}).get("uri")) for m in h.sinks[rest].pop_all()]
        if op == "close_A":
            h.detach("A")
            return "ok"
        if op == "reopen_A":
            if not h.attached("A"):
                h.attach("A")
            return "ok"
    except KeyError:
        return "error"
    raise AssertionError(op)


def _normal_state(h):
    st = h.state()
    return {"subs": {s: u for s, u in st["subs"].items()}, "pending": {s: list(p) for s, p in st["pending"].items()}}


def check(depth=4, alphabet=ALPHABET):
    stats = {"depth": depth, "alphabet": len(alphabet), "nodes": 0, "sequences": 0, "violations": []}

    def violate(trace, why):
        if len(stats["violations"]) < 5:
            stats["violations"].append({"trace": list(trace), "problem": why})

    def independent_asserts(h, trace):
        st = h.state()
        for s, pend in st["pending"].items():
            if len(pend) != len(set(pend)):
                violate(trace, "S3 duplicate pending in %s: %r" % (s, pend))
            for kind, uri in pend:
                if kind == UPDATED and uri not in st["subs"][s]:
                    violate(trace, "S1/S4 pending update for non-subscribed uri %s in %s" % (uri, s))
        for sid in ("A", "B"):
            if not h.attached(sid) and (sid in h.subs or sid in h.sinks):
                violate(trace, "S2 detached session %s still registered" % sid)

    def explore(h, m, level, trace):
        if level == depth:
            stats["sequences"] += 1
            return
        for op in alphabet:
            stats["nodes"] += 1
            h2, m2 = clone_hub(h), copy.deepcopy(m)
            t2 = trace + [op]
            try:
                r_real = apply_real(h2, op)
            except ContractViolation as exc:  # the runtime contracts inside Hub are a second, independent oracle
                violate(t2, "runtime contract fired: %s" % exc)
                continue
            r_model = m2.apply(op)
            if r_real != r_model:
                violate(t2, "result differs: real=%r model=%r" % (r_real, r_model))
            if _normal_state(h2) != m2.state():
                violate(t2, "state differs: real=%r model=%r" % (_normal_state(h2), m2.state()))
            independent_asserts(h2, t2)
            if not stats["violations"]:
                explore(h2, m2, level + 1, t2)

    explore(_fresh_hub(), Model(), 0, [])
    stats["ok"] = not stats["violations"]
    return stats
