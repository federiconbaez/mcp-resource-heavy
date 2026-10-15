import json
import random
import unittest

from tests import helpers
from tests.helpers import InProc, StdioServer, TempDirTestCase, init_msg, make_server, token
from mcpres import contracts, fuzz


class FuzzTests(TempDirTestCase):
    SEED = 1337

    def test_oracle_catches_wrong_behaviour(self):
        ping = '{"jsonrpc":"2.0","id":1,"method":"ping"}'
        self.assertIsNotNone(fuzz.check_output(ping, None))
        self.assertIsNotNone(fuzz.check_output(ping, '{"jsonrpc":"2.0","id":2,"result":{}}'))
        self.assertIsNotNone(fuzz.check_output(ping, '{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"x"}}'))
        self.assertIsNotNone(fuzz.check_output('{"jsonrpc":"2.0","method":"n"}', '{"jsonrpc":"2.0","id":null,"result":{}}'))
        self.assertIsNotNone(fuzz.check_output("{", '{"jsonrpc":"2.0","id":1,"result":{}}'))
        self.assertIsNone(fuzz.check_output(ping, '{"jsonrpc":"2.0","id":1,"result":{}}'))
        self.assertIsNone(fuzz.check_output("[]", '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"m"}}'))

    def test_generator_is_deterministic_and_diverse(self):
        a, b = fuzz.gen_cases(self.SEED, 300), fuzz.gen_cases(self.SEED, 300)
        self.assertEqual(a, b)
        self.assertNotEqual(a, fuzz.gen_cases(self.SEED + 1, 300))
        kinds = {"parse": 0, "obj": 0, "list": 0}
        for c in a:
            try:
                v = json.loads(c)
                kinds["list" if isinstance(v, list) else "obj"] += 1
            except (ValueError, RecursionError):
                kinds["parse"] += 1
        self.assertTrue(all(n > 20 for n in kinds.values()), kinds)

    def run_fuzz(self, client, n):
        stats = {"none": 0, "result": 0}
        for i, case in enumerate(fuzz.gen_cases(self.SEED, n)):
            out = client.session.handle_text(case)
            problem = fuzz.check_output(case, out)
            self.assertIsNone(problem, "case %d %r -> %r: %s" % (i, case[:100], (out or "")[:100], problem))
            if out is None:
                stats["none"] += 1
        return stats

    def test_initialized_session_survives_3000_random_cases(self):
        server = make_server(self.data_dir, auth=False)
        c = InProc(server.new_session("fuzz"))
        c.init()
        before = contracts.snapshot()["violated"]
        stats = self.run_fuzz(c, 3000)
        self.assertGreater(stats["none"], 10, "some cases are legitimately silent")
        self.assertEqual(contracts.snapshot()["violated"], before, "no runtime contract may fail under fuzz")
        self.assertEqual(c.call("ping", mid=1)["result"], {})
        self.assertTrue(server.audit.verify()["ok"])

    def test_uninitialized_session_survives_random_cases(self):
        server = make_server(self.data_dir, auth=False)
        c = InProc(server.new_session("fuzz"))
        self.run_fuzz(c, 1500)

    def test_authenticated_server_survives_random_cases(self):
        server = make_server(self.data_dir, auth=True)
        c = InProc(server.new_session("fuzz", token("mcp memory:read")))
        c.init()
        self.run_fuzz(c, 1500)

    def test_random_tokens_never_authenticate_or_crash(self):
        r = random.Random(self.SEED)
        a = helpers.Authenticator(helpers.SECRET, helpers.ISSUER)
        from mcpres.auth import AuthError
        valid = a.mint()
        for _ in range(2000):
            t = valid
            for _ in range(r.randint(1, 4)):
                i = r.randrange(len(t))
                t = t[:i] + r.choice("AZaz09-_.= \x00\u00e9") + t[i + 1:]
            if t == valid:
                continue
            try:
                a.verify(t)
            except AuthError:
                continue
            self.fail("mutated token accepted: %r" % t)
        for _ in range(500):
            junk = "".join(r.choice("abcXYZ019-_.=") for _ in range(r.randint(0, 80)))
            with self.assertRaises(AuthError):
                a.verify(junk)

    def test_stdio_subprocess_survives_fuzz(self):
        s = StdioServer(self.data_dir, token())
        s.call("initialize", init_msg()["params"])
        for case in fuzz.gen_cases(self.SEED + 7, 250):
            if not case.strip():
                continue
            s.send(case)
            s.send(json.dumps({"jsonrpc": "2.0", "id": "sentinel", "method": "ping"}))
            got = []
            while True:
                line = s.recv()
                self.assertIsNotNone(line, "server died on %r" % case[:80])
                if isinstance(line, dict) and line.get("id") == "sentinel":
                    break
                got.append(line)
            self.assertLessEqual(len(got), 1)
            out = json.dumps(got[0]) if got else None
            self.assertIsNone(fuzz.check_output(case, out), case[:100])
        self.assertEqual(s.close()[0], 0)


if __name__ == "__main__":
    unittest.main()
