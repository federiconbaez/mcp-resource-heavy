"""A deliberately small, strict JSON Schema validator written by hand.

Supported keywords (anything else is rejected by ``check_schema`` so that a
typo in a tool schema can never silently weaken validation):

    type (string or list), enum, const, required, properties,
    additionalProperties (bool or schema), minLength, maxLength, pattern,
    minimum, maximum, items (schema), minItems, maxItems,
    plus the annotation keywords: title, description, default, examples, $schema

``validate(instance, schema)`` returns a list of ``Violation`` (empty = valid).
"""

import re

ANNOTATION_KEYWORDS = {"title", "description", "default", "examples", "$schema"}
VALIDATION_KEYWORDS = {
    "type", "enum", "const", "required", "properties", "additionalProperties",
    "minLength", "maxLength", "pattern", "minimum", "maximum", "items",
    "minItems", "maxItems",
}
KNOWN_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}
MAX_ERRORS = 20
MAX_DEPTH = 32


class SchemaError(ValueError):
    """The *schema itself* is malformed or uses an unsupported keyword."""


class Violation:
    __slots__ = ("path", "message")

    def __init__(self, path, message):
        self.path = path
        self.message = message

    def __str__(self):
        return "%s: %s" % (self.path, self.message)

    def __repr__(self):
        return "Violation(%r, %r)" % (self.path, self.message)


def _is_type(value, name):
    # bool is a subclass of int in Python; JSON booleans are never numbers.
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and value.is_integer()
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def check_schema(schema, _path="$", _depth=0):
    """Raise SchemaError unless ``schema`` only uses supported, well-formed keywords."""
    if _depth > MAX_DEPTH:
        raise SchemaError("%s: schema nested too deeply" % _path)
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise SchemaError("%s: schema must be an object or boolean" % _path)
    for key in schema:
        if key not in ANNOTATION_KEYWORDS and key not in VALIDATION_KEYWORDS:
            raise SchemaError("%s: unsupported schema keyword %r" % (_path, key))
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not types or any(x not in KNOWN_TYPES for x in types):
            raise SchemaError("%s: bad 'type' %r" % (_path, t))
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise SchemaError("%s: 'enum' must be a non-empty array" % _path)
    if "required" in schema:
        r = schema["required"]
        if not isinstance(r, list) or not all(isinstance(x, str) for x in r):
            raise SchemaError("%s: 'required' must be an array of strings" % _path)
    for kw in ("minLength", "maxLength", "minItems", "maxItems"):
        if kw in schema:
            v = schema[kw]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise SchemaError("%s: %r must be a non-negative integer" % (_path, kw))
    for kw in ("minimum", "maximum"):
        if kw in schema:
            v = schema[kw]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise SchemaError("%s: %r must be a number" % (_path, kw))
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise SchemaError("%s: 'pattern' must be a string" % _path)
        try:
            _compile(schema["pattern"])
        except re.error as exc:
            raise SchemaError("%s: invalid pattern: %s" % (_path, exc))
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict):
            raise SchemaError("%s: 'properties' must be an object" % _path)
        for name, sub in props.items():
            check_schema(sub, "%s.properties.%s" % (_path, name), _depth + 1)
    if "additionalProperties" in schema:
        check_schema(schema["additionalProperties"], _path + ".additionalProperties", _depth + 1)
    if "items" in schema:
        check_schema(schema["items"], _path + ".items", _depth + 1)


def _compile(pattern):
    """ECMA-style anchors: a trailing ``$`` means end of string (Python's ``$`` would also accept a trailing newline)."""
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


def _fmt_path(path, key):
    if isinstance(key, int):
        return "%s[%d]" % (path, key)
    return "%s.%s" % (path, key)


def validate(instance, schema, path="$"):
    """Return a list of Violation for ``instance`` against ``schema``."""
    out = []
    _validate(instance, schema, path, out, 0)
    return out[:MAX_ERRORS]


def _validate(inst, schema, path, out, depth):
    if len(out) >= MAX_ERRORS:
        return
    if depth > MAX_DEPTH:
        out.append(Violation(path, "instance nested too deeply"))
        return
    if schema is True:
        return
    if schema is False:
        out.append(Violation(path, "no value is allowed here"))
        return

    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(inst, x) for x in types):
            out.append(Violation(path, "expected %s, got %s" % (" or ".join(types), _type_name(inst))))
            return  # further keyword checks would be meaningless

    if "const" in schema and not _json_equal(inst, schema["const"]):
        out.append(Violation(path, "must equal %r" % (schema["const"],)))
    if "enum" in schema and not any(_json_equal(inst, e) for e in schema["enum"]):
        out.append(Violation(path, "must be one of %s" % (schema["enum"],)))

    if isinstance(inst, str):
        if "minLength" in schema and len(inst) < schema["minLength"]:
            out.append(Violation(path, "string shorter than minLength %d" % schema["minLength"]))
        if "maxLength" in schema and len(inst) > schema["maxLength"]:
            out.append(Violation(path, "string longer than maxLength %d" % schema["maxLength"]))
        if "pattern" in schema and _compile(schema["pattern"]).search(inst) is None:
            out.append(Violation(path, "string does not match pattern %r" % schema["pattern"]))

    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            out.append(Violation(path, "%r is below minimum %r" % (inst, schema["minimum"])))
        if "maximum" in schema and inst > schema["maximum"]:
            out.append(Violation(path, "%r is above maximum %r" % (inst, schema["maximum"])))

    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            out.append(Violation(path, "array shorter than minItems %d" % schema["minItems"]))
        if "maxItems" in schema and len(inst) > schema["maxItems"]:
            out.append(Violation(path, "array longer than maxItems %d" % schema["maxItems"]))
        if "items" in schema:
            for i, item in enumerate(inst):
                _validate(item, schema["items"], _fmt_path(path, i), out, depth + 1)

    if isinstance(inst, dict):
        for name in schema.get("required", ()):
            if name not in inst:
                out.append(Violation(path, "missing required property %r" % name))
        props = schema.get("properties", {})
        for name, value in inst.items():
            if name in props:
                _validate(value, props[name], _fmt_path(path, name), out, depth + 1)
            elif "additionalProperties" in schema:
                ap = schema["additionalProperties"]
                if ap is False:
                    out.append(Violation(_fmt_path(path, name), "additional property not allowed"))
                elif ap is not True:
                    _validate(value, ap, _fmt_path(path, name), out, depth + 1)


def _json_equal(a, b):
    """JSON equality: True != 1, but 1 == 1.0."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def apply_defaults(instance, schema):
    """Return a shallow copy of an object instance with top-level ``default`` values filled in."""
    if not isinstance(instance, dict) or not isinstance(schema, dict):
        return instance
    out = dict(instance)
    for name, sub in schema.get("properties", {}).items():
        if name not in out and isinstance(sub, dict) and "default" in sub:
            out[name] = sub["default"]
    return out
