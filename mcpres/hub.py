"""Subscription hub: who is subscribed to which resource URI, and per-session notification queues.

Pure in-memory and transport-agnostic, so it can be model-checked (``modelcheck.py``) and unit-tested without sockets.

Semantics (each is a runtime contract and a model-checked property):
  SUB-1  ``notifications/resources/updated`` for URI u is queued only for sessions subscribed to u at publish time
  SUB-2  a detached (closed) session has no subscriptions and no queued notifications, and receives nothing afterwards
  SUB-3  notifications are coalesced: at most one pending ``updated`` per (session, uri) and one pending ``list_changed``
  SUB-4  after ``unsubscribe`` a session receives no further ``updated`` for that URI (already queued ones are dropped)
  SUB-5  per-session subscriptions are bounded (MAX_SUBSCRIPTIONS), hence so is its queue
"""

import threading
from collections import deque

from . import contracts

MAX_SUBSCRIPTIONS = 100

contracts.declare("SUB-1", "an updated notification is queued only for sessions subscribed to that URI")
contracts.declare("SUB-2", "a detached session has no subscriptions and no pending notifications")
contracts.declare("SUB-3", "pending notifications are coalesced per (session, uri) / list_changed")
contracts.declare("SUB-5", "subscriptions per session never exceed MAX_SUBSCRIPTIONS")

UPDATED = "notifications/resources/updated"
LIST_CHANGED = "notifications/resources/list_changed"


class SubscriptionLimit(Exception):
    pass


class Outbox:
    """Thread-safe bounded notification queue for one session (what an SSE stream or the stdio loop drains)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._items = deque()
        self.closed = False

    def put_coalesced(self, message):
        key = (message["method"], (message.get("params") or {}).get("uri"))
        with self._cond:
            if self.closed:
                return False
            if any((m["method"], (m.get("params") or {}).get("uri")) == key for m in self._items):
                return False
            self._items.append(message)
            self._cond.notify_all()
            return True

    def drop_uri(self, uri):
        with self._cond:
            keep = [m for m in self._items if not (m["method"] == UPDATED and m["params"]["uri"] == uri)]
            self._items = deque(keep)

    def pop_all(self):
        with self._cond:
            items = list(self._items)
            self._items.clear()
            return items

    def wait(self, timeout):
        """Block until something is queued, the outbox closes, or timeout. Returns True if there is something to read."""
        with self._cond:
            if not self._items and not self.closed:
                self._cond.wait(timeout)
            return bool(self._items)

    def close(self):
        with self._cond:
            self.closed = True
            self._items.clear()
            self._cond.notify_all()

    def pending(self):
        with self._cond:
            return [(m["method"], (m.get("params") or {}).get("uri")) for m in self._items]


def updated_message(uri):
    return {"jsonrpc": "2.0", "method": UPDATED, "params": {"uri": uri}}


def list_changed_message():
    return {"jsonrpc": "2.0", "method": LIST_CHANGED}


class Hub:
    def __init__(self):
        self._lock = threading.RLock()
        self.sinks = {}   # session id -> Outbox
        self.subs = {}    # session id -> set(uri)
        self.published = {"updated": 0, "list_changed": 0}

    # ---- membership
    def attach(self, sid, outbox=None):
        with self._lock:
            self.sinks[sid] = outbox or Outbox()
            self.subs[sid] = set()
            return self.sinks[sid]

    def detach(self, sid):
        with self._lock:
            box = self.sinks.pop(sid, None)
            self.subs.pop(sid, None)
            if box is not None:
                box.close()
            contracts.check("SUB-2", sid not in self.subs and sid not in self.sinks, "detached session still registered")

    def attached(self, sid):
        with self._lock:
            return sid in self.sinks

    # ---- subscriptions
    def subscribe(self, sid, uri):
        with self._lock:
            if sid not in self.subs:
                raise KeyError(sid)
            if uri not in self.subs[sid] and len(self.subs[sid]) >= MAX_SUBSCRIPTIONS:
                raise SubscriptionLimit("at most %d subscriptions per session" % MAX_SUBSCRIPTIONS)
            self.subs[sid].add(uri)
            contracts.check("SUB-5", len(self.subs[sid]) <= MAX_SUBSCRIPTIONS, "subscription bound exceeded")

    def unsubscribe(self, sid, uri):
        with self._lock:
            if sid in self.subs:
                self.subs[sid].discard(uri)
                self.sinks[sid].drop_uri(uri)

    def subscriptions(self, sid):
        with self._lock:
            return sorted(self.subs.get(sid, ()))

    # ---- publishing
    def publish_updated(self, uri):
        """Queue ``updated`` for every subscriber of ``uri``. Returns the session ids that were newly notified."""
        with self._lock:
            self.published["updated"] += 1
            hit = []
            for sid, uris in self.subs.items():
                if uri in uris:
                    contracts.check("SUB-1", uri in self.subs[sid], "notifying a non-subscriber")
                    if self.sinks[sid].put_coalesced(updated_message(uri)):
                        hit.append(sid)
            self._check_invariants()
            return hit

    def publish_list_changed(self):
        with self._lock:
            self.published["list_changed"] += 1
            hit = [sid for sid, box in self.sinks.items() if box.put_coalesced(list_changed_message())]
            self._check_invariants()
            return hit

    def _check_invariants(self):
        for sid, box in self.sinks.items():
            pend = box.pending()
            contracts.check("SUB-3", len(pend) == len(set(pend)), lambda: "duplicate pending notifications %r" % (pend,))
            for method, uri in pend:
                if method == UPDATED:
                    contracts.check("SUB-1", uri in self.subs[sid], lambda: "pending update for unsubscribed uri %s" % uri)

    # ---- introspection / cloning (model checking)
    def state(self):
        with self._lock:
            return {"subs": {s: sorted(u) for s, u in sorted(self.subs.items())},
                    "pending": {s: b.pending() for s, b in sorted(self.sinks.items())}}
