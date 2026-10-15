import base64
import json
import unittest

from mcpres import datasets
from tests.helpers import InProc, TempDirTestCase, make_server


class Base(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.c = InProc(make_server(self.data_dir, auth=False).new_session("t"))
        self.c.init()

    def prompt(self, name, args=None):
        return self.c.call("prompts/get", {"name": name, "arguments": args or {}})

    def complete(self, ref, name, value, context=None):
        p = {"ref": ref, "argument": {"name": name, "value": value}}
        if context:
            p["context"] = context
        return self.c.call("completion/complete", p)


def P(name):
    return {"type": "ref/prompt", "name": name}


def R(uri):
    return {"type": "ref/resource", "uri": uri}


class PromptTests(Base):
    def test_listing_declares_arguments(self):
        prompts = {p["name"]: p for p in self.c.call("prompts/list")["result"]["prompts"]}
        self.assertEqual(set(prompts), {"country_briefing", "element_study_guide", "unit_conversion_tutor", "glossary_quiz", "dataset_overview",
                                        "recall_and_summarize", "memory_digest"})
        self.assertEqual([(a["name"], a["required"]) for a in prompts["country_briefing"]["arguments"]],
                         [("country", True), ("audience", False), ("depth", False)])

    def test_country_briefing_is_multi_message_with_embedded_resources(self):
        res = self.prompt("country_briefing", {"country": "Argentina", "audience": "traveler", "depth": "3"})["result"]
        msgs = res["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "user", "user", "assistant", "user"])
        self.assertEqual([m["content"]["type"] for m in msgs], ["text", "resource", "resource", "text", "text"])
        emb = msgs[1]["content"]["resource"]
        self.assertEqual((emb["uri"], emb["mimeType"]), ("country://AR", "application/json"))
        self.assertEqual(json.loads(emb["text"])["capital"], "Buenos Aires")
        self.assertEqual(msgs[2]["content"]["resource"]["uri"], "static://dataset-notice")
        self.assertIn("3 paragraphs", msgs[4]["content"]["text"])
        self.assertIn("traveler", msgs[0]["content"]["text"])
        self.assertIn("Argentina", res["description"])

    def test_country_arg_forms_and_validation(self):
        for form in ("AR", "ar", "ARG", "argentina"):
            self.assertIn("result", self.prompt("country_briefing", {"country": form}), form)
        bad = [({}, "required"), ({"country": ""}, "minLength"), ({"country": "Atlantis"}, "unknown country"),
               ({"country": "AR", "audience": "pirate"}, "audience"), ({"country": "AR", "depth": "4"}, "depth"),
               ({"country": "AR", "depth": 2}, "depth"), ({"country": "AR", "extra": "x"}, "extra"), ({"country": ["AR"]}, "country")]
        for args, needle in bad:
            r = self.prompt("country_briefing", args)
            self.assertEqual(r["error"]["code"], -32602, args)
            self.assertTrue(any(needle.lower() in e.lower() for e in r["error"]["data"]["errors"]), (args, r["error"]["data"]))

    def test_element_guide_embeds_one_resource_per_element_and_dedupes(self):
        res = self.prompt("element_study_guide", {"elements": "Fe, copper,29,Zn", "level": "advanced"})["result"]
        uris = [m["content"]["resource"]["uri"] for m in res["messages"] if m["content"]["type"] == "resource"]
        self.assertEqual(uris, ["element://Fe", "element://Cu", "element://Zn"])
        self.assertIn("6 practice", res["messages"][-1]["content"]["text"])
        errs = self.prompt("element_study_guide", {"elements": "Fe,Xx,Yy"})["error"]["data"]["errors"]
        self.assertEqual(len(errs), 2)
        errs = self.prompt("element_study_guide", {"elements": "H,He,Li,Be,B,C"})["error"]["data"]["errors"]
        self.assertTrue(any("at most 5" in e for e in errs))
        self.assertEqual(self.prompt("element_study_guide", {"elements": "Fe,,Cu"})["error"]["code"], -32602)

    def test_unit_tutor_example_is_computed_and_validated(self):
        res = self.prompt("unit_conversion_tutor", {"category": "length", "value": "10", "from_unit": "km", "to_unit": "mi"})["result"]
        self.assertIn("6.21371 mi", res["messages"][2]["content"]["text"])
        self.assertEqual(res["messages"][1]["content"]["resource"]["uri"], "unit://length")
        self.assertIn("result", self.prompt("unit_conversion_tutor", {"category": "temperature", "value": "100", "from_unit": "C", "to_unit": "F"}))
        for args in ({"category": "length", "from_unit": "kg"}, {"category": "nope"}, {"category": "length", "value": "abc"},
                     {"category": "temperature", "value": "-500", "from_unit": "C", "to_unit": "K"}):
            self.assertEqual(self.prompt("unit_conversion_tutor", args)["error"]["code"], -32602, args)

    def test_quiz_deterministic_by_seed(self):
        a = self.prompt("glossary_quiz", {"count": "4", "seed": "7"})["result"]
        b = self.prompt("glossary_quiz", {"count": "4", "seed": "7"})["result"]
        c = self.prompt("glossary_quiz", {"count": "4", "seed": "8"})["result"]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        terms = [m["content"]["resource"]["uri"] for m in a["messages"] if m["content"]["type"] == "resource"]
        self.assertEqual((len(terms), len(set(terms))), (4, 4))
        for bad in ({"count": "0"}, {"count": "11"}, {"seed": "-1"}, {"seed": "x"}):
            self.assertEqual(self.prompt("glossary_quiz", bad)["error"]["code"], -32602, bad)

    def test_overview_contains_text_resources_and_an_image(self):
        msgs = self.prompt("dataset_overview")["result"]["messages"]
        kinds = [m["content"]["type"] for m in msgs]
        self.assertEqual(kinds, ["text", "resource", "resource", "image", "text"])
        img = msgs[3]["content"]
        self.assertEqual(img["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(img["data"]), datasets.BLOBS["sample.png"]["data"])
        self.assertEqual(self.prompt("dataset_overview", {"x": "y"})["error"]["code"], -32602)

    def test_unknown_prompt(self):
        self.assertEqual(self.prompt("nope")["error"]["code"], -32602)

    def test_embedded_user_terms_in_quiz_are_never_included(self):
        self.c.tool("glossary_add", {"term": "secret sauce", "definition": "private."})
        for seed in range(30):
            res = self.prompt("glossary_quiz", {"count": "10", "seed": str(seed)})["result"]
            self.assertNotIn("secret", json.dumps(res))


class CompletionTests(Base):
    def values(self, r):
        return r["result"]["completion"]["values"]

    def test_prompt_argument_completion(self):
        v = self.values(self.complete(P("country_briefing"), "country", "ar"))
        self.assertEqual(v, ["AR", "Argentina"])
        self.assertEqual(self.values(self.complete(P("country_briefing"), "country", "United")), ["United Kingdom", "United States"])
        self.assertEqual(self.values(self.complete(P("country_briefing"), "audience", "")), ["student", "traveler", "analyst"])
        self.assertEqual(self.values(self.complete(P("country_briefing"), "audience", "T")), ["traveler"])
        self.assertEqual(self.values(self.complete(P("country_briefing"), "country", "zzz")), [])
        self.assertEqual(self.values(self.complete(P("glossary_quiz"), "count", "1")), ["1", "10"])
        self.assertEqual(self.values(self.complete(P("unit_conversion_tutor"), "category", "m")), ["mass"])

    def test_comma_list_completion_and_context(self):
        v = self.values(self.complete(P("element_study_guide"), "elements", "Fe,C"))
        self.assertTrue(all(x.startswith("Fe,C") for x in v))
        self.assertTrue({"Fe,Ca", "Fe,Cl", "Fe,Co", "Fe,Cu", "Fe,Carbon", "Fe,Calcium"} <= set(v))
        self.assertNotIn("Fe,Fe", self.values(self.complete(P("element_study_guide"), "elements", "Fe,F")), "already chosen elements are not suggested again")
        r = self.values(self.complete(P("unit_conversion_tutor"), "from_unit", "", {"arguments": {"category": "mass"}}))
        self.assertEqual(r, ["mg", "g", "kg", "t", "oz", "lb", "st"])
        every = self.values(self.complete(P("unit_conversion_tutor"), "to_unit", "k"))
        self.assertIn("km", every)
        self.assertIn("kg", every)

    def test_template_completion(self):
        self.assertEqual(self.values(self.complete(R("country://{iso2}"), "iso2", "A")), ["AR", "AT", "AU"])
        self.assertEqual(self.values(self.complete(R("element://{symbol}"), "symbol", "C")), ["C", "Ca", "Cl", "Co", "Cr", "Cu"])
        self.assertEqual(self.values(self.complete(R("element://{symbol}"), "symbol", "3")), ["3", "30", "31", "32", "33", "34", "35", "36"])
        self.assertEqual(self.values(self.complete(R("unit://{category}"), "category", "te")), ["temperature"])
        self.assertEqual(self.values(self.complete(R("blob://{name}"), "name", "s")), ["sample.png"])
        self.assertIn("mime type", self.values(self.complete(R("glossary://term/{term}"), "term", "mi")))
        self.c.tool("glossary_add", {"term": "milestone", "definition": "A mark."})
        self.assertIn("milestone", self.values(self.complete(R("glossary://term/{term}"), "term", "mi")))
        self.c.tool("memory_store", {"content": "x", "tags": ["alpha", "beta"]})
        self.assertEqual(self.values(self.complete(R("memory://tag/{tag}"), "tag", "al")), ["alpha"])
        self.assertEqual(self.values(self.complete(R("memory://entry/{id}"), "id", "")), ["1"])

    def test_result_shape_and_limits(self):
        r = self.complete(P("country_briefing"), "country", "")["result"]["completion"]
        self.assertEqual(set(r), {"values", "total", "hasMore"})
        self.assertEqual(r["total"], 2 * len(datasets.COUNTRIES))
        self.assertLessEqual(len(r["values"]), 100)
        self.assertFalse(r["hasMore"])
        for i in range(120):
            self.c.tool("glossary_add", {"term": "term %03d" % i, "definition": "d."})
        r = self.complete(R("glossary://term/{term}"), "term", "term")["result"]["completion"]
        self.assertEqual((len(r["values"]), r["total"], r["hasMore"]), (100, 120, True))

    def test_invalid_requests_are_32602(self):
        bad = [
            {"ref": P("nope"), "argument": {"name": "x", "value": ""}},
            {"ref": P("country_briefing"), "argument": {"name": "nope", "value": ""}},
            {"ref": R("nope://{x}"), "argument": {"name": "x", "value": ""}},
            {"ref": R("country://{iso2}"), "argument": {"name": "code", "value": ""}},
            {"ref": {"type": "ref/other", "name": "x"}, "argument": {"name": "x", "value": ""}},
            {"ref": {"type": "ref/prompt"}, "argument": {"name": "x", "value": ""}},
            {"ref": {"type": "ref/prompt", "name": "country_briefing", "uri": "x"}, "argument": {"name": "country", "value": ""}},
            {"ref": {"type": "ref/resource", "uri": "country://{iso2}", "name": "x"}, "argument": {"name": "iso2", "value": ""}},
            {"ref": P("country_briefing"), "argument": {"name": "country"}},
            {"ref": P("country_briefing"), "argument": {"name": "country", "value": 5}},
            {"ref": P("country_briefing"), "argument": {"name": "country", "value": "x" * 300}},
            {"argument": {"name": "country", "value": ""}},
            {"ref": P("country_briefing")},
            {"ref": P("country_briefing"), "argument": {"name": "country", "value": ""}, "bogus": 1},
        ]
        for params in bad:
            self.assertEqual(self.c.call("completion/complete", params)["error"]["code"], -32602, params)
        self.assertEqual(self.c.call("completion/complete", [])["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
