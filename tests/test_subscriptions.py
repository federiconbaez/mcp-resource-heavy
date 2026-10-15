"""Subscriptions and list_changed notifications: hub unit tests, in-process sessions, real stdio subprocess, real HTTP SSE stream."""
import http.client
import json
import threading
import time
import unittest

from mcpres import contracts, modelcheck
from mcpres.hub import MAX_SUBSCRIPTIONS, Hub, SubscriptionLimit
from tests import helpers
from tests.helpers import InProc, StdioServer, TempDirTestCase, init_msg, make_server, rpc, start_http, token


class HubUnit(unittest.TestCase):
    def test_basic_delivery_and_coalescing(self):
        h = Hub()
        h.attach("a")
        h.attach("b")
        h.subscribe("a", "u1")
        self.assertEqual(h.publish_updated("u1"), ["a"])
        self.assertEqual(h.publish_updated("u1"), [], "coalesced while one is pending")
        self.assertEqual(h.publish_updated("other"), [])
        self.assertEqual([m["params"]["uri"] for m in h.sinks["a"].pop_all()], ["u1"])
        self.assertEqual(h.publish_updated("u1"), ["a"], "after draining a new one is queued")
        self.assertEqual(h.sinks["b"].pop_all(), [])

    def test_list_changed_goes_to_everyone_once(self):
        h = Hub()
        for s in "abc":
            h.attach(s)
        self.assertEqual(sorted(h.publish_list_changed()), ["a", "b", "c"])
        self.assertEqual(h.publish_list_changed(), [])
        self.assertEqual(h.sinks["b"].pop_all(), [{"jsonrpc": "2.0", "method": "notifications/resources/list_changed"}])

    def test_unsubscribe_drops_pending(self):
        h = Hub()
        h.attach("a")
        h.subscribe("a", "u")
        h.publish_updated("u")
        h.unsubscribe("a", "u")
        self.assertEqual(h.sinks["a"].pop_all(), [])
        self.assertEqual(h.publish_updated("u"), [])
        h.unsubscribe("a", "u")  # idempotent
        h.unsubscribe("ghost", "u")

    def test_detach_clears_everything_and_unblocks_waiters(self):
        h = Hub()
        box = h.attach("a")
        h.subscribe("a", "u")
        woke = []
        t = threading.Thread(target=lambda: woke.append(box.wait(5)))
        t.start()
        time.sleep(0.05)
        h.detach("a")
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(woke, [False])
        self.assertEqual(h.state(), {"subs": {}, "pending": {}})
        self.assertEqual(h.publish_updated("u"), [])
        with self.assertRaises(KeyError):
            h.subscribe("a", "u")

    def test_subscription_bound(self):
        h = Hub()
        h.attach("a")
        for i in range(MAX_SUBSCRIPTIONS):
            h.subscribe("a", "u%d" % i)
        h.subscribe("a", "u0")  # re-subscribing an existing uri is fine
        with self.assertRaises(SubscriptionLimit):
            h.subscribe("a", "one-too-many")
        self.assertLessEqual(len(h.sinks["a"].pending()), 1)

    def test_wait_wakes_on_publish(self):
        h = Hub()
        box = h.attach("a")
        h.subscribe("a", "u")
        got = []
        t = threading.Thread(target=lambda: got.append(box.wait(5)))
        t.start()
        time.sleep(0.05)
        t0 = time.time()
        h.publish_updated("u")
        t.join(2)
        self.assertEqual(got, [True])
        self.assertLess(time.time() - t0, 1.0)

    def test_concurrent_publishers_keep_invariants(self):
        h = Hub()
        for s in "ab":
            h.attach(s)
            h.subscribe(s, "u")
        before = contracts.snapshot()

        def spam():
            for _ in range(300):
                h.publish_updated("u")
                h.publish_list_changed()
                h.sinks["a"].pop_all()

        ts = [threading.Thread(target=spam) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(contracts.snapshot()["violated"], before["violated"])
        self.assertLessEqual(len(h.sinks["b"].pending()), 2)


class InProcessSubscriptions(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.data_dir, auth=False)

    def client(self, version="2025-03-26", ready=True):
        c = InProc(self.server.new_session("t"))
        c.session_obj = c.session
        c.send(init_msg(version))
        if ready:
            c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return c

    def notes(self, c):
        return [(n["method"].rsplit("/", 1)[1], (n.get("params") or {}).get("uri")) for n in c.session.drain_notifications()]

    def test_subscribe_bump_notify(self):
        c = self.client()
        self.assertEqual(c.call("resources/subscribe", {"uri": "dyn://counter"})["result"], {})
        self.assertEqual(self.notes(c), [])
        c.tool("counter_bump", {})
        c.tool("counter_bump", {})
        self.assertEqual(self.notes(c), [("updated", "dyn://counter")], "two bumps before a drain coalesce to one notification")
        c.call("resources/unsubscribe", {"uri": "dyn://counter"})
        c.tool("counter_bump", {})
        self.assertEqual(self.notes(c), [])

    def test_two_sessions_each_get_their_own_notifications(self):
        a, b = self.client(), self.client()
        a.call("resources/subscribe", {"uri": "dyn://counter"})
        b.call("resources/subscribe", {"uri": "dyn://counter"})
        b.call("resources/subscribe", {"uri": "glossary://user"})
        a.tool("counter_bump", {})
        self.assertEqual(self.notes(a), [("updated", "dyn://counter")])
        self.assertEqual(self.notes(b), [("updated", "dyn://counter")])
        b.tool("glossary_add", {"term": "alpha", "definition": "First."})
        self.assertEqual(self.notes(a), [("list_changed", None)])
        self.assertEqual(sorted(self.notes(b), key=str), [("list_changed", None), ("updated", "glossary://user")])

    def test_glossary_replace_updates_without_list_change_and_remove_changes_list(self):
        c = self.client()
        c.tool("glossary_add", {"term": "alpha", "definition": "First."})
        c.session.drain_notifications()
        for u in ("glossary://term/alpha", "glossary://all"):
            c.call("resources/subscribe", {"uri": u})
        c.tool("glossary_add", {"term": "alpha", "definition": "Second."})
        self.assertEqual(sorted(self.notes(c)), [("updated", "glossary://all"), ("updated", "glossary://term/alpha")], "replace: no list_changed")
        c.tool("glossary_remove", {"term": "alpha"})
        self.assertEqual(sorted(self.notes(c), key=str), [("list_changed", None), ("updated", "glossary://all"), ("updated", "glossary://term/alpha")])

    def test_memory_tools_notify(self):
        c = self.client()
        c.call("resources/subscribe", {"uri": "memory://index"})
        c.call("resources/subscribe", {"uri": "memory://tag/t1"})
        c.tool("memory_store", {"content": "x", "tags": ["t1"]})
        self.assertEqual(sorted(self.notes(c), key=str), [("list_changed", None), ("updated", "memory://index"), ("updated", "memory://tag/t1")])
        c.call("resources/subscribe", {"uri": "memory://entry/1"})
        c.tool("memory_forget", {"id": 1})
        self.assertIn(("updated", "memory://entry/1"), self.notes(c))

    def test_nothing_is_sent_before_initialized_notification(self):
        c = self.client(ready=False)
        c.call("resources/subscribe", {"uri": "dyn://counter"})
        c.tool("counter_bump", {})
        self.assertEqual(self.notes(c), [], "gated until notifications/initialized")
        c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(self.notes(c), [("updated", "dyn://counter")], "queued while gated, delivered once the client is ready")

    def test_subscribe_errors(self):
        c = self.client()
        self.assertEqual(c.call("resources/subscribe", {"uri": "country://ZZ"})["error"]["code"], -32002)
        self.assertEqual(c.call("resources/subscribe", {"uri": "nope://x"})["error"]["code"], -32002)
        self.assertEqual(c.call("resources/subscribe", {})["error"]["code"], -32602)
        self.assertEqual(c.call("resources/subscribe", {"uri": 5})["error"]["code"], -32602)
        r = c.call("resources/subscribe", {"uri": "country://ar"})
        self.assertEqual((r["error"]["code"], r["error"]["data"]["canonical"]), (-32602, "country://AR"))
        r = c.call("resources/subscribe", {"uri": "element://26"})
        self.assertEqual(r["error"]["data"]["canonical"], "element://Fe")
        r = c.call("resources/subscribe", {"uri": "glossary://term/URI"})
        self.assertEqual(r["error"]["data"]["canonical"], "glossary://term/uri")
        self.assertEqual(c.call("resources/unsubscribe", {"uri": "nope://x"})["error"]["code"], -32002)
        self.assertEqual(c.call("resources/unsubscribe", {"uri": "dyn://counter"})["result"], {}, "unsubscribing when not subscribed is a no-op")
        self.assertEqual(c.call("resources/subscribe", {"uri": "static://about"})["result"], {}, "static resources can be subscribed (they never change)")

    def test_subscription_limit_is_32602(self):
        c = self.client()
        uris = ["country://" + x["iso2"] for x in json.loads(c.call("resources/read", {"uri": "data://countries"})["result"]["contents"][0]["text"])["countries"]]
        uris += ["element://" + e["symbol"] for e in json.loads(c.call("resources/read", {"uri": "data://elements"})["result"]["contents"][0]["text"])["elements"]]
        uris += ["glossary://term/" + g["term"].replace(" ", "%20") for g in json.loads(c.call("resources/read", {"uri": "data://glossary"})["result"]["contents"][0]["text"])["glossary"]]
        self.assertGreater(len(uris), MAX_SUBSCRIPTIONS)
        codes = [c.call("resources/subscribe", {"uri": u}, mid=10 + i).get("error", {}).get("code") for i, u in enumerate(uris)]
        self.assertEqual(codes.count(None), MAX_SUBSCRIPTIONS)
        self.assertEqual(set(codes) - {None}, {-32602})

    def test_session_close_discards_state(self):
        c = self.client()
        c.call("resources/subscribe", {"uri": "dyn://counter"})
        sid = c.session.id
        c.session.close()
        self.assertFalse(self.server.hub.attached(sid))
        self.assertEqual(self.server.hub.state()["subs"], {})
        c.session.close()  # idempotent
        d = self.client()
        d.tool("counter_bump", {})
        self.assertEqual(self.notes(d), [])

    def test_capabilities_are_honest(self):
        """Every capability flag set to true is backed by a working method and a real notification."""
        c = InProc(self.server.new_session("t"))
        caps = c.send(init_msg("2025-06-18"))["result"]["capabilities"]
        c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertTrue(caps["resources"]["subscribe"] and caps["resources"]["listChanged"])
        self.assertEqual(c.call("resources/subscribe", {"uri": "dyn://counter"})["result"], {})
        c.tool("glossary_add", {"term": "honest", "definition": "True."})
        self.assertIn(("list_changed", None), self.notes(c))
        c.tool("counter_bump", {})
        self.assertIn(("updated", "dyn://counter"), self.notes(c))
        self.assertFalse(caps["tools"]["listChanged"])
        self.assertFalse(caps["prompts"]["listChanged"])
        before = {t["name"] for t in c.call("tools/list")["result"]["tools"]}
        c.tool("glossary_add", {"term": "more", "definition": "Still true."})
        self.assertEqual({t["name"] for t in c.call("tools/list")["result"]["tools"]}, before, "the tool list never changes, so listChanged=false is honest")


class StdioSubscriptions(TempDirTestCase):
    def test_notifications_follow_the_response_on_stdout(self):
        s = StdioServer(self.data_dir, token())
        try:
            init = s.call("initialize", init_msg("2025-03-26")["params"], mid=1)
            self.assertTrue(init["result"]["capabilities"]["resources"]["subscribe"])
            s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.assertEqual(s.call("resources/subscribe", {"uri": "dyn://counter"}, mid=2)["result"], {})
            s.send(rpc("tools/call", {"name": "counter_bump", "arguments": {"by": 3}}, mid=3))
            first, second = s.recv(), s.recv()
            self.assertEqual(first["id"], 3)
            self.assertEqual(second, {"jsonrpc": "2.0", "method": "notifications/resources/updated", "params": {"uri": "dyn://counter"}})
            s.send(rpc("tools/call", {"name": "glossary_add", "arguments": {"term": "stdio", "definition": "Standard streams."}}, mid=4))
            msgs = [s.recv(), s.recv()]
            self.assertEqual(msgs[0]["id"], 4)
            self.assertEqual(msgs[1]["method"], "notifications/resources/list_changed")
            read = s.call("resources/read", {"uri": "dyn://counter"}, mid=5)
            self.assertEqual(json.loads(read["result"]["contents"][0]["text"])["value"], 3)
            s.call("resources/unsubscribe", {"uri": "dyn://counter"}, mid=6)
            s.send(rpc("tools/call", {"name": "counter_bump", "arguments": {}}, mid=7))
            self.assertEqual(s.recv()["id"], 7)
            self.assertEqual(s.call("ping", mid=8)["id"], 8, "no stray notification precedes the next response")
        finally:
            code, err = s.close()
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(l)["event"] for l in err.splitlines()], ["ready"], "stderr carries only the startup event")


class HttpSse(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.data_dir, auth=True)
        self.t = start_http(self.server)
        self.addCleanup(self.t.shutdown)
        self.conns = []

    def tearDown(self):
        for c in self.conns:
            c.close()

    def conn(self):
        c = http.client.HTTPConnection("127.0.0.1", self.t.port, timeout=10)
        self.conns.append(c)
        return c

    def post(self, tok, obj, sid=None):
        c = self.conn()
        h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
        if sid:
            h["Mcp-Session-Id"] = sid
        c.request("POST", "/mcp", json.dumps(obj), h)
        r = c.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if data else None), r.getheader("Mcp-Session-Id")

    def session(self, tok, version="2025-03-26"):
        st, out, sid = self.post(tok, init_msg(version))
        self.assertEqual(st, 200, out)
        self.post(tok, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        return sid

    def open_stream(self, tok, sid, expect=200):
        c = self.conn()
        c.request("GET", "/mcp", headers={"Authorization": "Bearer " + tok, "Accept": "text/event-stream", "Mcp-Session-Id": sid})
        r = c.getresponse()
        self.assertEqual(r.status, expect)
        return r

    @staticmethod
    def read_event(resp):
        """Read one SSE event (skipping comments); returns dict of fields or None on EOF."""
        fields = {}
        while True:
            line = resp.fp.readline()
            if not line:
                return None
            line = line.decode().rstrip("\r\n")
            if line.startswith(":"):
                continue
            if line == "":
                if fields:
                    return fields
                continue
            k, _, v = line.partition(": ")
            fields[k] = v

    def test_sse_delivers_updated_and_list_changed(self):
        tok = token(subject="alice")
        sid = self.session(tok)
        self.assertEqual(self.post(tok, rpc("resources/subscribe", {"uri": "dyn://counter"}, mid=2), sid)[1]["result"], {})
        stream = self.open_stream(tok, sid)
        self.assertEqual(stream.getheader("Content-Type"), "text/event-stream")
        self.post(tok, rpc("tools/call", {"name": "counter_bump", "arguments": {}}, mid=3), sid)
        ev = self.read_event(stream)
        self.assertEqual(ev["event"], "message")
        self.assertEqual(json.loads(ev["data"]), {"jsonrpc": "2.0", "method": "notifications/resources/updated", "params": {"uri": "dyn://counter"}})
        self.post(tok, rpc("tools/call", {"name": "glossary_add", "arguments": {"term": "sse", "definition": "Streams."}}, mid=4), sid)
        ev = self.read_event(stream)
        self.assertEqual(json.loads(ev["data"])["method"], "notifications/resources/list_changed")
        self.assertEqual([ev["id"]], ["2"])

    def test_notification_caused_by_another_session_reaches_the_subscriber_stream(self):
        tok_a, tok_b = token(subject="alice"), token(subject="bob")
        sa, sb = self.session(tok_a), self.session(tok_b)
        self.post(tok_a, rpc("resources/subscribe", {"uri": "dyn://counter"}, mid=2), sa)
        stream = self.open_stream(tok_a, sa)
        self.post(tok_b, rpc("tools/call", {"name": "counter_bump", "arguments": {}}, mid=2), sb)
        ev = self.read_event(stream)
        self.assertEqual(json.loads(ev["data"])["params"]["uri"], "dyn://counter")

    def test_queued_before_stream_opened_is_delivered_on_open(self):
        tok = token()
        sid = self.session(tok)
        self.post(tok, rpc("resources/subscribe", {"uri": "dyn://counter"}, mid=2), sid)
        self.post(tok, rpc("tools/call", {"name": "counter_bump", "arguments": {}}, mid=3), sid)
        stream = self.open_stream(tok, sid)
        self.assertEqual(json.loads(self.read_event(stream)["data"])["method"], "notifications/resources/updated")

    def test_stream_rules(self):
        tok = token(subject="alice")
        sid = self.session(tok)
        c = self.conn()
        c.request("GET", "/mcp", headers={"Authorization": "Bearer " + tok, "Accept": "application/json", "Mcp-Session-Id": sid})
        self.assertEqual(c.getresponse().status, 406)
        c = self.conn()
        c.request("GET", "/mcp", headers={"Authorization": "Bearer " + tok, "Accept": "text/event-stream"})
        self.assertEqual(c.getresponse().status, 400)
        c = self.conn()
        c.request("GET", "/mcp", headers={"Authorization": "Bearer " + tok, "Accept": "text/event-stream", "Mcp-Session-Id": "nope"})
        self.assertEqual(c.getresponse().status, 404)
        c = self.conn()
        c.request("GET", "/mcp", headers={"Accept": "text/event-stream", "Mcp-Session-Id": sid})
        self.assertEqual(c.getresponse().status, 401)
        self.open_stream(token(subject="mallory"), sid, expect=403)
        first = self.open_stream(tok, sid)
        self.open_stream(tok, sid, expect=409)
        first.close()

    def test_delete_session_ends_stream_and_clears_subscriptions(self):
        tok = token()
        sid = self.session(tok)
        self.post(tok, rpc("resources/subscribe", {"uri": "dyn://counter"}, mid=2), sid)
        stream = self.open_stream(tok, sid)
        c = self.conn()
        c.request("DELETE", "/mcp", headers={"Authorization": "Bearer " + tok, "Mcp-Session-Id": sid})
        self.assertEqual(c.getresponse().status, 200)
        deadline = time.time() + 5
        ev = "pending"
        while time.time() < deadline:
            ev = self.read_event(stream)
            break
        self.assertIsNone(ev, "the stream is closed by the server after DELETE")
        self.assertEqual(self.server.hub.state(), {"subs": {}, "pending": {}})

    def test_keepalive_comment_is_sent(self):
        from mcpres.transport_http import _Handler
        old = _Handler.KEEPALIVE_SECONDS
        _Handler.KEEPALIVE_SECONDS = 0.3
        self.addCleanup(setattr, _Handler, "KEEPALIVE_SECONDS", old)
        tok = token()
        sid = self.session(tok)
        stream = self.open_stream(tok, sid)
        lines = []
        deadline = time.time() + 5
        while time.time() < deadline and ": keepalive" not in lines:
            lines.append(stream.fp.readline().decode().strip())
        self.assertIn(": keepalive", lines)


class SubscriptionModelCheck(unittest.TestCase):
    def setUp(self):
        self._snap = contracts.snapshot()
        self.addCleanup(contracts.restore, self._snap)  # injected bugs make contracts fire on purpose; do not pollute counters

    def test_depth4_exhaustive_passes(self):
        stats = modelcheck.check(4)
        self.assertEqual(stats["sequences"], 13 ** 4)
        self.assertTrue(stats["ok"], stats["violations"][:2])

    def test_injected_bug_missing_coalescing_is_caught(self):
        from mcpres.hub import Outbox
        orig = Outbox.put_coalesced

        def no_dedupe(self_, message):
            with self_._cond:
                if self_.closed:
                    return False
                self_._items.append(message)
                self_._cond.notify_all()
                return True
        Outbox.put_coalesced = no_dedupe
        try:
            stats = modelcheck.check(3, alphabet=("sub_AX", "upd_X", "drain_A"))
        finally:
            Outbox.put_coalesced = orig
        self.assertFalse(stats["ok"])
        self.assertIn("upd_X", stats["violations"][0]["trace"])
        self.assertIn("contract", stats["violations"][0]["problem"], "caught by the runtime SUB-3 contract as well as by the model")

    def test_injected_bug_unsubscribe_keeps_pending_is_caught(self):
        orig = Hub.unsubscribe

        def leaky(self_, sid, uri):
            with self_._lock:
                if sid in self_.subs:
                    self_.subs[sid].discard(uri)
        Hub.unsubscribe = leaky
        try:
            stats = modelcheck.check(3, alphabet=("sub_AX", "upd_X", "unsub_AX", "drain_A"))
        finally:
            Hub.unsubscribe = orig
        self.assertFalse(stats["ok"])

    def test_injected_bug_detach_leaves_subscriptions_is_caught(self):
        orig = Hub.detach

        def leaky(self_, sid):
            with self_._lock:
                box = self_.sinks.pop(sid, None)
                if box is not None:
                    box.close()
        Hub.detach = leaky
        try:
            stats = modelcheck.check(3, alphabet=("sub_AX", "close_A", "upd_X", "reopen_A"))
        finally:
            Hub.detach = orig
        self.assertFalse(stats["ok"])


if __name__ == "__main__":
    unittest.main()
