import unittest

from tests import helpers  # noqa: F401
from mcpres import schema
from mcpres.catalog import Catalog
from mcpres.memory import MemoryStore


def v(inst, sch):
    return [str(x) for x in schema.validate(inst, sch)]


class SchemaTests(unittest.TestCase):
    def test_types(self):
        self.assertEqual(v("a", {"type": "string"}), [])
        self.assertTrue(v(1, {"type": "string"}))
        self.assertEqual(v(1, {"type": "integer"}), [])
        self.assertEqual(v(2.0, {"type": "integer"}), [])
        self.assertTrue(v(2.5, {"type": "integer"}))
        self.assertTrue(v(True, {"type": "integer"}), "bool is not an integer")
        self.assertTrue(v(True, {"type": "number"}), "bool is not a number")
        self.assertEqual(v(None, {"type": "null"}), [])
        self.assertEqual(v(1, {"type": ["string", "integer"]}), [])
        self.assertTrue(v([], {"type": "object"}))
        self.assertTrue(v({}, {"type": "array"}))

    def test_required_and_additional(self):
        s = {"type": "object", "required": ["a"], "additionalProperties": False, "properties": {"a": {"type": "string"}}}
        self.assertEqual(v({"a": "x"}, s), [])
        self.assertIn("missing required property 'a'", v({}, s)[0])
        self.assertIn("additional property not allowed", v({"a": "x", "b": 1}, s)[0])
        self.assertEqual(v({"a": "x", "b": 1}, {"type": "object", "properties": {"a": {}}}), [])
        self.assertTrue(v({"a": "x", "b": 1}, {"type": "object", "additionalProperties": {"type": "string"}}))

    def test_enum_const(self):
        self.assertEqual(v("a", {"enum": ["a", "b"]}), [])
        self.assertTrue(v("c", {"enum": ["a", "b"]}))
        self.assertTrue(v(True, {"enum": [1]}), "True must not equal 1")
        self.assertEqual(v(1.0, {"enum": [1]}), [])
        self.assertEqual(v(3, {"const": 3}), [])
        self.assertTrue(v(4, {"const": 3}))

    def test_string_and_number_bounds(self):
        s = {"type": "string", "minLength": 2, "maxLength": 4}
        self.assertTrue(v("a", s))
        self.assertEqual(v("abcd", s), [])
        self.assertTrue(v("abcde", s))
        n = {"type": "integer", "minimum": 1, "maximum": 5}
        self.assertTrue(v(0, n))
        self.assertEqual(v(5, n), [])
        self.assertTrue(v(6, n))
        self.assertEqual(v(len("\U0001F600"), {"minimum": 1}), [])
        self.assertTrue(v("ab\n", {"type": "string", "pattern": "^[a-z]+$"}), "$ must not match before trailing newline")

    def test_items_and_paths(self):
        s = {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "string"}}
        self.assertTrue(v([], s))
        self.assertEqual(v(["a"], s), [])
        self.assertTrue(v(["a", "b", "c"], s))
        errs = v(["a", 5], s)
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0].startswith("$[1]"))
        nested = {"type": "object", "properties": {"x": {"type": "array", "items": {"type": "integer"}}}}
        self.assertTrue(v({"x": [1, "z"]}, nested)[0].startswith("$.x[1]"))

    def test_error_cap(self):
        errs = schema.validate(list(range(100)), {"type": "array", "items": {"type": "string"}})
        self.assertLessEqual(len(errs), schema.MAX_ERRORS)

    def test_check_schema_rejects_unknown_keywords(self):
        with self.assertRaises(schema.SchemaError):
            schema.check_schema({"type": "object", "additionalProperites": False})
        with self.assertRaises(schema.SchemaError):
            schema.check_schema({"type": "strng"})
        with self.assertRaises(schema.SchemaError):
            schema.check_schema({"minLength": -1})
        with self.assertRaises(schema.SchemaError):
            schema.check_schema({"pattern": "("})
        schema.check_schema({"type": "object", "properties": {"a": {"type": "string", "description": "x"}}})

    def test_all_tool_schemas_are_valid_and_strict(self):
        import tempfile
        d = tempfile.mkdtemp()
        cat = Catalog(MemoryStore(d + "/m.db"))
        for t in cat.tools.values():
            schema.check_schema(t.input_schema)
            self.assertIs(t.input_schema["additionalProperties"], False, t.name)
        for p in cat.PROMPTS.values():
            schema.check_schema(p["schema"])

    def test_apply_defaults(self):
        s = {"properties": {"a": {"default": 3}, "b": {}}}
        self.assertEqual(schema.apply_defaults({"b": 1}, s), {"a": 3, "b": 1})
        self.assertEqual(schema.apply_defaults({"a": 9}, s), {"a": 9})


if __name__ == "__main__":
    unittest.main()
