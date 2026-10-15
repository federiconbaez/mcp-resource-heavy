import base64
import hashlib
import json
import unittest

from mcpres import datasets
from tests.helpers import InProc, TempDirTestCase, init_msg, make_server, token


def read(c, uri, mid=1):
    return c.call("resources/read", {"uri": uri}, mid)


def body(c, uri):
    r = read(c, uri)
    assert "result" in r, r
    return json.loads(r["result"]["contents"][0]["text"])


def tool(c, name, args=None):
    r = c.tool(name, args or {})
    assert "result" in r, r
    res = r["result"]
    return res, (json.loads(res["content"][0]["text"]) if not res["isError"] else res["content"][0]["text"])


class ResourceKinds(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.data_dir, auth=False, page_size=500)
        self.c = InProc(self.server.new_session("t"))
        self.c.init()

    def test_static(self):
        about = read(self.c, "static://about")["result"]["contents"][0]
        self.assertEqual((about["mimeType"], about["uri"]), ("text/markdown", "static://about"))
        self.assertIn("resource-heavy", about["text"])
        self.assertEqual(read(self.c, "static://about")["result"], read(self.c, "static://about")["result"], "static means identical on every read")
        self.assertIn("approximate", read(self.c, "static://dataset-notice")["result"]["contents"][0]["text"].lower())
        schema = body(self.c, "static://schema/country")
        self.assertIn("iso2", schema["required"])

    def test_whole_datasets(self):
        self.assertEqual(len(body(self.c, "data://countries")["countries"]), len(datasets.COUNTRIES))
        self.assertEqual(len(body(self.c, "data://elements")["elements"]), 36)
        self.assertEqual(set(body(self.c, "data://units")["categories"]), set(datasets.UNIT_CATEGORIES))
        self.assertEqual(len(body(self.c, "data://glossary")["glossary"]), len(datasets.GLOSSARY))

    def test_templated(self):
        self.assertEqual(body(self.c, "country://AR")["capital"], "Buenos Aires")
        self.assertEqual(body(self.c, "country://ar")["iso2"], "AR", "case-insensitive on read")
        self.assertEqual(body(self.c, "element://Fe")["number"], 26)
        self.assertEqual(body(self.c, "element://26")["symbol"], "Fe")
        self.assertEqual(body(self.c, "element://Co")["name"], "Cobalt")
        self.assertEqual(body(self.c, "unit://length")["base_unit"], "m")
        self.assertEqual(body(self.c, "unit://temperature")["units"]["F"]["to_K"], "(F - 32) * 5/9 + 273.15")
        self.assertEqual(body(self.c, "glossary://term/mime%20type")["term"], "mime type")
        self.assertEqual(body(self.c, "glossary://term/URI")["term"], "uri", "normalised")
        for bad in ("country://ZZ", "country://ARG", "element://Zz", "element://99", "element://0", "unit://nope", "glossary://term/nope",
                    "blob://nope.bin", "data://nope", "static://nope", "dyn://nope", "glossary://", "country://", "memory://entry/9", "element://CO"):
            r = read(self.c, bad)
            self.assertEqual(r["error"]["code"], -32002, bad)
            self.assertEqual(r["error"]["data"], {"uri": bad})

    def test_dynamic_counter_changes_and_persists(self):
        self.assertEqual(body(self.c, "dyn://counter"), {"value": 0, "bumps": 0})
        tool(self.c, "counter_bump")
        tool(self.c, "counter_bump", {"by": 5})
        tool(self.c, "counter_bump", {"by": -2})
        self.assertEqual(body(self.c, "dyn://counter"), {"value": 4, "bumps": 3})
        again = InProc(make_server(self.data_dir, auth=False).new_session("t2"))
        again.init()
        self.assertEqual(body(again, "dyn://counter")["value"], 4, "survives a new server instance over the same data dir")
        self.assertEqual(tool(self.c, "counter_bump", {"by": 5000})[1].startswith("Invalid arguments"), True)

    def test_dynamic_clock_differs_between_reads(self):
        times = []
        clock = [1_800_000_000.0]
        server = make_server(self.data_dir + "-c", auth=False, clock=lambda: clock[0])
        c = InProc(server.new_session("t"))
        c.init()
        times.append(body(c, "dyn://clock")["utc"])
        clock[0] += 61.5
        times.append(body(c, "dyn://clock")["utc"])
        self.assertEqual(times, ["2027-01-15T08:00:00.000Z", "2027-01-15T08:01:01.500Z"])

    def test_user_glossary_is_dynamic_resource(self):
        self.assertEqual(body(self.c, "glossary://user")["terms"], [])
        res, out = tool(self.c, "glossary_add", {"term": "Zebra Crossing", "definition": "A place to cross."})
        self.assertEqual((out["term"], out["created_new"]), ("zebra crossing", True))
        self.assertEqual(body(self.c, "glossary://term/zebra%20crossing")["definition"], "A place to cross.")
        self.assertEqual([t["term"] for t in body(self.c, "glossary://user")["terms"]], ["zebra crossing"])
        allterms = [t["term"] for t in body(self.c, "glossary://all")["terms"]]
        self.assertIn("zebra crossing", allterms)
        self.assertIn("uri", allterms)
        self.assertEqual(allterms, sorted(allterms))
        uris = {r["uri"] for r in self.c.call("resources/list")["result"]["resources"]}
        self.assertIn("glossary://term/zebra%20crossing", uris)
        self.assertEqual(tool(self.c, "glossary_add", {"term": "uri", "definition": "x"})[1], "'uri' is a built-in term and cannot be redefined")
        self.assertTrue(tool(self.c, "glossary_add", {"term": "BAD!", "definition": "x"})[0]["isError"])
        self.assertEqual(tool(self.c, "glossary_remove", {"term": "zebra crossing"})[1], {"removed": "zebra crossing"})
        self.assertEqual(read(self.c, "glossary://term/zebra%20crossing")["error"]["code"], -32002)
        self.assertTrue(tool(self.c, "glossary_remove", {"term": "uri"})[0]["isError"])

    def test_binary_blob_roundtrip_matches_independent_digest(self):
        for name in ("sample.png", "random.bin"):
            item = read(self.c, "blob://" + name)["result"]["contents"][0]
            self.assertIn("blob", item)
            self.assertNotIn("text", item)
            raw = base64.b64decode(item["blob"], validate=True)
            self.assertEqual(raw, datasets.BLOBS[name]["data"])
            self.assertEqual(item["mimeType"], datasets.BLOBS[name]["mime"])
            res, out = tool(self.c, "resource_digest", {"uri": "blob://" + name})
            self.assertEqual(out["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual((out["bytes"], out["kind"]), (len(raw), "blob"))
        listed = {r["uri"]: r for r in self.c.call("resources/list")["result"]["resources"]}
        self.assertEqual(listed["blob://sample.png"]["size"], len(datasets.BLOBS["sample.png"]["data"]))
        self.assertEqual(listed["blob://sample.png"]["mimeType"], "image/png")

    def test_text_digest_and_unknown(self):
        res, out = tool(self.c, "resource_digest", {"uri": "static://dataset-notice"})
        self.assertEqual(out["kind"], "text")
        self.assertEqual(self.c.tool("resource_digest", {"uri": "nope://x"})["error"]["code"], -32002)

    def test_templates_cover_the_documented_kinds(self):
        tpl = {t["uriTemplate"]: t for t in self.c.call("resources/templates/list")["result"]["resourceTemplates"]}
        self.assertEqual(set(tpl), {"country://{iso2}", "element://{symbol}", "unit://{category}", "glossary://term/{term}", "blob://{name}",
                                    "memory://entry/{id}", "memory://tag/{tag}"})
        for t in tpl.values():
            self.assertTrue({"name", "title", "description", "mimeType"} <= set(t))

    def test_every_listed_resource_is_readable(self):
        self.c.tool("memory_store", {"content": "readable", "tags": ["a"]})
        cur, uris = None, []
        server = make_server(self.data_dir + "-p", auth=False, page_size=40)
        c = InProc(server.new_session("t"))
        c.init()
        c.tool("memory_store", {"content": "readable", "tags": ["a"]})
        while True:
            res = c.call("resources/list", {"cursor": cur} if cur else {})["result"]
            uris += [r["uri"] for r in res["resources"]]
            cur = res.get("nextCursor")
            if not cur:
                break
        self.assertGreaterEqual(len(uris), 130)
        for uri in uris:
            r = read(c, uri)
            self.assertIn("result", r, uri)
            item = r["result"]["contents"][0]
            self.assertEqual(item["uri"], uri)
            self.assertTrue(("text" in item) != ("blob" in item), uri)

    def test_resource_links_only_in_2025_06_18(self):
        res, _ = tool(self.c, "country_lookup", {"query": "Japan"})
        self.assertEqual([x["type"] for x in res["content"]], ["text"])
        c2 = InProc(self.server.new_session("t2"))
        c2.init("2025-06-18")
        res, _ = tool(c2, "country_lookup", {"query": "Japan"})
        self.assertEqual(res["content"][1], {"type": "resource_link", "uri": "country://JP", "name": "JP", "mimeType": "application/json"})
        self.assertEqual(res["structuredContent"]["country"]["capital"], "Tokyo")
        self.assertNotIn("_links", res["structuredContent"])


class ScopedAccess(TempDirTestCase):
    def client(self, scopes):
        server = make_server(self.data_dir, auth=True)
        c = InProc(server.new_session("t", token(scopes)))
        self.assertIn("result", c.send(init_msg()))
        return c

    def test_data_scope_gates_dataset_resources_and_tools(self):
        c = self.client("mcp memory:read")
        self.assertEqual(read(c, "country://AR")["error"]["code"], -32003)
        self.assertEqual(c.tool("country_lookup", {"query": "AR"})["error"]["code"], -32003)
        uris = {r["uri"] for r in c.call("resources/list")["result"]["resources"]}
        self.assertEqual({u.split("://")[0] for u in uris}, {"memory"})
        tpls = {t["uriTemplate"] for t in c.call("resources/templates/list")["result"]["resourceTemplates"]}
        self.assertEqual({t.split("://")[0] for t in tpls}, {"memory"})
        self.assertEqual(c.call("resources/subscribe", {"uri": "dyn://counter"})["error"]["code"], -32003)
        self.assertEqual(c.call("completion/complete", {"ref": {"type": "ref/resource", "uri": "country://{iso2}"}, "argument": {"name": "iso2", "value": "A"}})["error"]["code"], -32003)
        self.assertEqual(c.call("prompts/get", {"name": "country_briefing", "arguments": {"country": "AR"}})["error"]["code"], -32003)
        names = {p["name"] for p in c.call("prompts/list")["result"]["prompts"]}
        self.assertEqual(names, {"memory_digest", "recall_and_summarize"})

    def test_write_scope_needed_to_mutate(self):
        c = self.client("mcp data:read")
        self.assertEqual(c.tool("counter_bump", {})["error"]["code"], -32003)
        self.assertEqual(c.tool("glossary_add", {"term": "x", "definition": "y"})["error"]["code"], -32003)
        self.assertFalse(c.tool("unit_convert", {"value": 1, "from": "m", "to": "cm"})["result"]["isError"])
        self.assertEqual(read(c, "dyn://counter")["result"]["contents"][0]["uri"], "dyn://counter")

    def test_audit_resource_needs_audit_scope(self):
        c = self.client("mcp data:read memory:read")
        self.assertEqual(read(c, "audit://chain")["error"]["code"], -32003)


class DatasetTools(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.c = InProc(make_server(self.data_dir, auth=False).new_session("t"))
        self.c.init()

    def test_country_tools(self):
        for q in ("Argentina", "ar", "ARG", " argentina "):
            self.assertEqual(tool(self.c, "country_lookup", {"query": q})[1]["country"]["iso2"], "AR", q)
        self.assertTrue(tool(self.c, "country_lookup", {"query": "Atlantis"})[0]["isError"])
        out = tool(self.c, "country_search", {"region": "South America", "sort": "population", "limit": 3})[1]
        self.assertEqual([c["iso2"] for c in out["countries"]], ["BR", "CO", "AR"])
        self.assertEqual(out["count"], 7)
        out = tool(self.c, "country_search", {"language": "spanish", "min_population_millions": 100})[1]
        self.assertEqual([c["iso2"] for c in out["countries"]], ["MX"])
        out = tool(self.c, "country_search", {"text": "land", "limit": 50})[1]
        self.assertTrue({"Finland", "Ireland", "Netherlands", "New Zealand", "Switzerland", "Poland"} <= {c["name"] for c in out["countries"]})
        self.assertEqual(tool(self.c, "country_search", {"region": "Mars"})[1].startswith("Invalid arguments"), True)

    def test_element_tools(self):
        for q in ("Fe", "iron", "26", "IRON"):
            self.assertEqual(tool(self.c, "element_lookup", {"query": q})[1]["element"]["symbol"], "Fe", q)
        self.assertTrue(tool(self.c, "element_lookup", {"query": "Uranium"})[0]["isError"])
        out = tool(self.c, "element_search", {"category": "halogen"})[1]
        self.assertEqual([e["symbol"] for e in out["elements"]], ["F", "Cl", "Br"])
        out = tool(self.c, "element_search", {"period": 2, "min_mass": 10, "max_mass": 16})[1]
        self.assertEqual([e["symbol"] for e in out["elements"]], ["B", "C", "N", "O"])
        self.assertEqual(tool(self.c, "element_search", {"group": 18})[1]["count"], 4)

    def test_unit_tools(self):
        out = tool(self.c, "unit_convert", {"value": 5, "from": "km", "to": "mi"})[1]
        self.assertAlmostEqual(out["result"], 3.10686, places=4)
        self.assertEqual(out["category"], "length")
        self.assertTrue(tool(self.c, "unit_convert", {"value": 1, "from": "kg", "to": "m"})[0]["isError"])
        self.assertTrue(tool(self.c, "unit_convert", {"value": -1, "from": "K", "to": "C"})[0]["isError"])
        self.assertEqual(sorted(tool(self.c, "unit_list")[1]["categories"]), list(datasets.UNIT_CATEGORIES))
        self.assertEqual(tool(self.c, "unit_list", {"category": "mass"})[1]["base_unit"], "kg")

    def test_glossary_tools(self):
        self.assertEqual(tool(self.c, "glossary_lookup", {"term": "URI"})[1]["entry"]["term"], "uri")
        out = tool(self.c, "glossary_search", {"query": "re"})[1]
        terms = [r["term"] for r in out["results"]]
        self.assertEqual(terms[0], "resource", "prefix matches rank first")
        self.assertEqual(out["count"] >= len(terms), True)
        self.assertEqual(tool(self.c, "glossary_search", {"query": "zzzzqq"})[1]["count"], 0)
        self.assertTrue(tool(self.c, "glossary_lookup", {"term": "nothing"})[0]["isError"])


if __name__ == "__main__":
    unittest.main()
