import json
import unittest

from tests import helpers
from tests.helpers import StdioServer, TempDirTestCase, init_msg, rpc, token, run_cli


class StdioE2E(TempDirTestCase):
    def test_full_session_over_real_subprocess(self):
        s = StdioServer(self.data_dir, token())
        self.assertEqual(s.call("ping")["result"], {})
        init = s.call("initialize", init_msg("2025-03-26")["params"], mid="init-1")
        self.assertEqual(init["id"], "init-1")
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = s.call("tools/list", mid=2)["result"]["tools"]
        self.assertEqual(len(tools), 16)
        r = s.call("tools/call", {"name": "memory_store", "arguments": {"content": "e2e"}}, mid=3)
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(s.recv()["method"], "notifications/resources/list_changed", "memory_store changed the resource list; the notification follows the response")
        # batch + notification in one line
        s.send([rpc("ping", mid=10), {"jsonrpc": "2.0", "method": "notifications/initialized"}, rpc("ping", mid=11)])
        out = s.recv()
        self.assertEqual(sorted(o["id"] for o in out), [10, 11])
        self.assertEqual(s.close()[0], 0)

    def test_stdout_is_pure_jsonrpc_and_stderr_has_diagnostics(self):
        s = StdioServer(self.data_dir, token())
        s.call("initialize", init_msg()["params"])
        s.send("garbage{")
        out = s.recv()
        self.assertEqual(out["error"]["code"], -32700)
        code, err = s.close()
        self.assertEqual(code, 0)
        self.assertIn('"event": "ready"', err)

    def test_requires_token_unless_no_auth(self):
        s = StdioServer(self.data_dir, None)
        self.assertEqual(s.call("initialize", init_msg()["params"])["error"]["code"], -32001)
        code, err = s.close()
        self.assertIn("MCP_TOKEN not set", err)
        s = StdioServer(self.data_dir, None, extra=["--no-auth"])
        self.assertIn("result", s.call("initialize", init_msg()["params"]))
        s.close()

    def test_bad_tokens_rejected_end_to_end(self):
        for tok in (token(ttl=-10), token(audience="x"), token("memory:read"), token()[:-5] + "AAAAA", "junk"):
            s = StdioServer(self.data_dir, tok)
            self.assertIn("error", s.call("initialize", init_msg()["params"]), tok)
            self.assertEqual(s.call("tools/list", mid=2)["error"]["code"], -32600)
            s.close()

    def test_cli_mint_token_is_accepted_by_server(self):
        env = {"MCP_FORGE_SECRET": helpers.SECRET.decode()}
        out = run_cli("mint-token", "--issuer", helpers.ISSUER, "--scope", "mcp memory:read", "--sub", "cli-user", env=env)
        self.assertEqual(out.returncode, 0)
        tok = out.stdout.strip()
        v = run_cli("verify-token", tok, "--issuer", helpers.ISSUER, "--require-scope", "memory:read", env=env)
        self.assertEqual(v.returncode, 0)
        self.assertEqual(json.loads(v.stdout)["claims"]["sub"], "cli-user")
        expired = run_cli("mint-token", "--issuer", helpers.ISSUER, "--ttl=-30", env=env).stdout.strip()
        v = run_cli("verify-token", expired, "--issuer", helpers.ISSUER, env=env)
        self.assertEqual(v.returncode, 1)
        self.assertIn("expired", v.stdout)
        s = StdioServer(self.data_dir, tok)
        self.assertIn("result", s.call("initialize", init_msg()["params"]))
        s.close()

    def test_oversized_line_then_recovery(self):
        s = StdioServer(self.data_dir, token())
        s.call("initialize", init_msg()["params"])
        s.send('{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + "a" * (1 << 21) + '"}}')
        self.assertEqual(s.recv()["error"]["code"], -32600)
        self.assertEqual(s.call("ping", mid=5)["result"], {})
        s.close()

    def test_eof_exits_cleanly_without_output(self):
        s = StdioServer(self.data_dir, token())
        code, err = s.close()
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
