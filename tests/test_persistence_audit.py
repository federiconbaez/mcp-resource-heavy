import json
import os
import threading
import unittest

from tests import helpers
from tests.helpers import InProc, StdioServer, TempDirTestCase, init_msg, make_server, run_cli, token
from mcpres.audit import GENESIS, AuditLog, verify_file
from mcpres.memory import MemoryStore, fold


class MemoryTests(TempDirTestCase):
    def test_fold(self):
        self.assertEqual(fold("Canci\u00f3n \u00d1and\u00fa"), "cancion nandu")

    def test_persistence_across_store_instances(self):
        path = os.path.join(self.data_dir, "m.db")
        a = MemoryStore(path)
        mid = a.store("remember me", ["x"], 5, "tester")["id"]
        del a
        b = MemoryStore(path)
        got = b.get(mid)
        self.assertEqual((got["content"], got["tags"], got["importance"], got["created_by"]), ("remember me", ["x"], 5, "tester"))
        self.assertEqual(b.count(), 1)

    def test_ids_never_reused_after_forget(self):
        m = MemoryStore(os.path.join(self.data_dir, "m.db"))
        a = m.store("a")["id"]
        m.forget(a)
        self.assertGreater(m.store("b")["id"], a)

    def test_cascade_delete_of_tags(self):
        m = MemoryStore(os.path.join(self.data_dir, "m.db"))
        a = m.store("a", ["t"])["id"]
        m.forget(a)
        self.assertEqual(m.tag_counts(), {})

    def test_concurrent_writers(self):
        m = MemoryStore(os.path.join(self.data_dir, "m.db"))
        errs = []

        def work(i):
            try:
                for j in range(10):
                    m.store("thread %d item %d" % (i, j))
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(m.count(), 60)

    def test_persistence_across_server_restart_inprocess(self):
        s1 = make_server(self.data_dir, auth=False)
        c1 = InProc(s1.new_session("t"))
        c1.init()
        c1.tool("memory_store", {"content": "persisted note", "tags": ["keep"]})
        del c1, s1
        s2 = make_server(self.data_dir, auth=False)
        c2 = InProc(s2.new_session("t"))
        c2.init()
        res = json.loads(c2.tool("memory_recall", {"query": "persisted"})["result"]["content"][0]["text"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["tags"], ["keep"])

    def test_persistence_across_real_process_restart_and_sigkill(self):
        tok = token()
        s1 = StdioServer(self.data_dir, tok)
        self.assertIn("result", s1.call("initialize", init_msg()["params"]))
        s1.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = s1.call("tools/call", {"name": "memory_store", "arguments": {"content": "survives kill -9", "importance": 5}}, mid=2)
        self.assertFalse(r["result"]["isError"])
        s1.proc.kill()  # no graceful shutdown
        s1.proc.wait()
        s1.proc.stdin.close(); s1.proc.stdout.close(); s1.proc.stderr.close()
        s2 = StdioServer(self.data_dir, tok)
        s2.call("initialize", init_msg()["params"])
        r = s2.call("tools/call", {"name": "memory_recall", "arguments": {"query": "survives"}}, mid=2)
        found = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(found["count"], 1)
        self.assertEqual(s2.close()[0], 0)
        self.assertEqual(run_cli("audit-verify", "--data-dir", self.data_dir).returncode, 0, "audit chain must survive kill -9")


class AuditTests(TempDirTestCase):
    def make_log(self, n=6):
        path = os.path.join(self.data_dir, "audit.log")
        log = AuditLog(path)
        for i in range(n):
            log.append("request" if i % 2 == 0 else "response", '{"n": %d}' % i, "test", "s1", "me")
        return path, log

    def lines(self, path):
        with open(path, "rb") as fh:
            return fh.read().splitlines(True)

    def write(self, path, lines):
        with open(path, "wb") as fh:
            fh.write(b"".join(lines))

    def test_chain_structure(self):
        path, log = self.make_log()
        entries = [json.loads(l) for l in self.lines(path)]
        self.assertEqual([e["seq"] for e in entries], [1, 2, 3, 4, 5, 6])
        self.assertEqual(entries[0]["prev"], GENESIS)
        for a, b in zip(entries, entries[1:]):
            self.assertEqual(b["prev"], a["hash"])
        res = verify_file(path)
        self.assertEqual((res["ok"], res["count"], res["head"]), (True, 6, entries[-1]["hash"]))

    def test_resume_after_reopen(self):
        path, log = self.make_log(3)
        AuditLog(path).append("event", "x")
        res = verify_file(path)
        self.assertEqual((res["ok"], res["count"]), (True, 4))

    def test_modified_payload_detected(self):
        path, _ = self.make_log()
        ls = self.lines(path)
        ls[2] = ls[2].replace(b'{\\"n\\": 2}', b'{\\"n\\": 9}') if b'{\\"n\\": 2}' in ls[2] else ls[2].replace(b"2", b"9", 1)
        self.write(path, ls)
        res = verify_file(path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["line"], 3)

    def test_recomputed_hash_forgery_breaks_next_link(self):
        path, _ = self.make_log()
        ls = self.lines(path)
        e = json.loads(ls[1])
        e["payload"] = "forged"
        from mcpres.audit import canonical, entry_hash
        e["hash"] = entry_hash(e)  # attacker fixes the entry's own hash...
        ls[1] = canonical(e) + b"\n"
        self.write(path, ls)
        res = verify_file(path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["line"], 3, "...but the next entry's prev no longer matches")

    def test_deletion_reorder_insertion_detected(self):
        path, _ = self.make_log()
        ls = self.lines(path)
        for tampered in (ls[:2] + ls[3:], ls[:1] + [ls[2], ls[1]] + ls[3:], ls[:3] + [ls[1]] + ls[3:], [ls[0]] * 2):
            self.write(path, tampered)
            self.assertFalse(verify_file(path)["ok"])

    def test_truncation_needs_anchor(self):
        path, log = self.make_log()
        head = verify_file(path)
        ls = self.lines(path)
        self.write(path, ls[:-2])
        self.assertTrue(verify_file(path)["ok"], "documented limitation: tail truncation alone is invisible")
        self.assertFalse(verify_file(path, expect_head=head["head"])["ok"])
        self.assertFalse(verify_file(path, expect_count=head["count"])["ok"])
        self.write(path, ls)
        self.assertTrue(verify_file(path, expect_head=head["head"], expect_count=6)["ok"])

    def test_garbage_and_partial_lines(self):
        path, _ = self.make_log(3)
        with open(path, "ab") as fh:
            fh.write(b'{"seq": 4, "prev"')
        self.assertFalse(verify_file(path)["ok"])
        self.write(path, [b"not json\n"])
        self.assertFalse(verify_file(path)["ok"])
        self.write(path, [b"[1,2]\n"])
        self.assertFalse(verify_file(path)["ok"])

    def test_missing_file(self):
        self.assertFalse(verify_file(os.path.join(self.tmp, "nope.log"))["ok"])

    def test_concurrent_appends_keep_chain_valid(self):
        path = os.path.join(self.data_dir, "audit.log")
        logs = [AuditLog(path) for _ in range(3)]  # separate instances ~ separate processes (flock)
        ts = [threading.Thread(target=lambda lg=lg: [lg.append("event", "x") for _ in range(25)]) for lg in logs]
        [t.start() for t in ts]
        [t.join() for t in ts]
        res = verify_file(path)
        self.assertEqual((res["ok"], res["count"]), (True, 75))

    def test_server_traffic_is_chained_and_cli_detects_tamper(self):
        s = make_server(self.data_dir, auth=True)
        c = InProc(s.new_session("test", token()))
        c.init()
        c.tool("memory_store", {"content": "audited"})
        c.send("{bad json")
        res = run_cli("audit-verify", "--data-dir", self.data_dir)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("AUDIT CHAIN OK", res.stderr)
        entries = [json.loads(l) for l in self.lines(os.path.join(self.data_dir, "audit.log"))]
        dirs = [e["dir"] for e in entries]
        # requests: initialize, initialized-notification, tools/call, malformed line; responses: all but the notification
        self.assertEqual((dirs.count("request"), dirs.count("response")), (4, 3))
        self.assertTrue(all(e["session"] for e in entries))
        self.assertTrue(any("audited" in e["payload"] for e in entries))
        self.assertFalse(any("eyJ" in e["payload"] for e in entries), "bearer tokens must never reach the audit log")
        path = os.path.join(self.data_dir, "audit.log")
        with open(path, "rb") as fh:
            raw = fh.read()
        with open(path, "wb") as fh:
            fh.write(raw.replace(b"audited", b"AUDITED", 1))
        bad = run_cli("audit-verify", "--data-dir", self.data_dir)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("BROKEN", bad.stderr)


if __name__ == "__main__":
    unittest.main()
