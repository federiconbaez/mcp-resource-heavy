import json
import unittest

from mcpres import paging
from tests.helpers import InProc, TempDirTestCase, make_server


class PagingUnit(unittest.TestCase):
    def test_walk_is_complete_and_duplicate_free(self):
        items = ["k%03d" % i for i in range(103)]
        for size in (1, 2, 7, 25, 102, 103, 104, 500):
            seen, cur, pages = [], None, 0
            while True:
                page, cur = paging.paginate(items, lambda x: x, cur, size, "t")
                seen += page
                pages += 1
                if cur is None:
                    break
            self.assertEqual(seen, items, size)
            self.assertEqual(pages, -(-103 // size), size)

    def test_exact_multiple_has_no_phantom_last_cursor(self):
        page, cur = paging.paginate(list("abcd"), lambda x: x, None, 2, "t")
        page2, cur2 = paging.paginate(list("abcd"), lambda x: x, cur, 2, "t")
        self.assertEqual((page, page2, cur2), (["a", "b"], ["c", "d"], None))

    def test_empty_list(self):
        self.assertEqual(paging.paginate([], lambda x: x, None, 5, "t"), ([], None))

    def test_keyset_stable_under_insert_and_delete_between_pages(self):
        items = ["b", "d", "f", "h", "j"]
        page, cur = paging.paginate(items, lambda x: x, None, 2, "t")
        self.assertEqual(page, ["b", "d"])
        items = ["a", "c", "d", "e", "f", "h", "j", "k"]  # inserts before/after the cursor, 'b' deleted
        rest, cur = [], cur
        while True:
            p, cur = paging.paginate(items, lambda x: x, cur, 2, "t")
            rest += p
            if cur is None:
                break
        self.assertEqual(rest, ["e", "f", "h", "j", "k"], "nothing already returned repeats; nothing after the cursor is skipped")

    def test_cursor_validation(self):
        good = paging.encode_cursor("t", "key")
        self.assertEqual(paging.decode_cursor(good, "t"), "key")
        bad = ["", "x", "!!!", good + "=", good[:-2], "e30", paging.encode_cursor("other", "key"), "a" * 2000, 5, None,
               "eyJ2IjoxLCJrIjoidCIsImEiOjV9"]  # a is not a string
        for b in bad:
            with self.assertRaises(paging.CursorError, msg=repr(b)[:30]):
                paging.decode_cursor(b, "t")

    def test_cursor_is_opaque_urlsafe_text(self):
        c = paging.encode_cursor("resources", "country://AR")
        self.assertTrue(all(ch.isalnum() or ch in "-_" for ch in c))


class PagingOverMcp(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.data_dir, auth=False, page_size=10)
        self.c = InProc(self.server.new_session("t"))
        self.c.init()

    def walk(self, method, key, **extra):
        out, cur, n = [], None, 0
        while True:
            params = dict(extra)
            if cur:
                params["cursor"] = cur
            res = self.c.call(method, params, mid=100 + n)["result"]
            out += res[key]
            n += 1
            self.assertLessEqual(len(res[key]), 10)
            cur = res.get("nextCursor")
            if not cur:
                return out, n

    def test_each_list_pages_completely(self):
        for method, key, ident in (("resources/list", "resources", "uri"), ("tools/list", "tools", "name"),
                                   ("prompts/list", "prompts", "name"), ("resources/templates/list", "resourceTemplates", "uriTemplate")):
            full = InProc(make_server(self.data_dir + "-big", auth=False, page_size=500).new_session("t"))
            full.init()
            expected = [x[ident] for x in full.call(method)["result"][key]]
            got, pages = self.walk(method, key)
            self.assertEqual([x[ident] for x in got], expected, method)
            self.assertEqual(len(set(expected)), len(expected), "ids unique: " + method)
            self.assertEqual(pages, -(-len(expected) // 10), method)

    def test_resource_list_is_large_and_sorted(self):
        got, pages = self.walk("resources/list", "resources")
        self.assertGreaterEqual(len(got), 130)
        self.assertEqual([r["uri"] for r in got], sorted(r["uri"] for r in got))

    def test_cursor_from_another_list_or_garbage_is_invalid_params(self):
        res = self.c.call("resources/list")["result"]
        cur = res["nextCursor"]
        for method in ("tools/list", "prompts/list", "resources/templates/list"):
            self.assertEqual(self.c.call(method, {"cursor": cur})["error"]["code"], -32602, method)
        for bad in ("garbage", "", "a" * 3000, "eyJ2IjoxfQ"):
            r = self.c.call("resources/list", {"cursor": bad})
            self.assertEqual(r["error"]["code"], -32602, bad[:20])
        self.assertEqual(self.c.call("resources/list", {"cursor": 5})["error"]["code"], -32602)

    def test_new_resource_between_pages_does_not_duplicate(self):
        first = self.c.call("resources/list")["result"]
        self.c.tool("glossary_add", {"term": "aaa-new", "definition": "Added between page fetches."})
        self.c.tool("memory_store", {"content": "x"})
        rest, cur = [], first["nextCursor"]
        while cur:
            res = self.c.call("resources/list", {"cursor": cur}, mid=7)["result"]
            rest += res["resources"]
            cur = res.get("nextCursor")
        seen = [r["uri"] for r in first["resources"]] + [r["uri"] for r in rest]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertIn("memory://entry/1", seen)

    def test_contracts_clean(self):
        from mcpres import contracts
        before = contracts.snapshot()
        _, pages = self.walk("resources/list", "resources")
        after = contracts.snapshot()
        self.assertEqual(after["violated"], before["violated"])
        self.assertGreaterEqual(after["checked"] - before["checked"], pages, "PAGE-1 is evaluated on every list page")


if __name__ == "__main__":
    unittest.main()
