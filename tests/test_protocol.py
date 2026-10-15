import itertools
import json
import os
import unittest

from tests import helpers
from tests.helpers import InProc, TempDirTestCase, init_msg, make_server, token


def text_of(res):
    return json.loads(res["result"]["content"][0]["text"])


class LifecycleTests(TempDirTestCase):
    def client(self, auth=False, tok=None):
        s = make_server(self.data_dir, auth=auth).new_session("test", tok)
        return InProc(s)

    def test_rejects_everything_before_initialize(self):
        c = self.client()
        for m in ("tools/list", "tools/call", "resources/list", "resources/read", "resources/templates/list",
                  "prompts/list", "prompts/get", "bogus/method"):
            out = c.call(m, {})
            self.assertEqual(out["error"]["code"], -32600, m)
            self.assertEqual(out["error"]["data"]["reason"], "not_initialized")

    def test_ping_before_and_after(self):
        c = self.client()
        self.assertEqual(c.call("ping")["result"], {})
        c.init()
        self.assertEqual(c.call("ping")["result"], {})

    def test_version_negotiation(self):
        for v in ("2025-03-26", "2024-11-05", "2025-06-18"):
            self.assertEqual(self.client().send(init_msg(v))["result"]["protocolVersion"], v)
        self.assertEqual(self.client().send(init_msg("2030-01-01"))["result"]["protocolVersion"], "2025-03-26")

    def test_initialize_result_shape(self):
        res = self.client().send(init_msg())["result"]
        self.assertEqual(res["serverInfo"]["name"], "mcp-resource-heavy")
        for cap in ("tools", "resources", "prompts"):
            self.assertIn(cap, res["capabilities"])
        self.assertNotIn("logging", res["capabilities"])
        self.assertNotIn("completions", res["capabilities"])

    def test_unadvertised_methods_not_found(self):
        c = self.client()
        c.init()
        for m in ("logging/setLevel", "completion/complete", "resources/subscribe", "resources/unsubscribe"):
            self.assertEqual(c.call(m, {})["error"]["code"], -32601, m)

    def test_initialize_params_validation(self):
        c = self.client()
        base = init_msg()
        for mutate in (lambda p: p.pop("protocolVersion"), lambda p: p.pop("capabilities"), lambda p: p.pop("clientInfo"),
                       lambda p: p.update(protocolVersion=3), lambda p: p.update(clientInfo={"name": "x"}),
                       lambda p: p.update(extra=1), lambda p: p.update(capabilities=[])):
            msg = json.loads(json.dumps(base))
            mutate(msg["params"])
            self.assertEqual(c.send(msg)["error"]["code"], -32602)
        self.assertEqual(c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})["error"]["code"], -32602)
        self.assertEqual(c.call("tools/list")["error"]["code"], -32600, "failed initialize must not initialize")

    def test_double_initialize_and_batched_initialize(self):
        c = self.client()
        out = c.send([init_msg()])
        self.assertEqual(out[0]["error"]["code"], -32600)
        self.assertEqual(c.send(init_msg())["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(c.send(init_msg())["error"]["data"]["reason"], "already_initialized")

    def test_initialized_notification_sets_ready_and_is_silent(self):
        s = make_server(self.data_dir, auth=False).new_session("test")
        c = InProc(s)
        self.assertIsNone(c.send({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertFalse(s.ready, "initialized before initialize must be ignored")
        c.send(init_msg())
        self.assertFalse(s.ready)
        self.assertIsNone(c.send({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertTrue(s.ready)

    def test_batching_depends_on_negotiated_version(self):
        c = self.client()
        c.init("2025-03-26")
        self.assertEqual(c.send([helpers.rpc("ping", mid=1)])[0]["result"], {})
        c2 = self.client()
        c2.init("2025-06-18")
        self.assertEqual(c2.send([helpers.rpc("ping", mid=1)])[0]["error"]["code"], -32600)


class ToolsResourcesPromptsTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.data_dir, auth=False)
        self.c = InProc(self.server.new_session("test"))
        self.c.init()

    def test_tools_list_descriptors(self):
        tools = {t["name"]: t for t in self.c.call("tools/list")["result"]["tools"]}
        self.assertEqual(set(tools), {"memory_store", "memory_recall", "memory_list", "memory_forget"})
        self.assertNotIn("title", tools["memory_store"])  # 2025-06-18 only
        self.assertTrue(tools["memory_forget"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["memory_list"]["annotations"]["readOnlyHint"])
        c = InProc(self.server.new_session("t"))
        c.init("2025-06-18")
        t = {x["name"]: x for x in c.call("tools/list")["result"]["tools"]}["memory_store"]
        self.assertIn("title", t)
        self.assertIn("outputSchema", t)

    def test_store_recall_list_forget_flow(self):
        stored = text_of(self.c.tool("memory_store", {"content": "Federico likes caf\u00e9", "tags": ["pref"], "importance": 4}))["stored"]
        self.assertEqual(stored["importance"], 4)
        self.assertEqual(text_of(self.c.tool("memory_recall", {"query": "CAFE"}))["count"], 1)
        self.assertEqual(text_of(self.c.tool("memory_recall", {"query": "pref"}))["count"], 1, "tags are searchable")
        self.assertEqual(text_of(self.c.tool("memory_recall", {"query": "caf", "tag": "other"}))["count"], 0)
        listed = text_of(self.c.tool("memory_list", {}))
        self.assertEqual((listed["total"], listed["entries"][0]["id"]), (1, stored["id"]))
        self.assertFalse(self.c.tool("memory_forget", {"id": stored["id"]})["result"]["isError"])
        forgot_again = self.c.tool("memory_forget", {"id": stored["id"]})
        self.assertTrue(forgot_again["result"]["isError"])
        self.assertNotIn("error", forgot_again)
        self.assertEqual(text_of(self.c.tool("memory_list", {}))["total"], 0)

    def test_tool_validation_errors_are_iserror_results(self):
        bad = [("memory_store", {}), ("memory_store", {"content": ""}), ("memory_store", {"content": "x" * 4001}),
               ("memory_store", {"content": "x", "importance": 0}), ("memory_store", {"content": "x", "importance": 6}),
               ("memory_store", {"content": "x", "tags": ["a b"]}), ("memory_store", {"content": "x", "tags": ["a"] * 17}),
               ("memory_store", {"content": "x", "zzz": 1}), ("memory_recall", {}), ("memory_recall", {"query": "x", "limit": 51}),
               ("memory_recall", {"query": "   "}), ("memory_list", {"limit": 0}), ("memory_list", {"offset": -1}),
               ("memory_forget", {"id": 0}), ("memory_forget", {"id": 1.5}), ("memory_forget", {})]
        for name, args in bad:
            out = self.c.tool(name, args)
            self.assertNotIn("error", out, (name, args))
            self.assertTrue(out["result"]["isError"], (name, args))

    def test_protocol_errors_for_tools_call(self):
        self.assertEqual(self.c.call("tools/call", {"name": "ghost"})["error"]["code"], -32602)
        self.assertEqual(self.c.call("tools/call", {})["error"]["code"], -32602)
        self.assertEqual(self.c.call("tools/call", {"name": "memory_list", "arguments": []})["error"]["code"], -32602)
        self.assertEqual(self.c.call("tools/call", [1])["error"]["code"], -32602)
        self.assertEqual(self.c.call("tools/call", {"name": "memory_list", "bogus": 1})["error"]["code"], -32602)

    def test_sql_injection_and_wildcards_are_inert(self):
        self.c.tool("memory_store", {"content": "100% real_deal"})
        self.c.tool("memory_store", {"content": "something else"})
        self.assertEqual(text_of(self.c.tool("memory_recall", {"query": "100%"}))["count"], 1)
        self.assertEqual(text_of(self.c.tool("memory_recall", {"query": "real_deal"}))["count"], 1)
        self.assertEqual(text_of(self.c.tool("memory_recall", {"query": "%"}))["count"], 1)
        self.assertEqual(text_of(self.c.tool("memory_recall", {"query": "x' OR '1'='1"}))["count"], 0)
        self.assertEqual(text_of(self.c.tool("memory_recall", {"query": "'; DROP TABLE memories;--"}))["count"], 0)
        self.assertEqual(text_of(self.c.tool("memory_list", {}))["total"], 2)

    def test_recall_ranking_and_limit(self):
        for i in range(8):
            self.c.tool("memory_store", {"content": "alpha beta " * (i + 1), "importance": 1})
        res = text_of(self.c.tool("memory_recall", {"query": "alpha", "limit": 3}))
        self.assertEqual(res["count"], 3)
        scores = [r["score"] for r in res["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_resources(self):
        stored = text_of(self.c.tool("memory_store", {"content": "res check", "tags": ["t1"]}))["stored"]
        res = self.c.call("resources/list")["result"]["resources"]
        uris = {r["uri"] for r in res}
        self.assertTrue({"memory://index", "audit://chain", "memory://entry/%d" % stored["id"]} <= uris)
        idx = json.loads(self.c.call("resources/read", {"uri": "memory://index"})["result"]["contents"][0]["text"])
        self.assertEqual((idx["total"], idx["tags"]), (1, {"t1": 1}))
        ent = json.loads(self.c.call("resources/read", {"uri": "memory://entry/%d" % stored["id"]})["result"]["contents"][0]["text"])
        self.assertEqual(ent["content"], "res check")
        tag = json.loads(self.c.call("resources/read", {"uri": "memory://tag/t1"})["result"]["contents"][0]["text"])
        self.assertEqual(len(tag["entries"]), 1)
        tpl = self.c.call("resources/templates/list")["result"]["resourceTemplates"]
        self.assertEqual({t["uriTemplate"] for t in tpl}, {"memory://entry/{id}", "memory://tag/{tag}"})
        for bad, code in (("memory://entry/424242", -32002), ("memory://nope", -32002), ("http://x", -32002), ("memory://entry/1/2", -32002)):
            self.assertEqual(self.c.call("resources/read", {"uri": bad})["error"]["code"], code, bad)
        self.assertEqual(self.c.call("resources/read", {})["error"]["code"], -32602)
        self.assertEqual(self.c.call("resources/read", {"uri": 5})["error"]["code"], -32602)

    def test_audit_resource_reports_chain_and_contracts(self):
        body = json.loads(self.c.call("resources/read", {"uri": "audit://chain"})["result"]["contents"][0]["text"])
        self.assertTrue(body["chain"]["ok"])
        self.assertEqual(body["contracts"]["violated"], 0)
        self.assertIn("RPC-1", body["invariants"])
        self.assertTrue(body["tail"])

    def test_prompts(self):
        self.c.tool("memory_store", {"content": "tengo un perro llamado Sol"})
        prompts = {p["name"]: p for p in self.c.call("prompts/list")["result"]["prompts"]}
        self.assertEqual(set(prompts), {"recall_and_summarize", "memory_digest"})
        res = self.c.call("prompts/get", {"name": "recall_and_summarize", "arguments": {"query": "perro", "style": "terse", "limit": "2"}})["result"]
        text = res["messages"][0]["content"]["text"]
        self.assertIn("perro llamado Sol", text)
        self.assertIn("terse", text)
        self.assertEqual(res["messages"][0]["role"], "user")
        for args in ({}, {"query": ""}, {"query": "x", "style": "haiku"}, {"query": "x", "limit": "0"}, {"query": "x", "limit": "21"},
                     {"query": "x", "limit": 5}, {"query": "x", "other": "y"}, {"query": ["x"]}):
            self.assertEqual(self.c.call("prompts/get", {"name": "recall_and_summarize", "arguments": args})["error"]["code"], -32602, args)
        self.assertEqual(self.c.call("prompts/get", {"name": "nope"})["error"]["code"], -32602)
        self.assertIn("perro", self.c.call("prompts/get", {"name": "memory_digest", "arguments": {}})["result"]["messages"][0]["content"]["text"])

    def test_list_methods_reject_cursor_accept_meta(self):
        for m in ("tools/list", "resources/list", "resources/templates/list", "prompts/list"):
            self.assertEqual(self.c.call(m, {"cursor": "x"})["error"]["code"], -32602)
            self.assertIn("result", self.c.call(m, {"_meta": {"progressToken": "p"}}))
            self.assertEqual(self.c.call(m, [])["error"]["code"], -32602)

    def test_structured_content_only_for_2025_06_18(self):
        self.assertNotIn("structuredContent", self.c.tool("memory_list", {})["result"])
        c = InProc(self.server.new_session("t"))
        c.init("2025-06-18")
        res = c.tool("memory_list", {})["result"]
        self.assertEqual(res["structuredContent"]["total"], json.loads(res["content"][0]["text"])["total"])


class ScopeEnforcementTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.data_dir, auth=True)

    def client(self, scopes):
        c = InProc(self.server.new_session("test", token(scopes)))
        self.assertIn("result", c.send(init_msg()))
        return c

    def test_read_only_scope(self):
        c = self.client("mcp memory:read")
        self.assertEqual(c.tool("memory_store", {"content": "x"})["error"]["code"], -32003)
        self.assertEqual(c.tool("memory_forget", {"id": 1})["error"]["code"], -32003)
        self.assertEqual(c.call("resources/read", {"uri": "audit://chain"})["error"]["code"], -32003)
        self.assertFalse(c.tool("memory_list", {})["result"]["isError"])
        uris = {r["uri"] for r in c.call("resources/list")["result"]["resources"]}
        self.assertNotIn("audit://chain", uris)

    def test_only_base_scope(self):
        c = self.client("mcp")
        self.assertEqual(c.tool("memory_list", {})["error"]["code"], -32003)
        self.assertEqual(c.call("prompts/get", {"name": "memory_digest"})["error"]["code"], -32003)
        self.assertEqual(c.call("resources/list")["result"]["resources"], [])
        self.assertIn("tools", c.call("tools/list")["result"])

    def test_error_data_names_required_scope(self):
        err = self.client("mcp").tool("memory_store", {"content": "x"})["error"]
        self.assertEqual(err["data"]["required"], ["memory:write"])

    def test_initialize_requires_valid_token_and_base_scope(self):
        for tok, code in ((None, -32001), ("junk", -32001), (token(ttl=-5), -32001), (token("memory:read"), -32003)):
            c = InProc(self.server.new_session("test", tok))
            out = c.send(init_msg())
            self.assertEqual(out["error"]["code"], code, tok)
            self.assertEqual(c.call("tools/list")["error"]["code"], -32600)

    def test_token_expiring_mid_session_is_enforced(self):
        from mcpres.auth import Authenticator
        clock = [1_800_000_000]
        auth = Authenticator(helpers.SECRET, helpers.ISSUER, clock=lambda: clock[0])
        server = helpers.McpServer(os.path.join(self.tmp, "d2"), auth, audit_fsync=False)
        tok = auth.mint(ttl=30)
        c = InProc(server.new_session("stdio", tok))
        c.init()
        self.assertIn("result", c.call("tools/list"))
        clock[0] += 31
        self.assertEqual(c.call("tools/list")["error"]["code"], -32001)

    def test_auth_failure_is_audited(self):
        InProc(self.server.new_session("test", "junk")).send(init_msg())
        events = [e for e in self.server.audit.tail(20) if e["dir"] == "event"]
        self.assertTrue(any("auth_failure" in e["payload"] for e in events))


class LifecycleModelCheck(TempDirTestCase):
    """Bounded exhaustive model check: every event sequence up to length 5 is run against the real
    server and compared with an independent 2-state oracle."""

    EVENTS = ["init_ok", "init_bad", "initialized_notif", "ping", "tools_list", "unknown"]

    @staticmethod
    def msg(ev, mid):
        if ev == "init_ok":
            return init_msg(mid=mid)
        if ev == "init_bad":
            return {"jsonrpc": "2.0", "id": mid, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}}}
        if ev == "initialized_notif":
            return {"jsonrpc": "2.0", "method": "notifications/initialized"}
        if ev == "ping":
            return helpers.rpc("ping", mid=mid)
        if ev == "tools_list":
            return helpers.rpc("tools/list", mid=mid)
        return helpers.rpc("no/such/method", mid=mid)

    @staticmethod
    def oracle(ev, initialized):
        """Return (expected, new_state): expected is None (silent), 'ok' or an error code."""
        if ev == "initialized_notif":
            return None, initialized
        if ev == "ping":
            return "ok", initialized
        if ev == "init_ok":
            return ("ok", True) if not initialized else (-32600, True)
        if ev == "init_bad":
            return (-32602, False) if not initialized else (-32600, True)
        if ev == "tools_list":
            return ("ok", True) if initialized else (-32600, False)
        return (-32601, True) if initialized else (-32600, False)

    def test_all_sequences_up_to_length_5(self):
        server = make_server(self.data_dir, auth=False, fsync=False)
        n = 0
        for length in range(1, 6):
            for seq in itertools.product(self.EVENTS, repeat=length):
                c = InProc(server.new_session("model"))
                state = False
                for i, ev in enumerate(seq):
                    expected, state = self.oracle(ev, state)
                    out = c.send(self.msg(ev, i + 1))
                    if expected is None:
                        self.assertIsNone(out, seq)
                    elif expected == "ok":
                        self.assertIn("result", out, (seq, i, out))
                    else:
                        self.assertEqual(out["error"]["code"], expected, (seq, i, out))
                    if out is not None:
                        self.assertEqual(out["id"], i + 1)
                n += 1
        self.assertEqual(n, sum(6 ** k for k in range(1, 6)))


if __name__ == "__main__":
    unittest.main()
