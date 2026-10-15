import json
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
from mcpres import contracts, jsonrpc
from mcpres.jsonrpc import Dispatcher, RpcError


def echo_handler(req):
    if req.method == "boom":
        raise RuntimeError("secret internal detail")
    if req.method == "rpcerr":
        raise RpcError(-32602, "bad", {"k": 1})
    return {"method": req.method, "params": req.params}


def run(text, handler=echo_handler):
    out = Dispatcher(handler).handle_text(text)
    return None if out is None else json.loads(out)


class JsonRpcTests(unittest.TestCase):
    def test_success_echoes_id_types(self):
        for mid in (1, "a", "", 0, -3, 2 ** 70, "\u00e9\U0001F600"):
            out = run(json.dumps({"jsonrpc": "2.0", "id": mid, "method": "m"}))
            self.assertEqual(out["id"], mid)
            self.assertIs(type(out["id"]), type(mid))
            self.assertIn("result", out)
            self.assertNotIn("error", out)

    def test_parse_error(self):
        for bad in ("", "{", "nope", '{"a":}', "[1,", '{"jsonrpc":"2.0","method":"x","params":NaN}', "Infinity", "\x00"):
            out = run(bad)
            self.assertEqual(out["error"]["code"], -32700, bad)
            self.assertIsNone(out["id"])

    def test_deep_nesting_is_parse_error_not_crash(self):
        self.assertEqual(run("[" * 200000)["error"]["code"], -32700)

    def test_invalid_request_variants(self):
        cases = [
            '{"jsonrpc":"2.0"}', '{"method":"x","id":1}', '{"jsonrpc":"1.0","method":"x","id":1}',
            '{"jsonrpc":2,"method":"x","id":1}', '{"jsonrpc":"2.0","method":1,"id":1}', '{"jsonrpc":"2.0","method":"","id":1}',
            '{"jsonrpc":"2.0","method":"x","params":3,"id":1}', '{"jsonrpc":"2.0","method":"x","params":"s"}',
            '{"jsonrpc":"2.0","method":"x","id":true}', '{"jsonrpc":"2.0","method":"x","id":1.5}',
            '{"jsonrpc":"2.0","method":"x","id":null}', '{"jsonrpc":"2.0","method":"x","id":[1]}',
            '{"jsonrpc":"2.0","method":"x","id":1,"result":1}', '1', '"s"', 'null', 'true',
        ]
        for c in cases:
            out = run(c)
            self.assertEqual(out["error"]["code"], -32600, c)

    def test_invalid_request_echoes_valid_id_only(self):
        self.assertEqual(run('{"jsonrpc":"1.0","method":"x","id":7}')["id"], 7)
        self.assertIsNone(run('{"jsonrpc":"2.0","method":"x","id":true}')["id"])
        self.assertIsNone(run('{"jsonrpc":"2.0","method":1}')["id"])

    def test_notifications_get_no_response(self):
        self.assertIsNone(run('{"jsonrpc":"2.0","method":"x"}'))
        self.assertIsNone(run('{"jsonrpc":"2.0","method":"boom"}'))  # handler failure on a notification is silent
        self.assertIsNone(run('{"jsonrpc":"2.0","method":"rpcerr"}'))
        self.assertIsNone(run('{"jsonrpc":"2.0","id":1,"result":{}}'))  # stray client response
        self.assertIsNone(run('{"jsonrpc":"2.0","id":1,"error":{"code":1,"message":"m"}}'))

    def test_batch_rules(self):
        self.assertEqual(run("[]")["error"]["code"], -32600)
        out = run("[1,2,3]")
        self.assertEqual([o["error"]["code"] for o in out], [-32600] * 3)
        out = run(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "a"}, {"jsonrpc": "2.0", "method": "n"},
                              {"jsonrpc": "2.0", "id": "z", "method": "rpcerr"}, 5]))
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["id"], 1)
        self.assertEqual(out[1]["id"], "z")
        self.assertEqual(out[1]["error"]["code"], -32602)
        self.assertEqual(out[1]["error"]["data"], {"k": 1})
        self.assertEqual(out[2]["error"]["code"], -32600)
        self.assertIsNone(run(json.dumps([{"jsonrpc": "2.0", "method": "n"}] * 3)))

    def test_handler_exceptions(self):
        out = run('{"jsonrpc":"2.0","id":5,"method":"boom"}')
        self.assertEqual(out["error"]["code"], -32603)
        self.assertEqual(out["id"], 5)
        self.assertNotIn("secret", json.dumps(out), "internal details must not leak")

    def test_method_not_found_and_invalid_params_codes(self):
        self.assertEqual(jsonrpc.METHOD_NOT_FOUND, -32601)
        self.assertEqual(jsonrpc.INVALID_PARAMS, -32602)
        self.assertEqual(jsonrpc.INTERNAL_ERROR, -32603)
        out = run('{"jsonrpc":"2.0","id":1,"method":"rpcerr"}')
        self.assertEqual(out["error"], {"code": -32602, "message": "bad", "data": {"k": 1}})

    def test_output_is_single_line_ascii(self):
        text = Dispatcher(echo_handler).handle_text(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "m", "params": {"s": "a\nb\u00e9\U0001F600"}}))
        self.assertNotIn("\n", text)
        text.encode("ascii")

    def test_nan_result_is_internal_error_not_crash(self):
        d = Dispatcher(lambda r: {"x": float("nan")})
        out = json.loads(d.handle_text('{"jsonrpc":"2.0","id":1,"method":"m"}'))
        self.assertEqual(out["error"]["code"], -32603)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.snap = contracts.snapshot()
        self.before = self.snap["violated"]
        # some tests below provoke violations on purpose; don't let them pollute process-wide counters
        self.addCleanup(contracts.restore, self.snap)

    def test_contracts_run_and_pass_on_good_traffic(self):
        c0 = contracts.snapshot()["checked"]
        run('{"jsonrpc":"2.0","id":1,"method":"m"}')
        self.assertGreater(contracts.snapshot()["checked"], c0)
        self.assertEqual(contracts.snapshot()["violated"], self.before)

    def test_wrong_id_is_caught_by_contract(self):
        events = []
        d = Dispatcher(echo_handler, on_event=lambda k, det: events.append((k, det)))
        with mock.patch.object(jsonrpc, "make_result", lambda i, r: {"jsonrpc": "2.0", "id": 99, "result": r}):
            out = json.loads(d.handle_text('{"jsonrpc":"2.0","id":1,"method":"m"}'))
        self.assertEqual(out["error"]["code"], -32603)
        self.assertEqual(out["id"], 1)
        self.assertEqual(out["error"]["data"]["contract"], "RPC-1")
        self.assertEqual(events[0][0], "contract_violation")
        self.assertEqual(contracts.snapshot()["violated"], self.before + 1)

    def test_response_to_notification_is_caught(self):
        events = []
        d = Dispatcher(echo_handler, on_event=lambda k, det: events.append(k))
        orig = Dispatcher._one.__wrapped__

        def leaky(self, obj, in_batch):  # a buggy dispatcher that answers notifications
            r = orig(self, obj, in_batch)
            return r if r is not None else jsonrpc.make_result(None, {})

        with mock.patch.object(Dispatcher, "_one", contracts.ensures(jsonrpc.INV_ONE, jsonrpc._post_one)(leaky)):
            out = d.handle_text('{"jsonrpc":"2.0","method":"n"}')
        self.assertIsNone(out, "contract violation on a notification must be swallowed, not answered")
        self.assertIn("contract_violation", events)

    def test_ensures_requires_decorators(self):
        @contracts.requires("T-PRE", lambda a, k: a[0] > 0 or "need positive")
        @contracts.ensures("T-POST", lambda a, k, r: r == a[0] * 2)
        def double(x):
            return x * 2 if x != 3 else 7

        self.assertEqual(double(2), 4)
        with self.assertRaises(contracts.ContractViolation) as cm:
            double(-1)
        self.assertEqual(cm.exception.name, "T-PRE")
        with self.assertRaises(contracts.ContractViolation) as cm:
            double(3)
        self.assertEqual(cm.exception.name, "T-POST")


if __name__ == "__main__":
    unittest.main()
