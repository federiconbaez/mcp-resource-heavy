"""The resource space: static, templated, dynamic and binary resources, their URIs, listing, reading and classification."""

import base64
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from . import datasets
from .userdata import UserDataError, normalise_term

SCOPE_DATA = "data:read"
SCOPE_MEMORY = "memory:read"
SCOPE_AUDIT = "audit:read"

JSON_MIME = "application/json"

TEMPLATES = (
    {"uriTemplate": "country://{iso2}", "name": "country", "title": "Country by ISO 3166-1 alpha-2 code",
     "description": "Approximate, illustrative country facts (dataset 'countries').", "mimeType": JSON_MIME},
    {"uriTemplate": "element://{symbol}", "name": "element", "title": "Chemical element by symbol or atomic number (Z=1..36)",
     "description": "Symbol is case-sensitive (Co is cobalt, CO is not an element); a decimal atomic number also works.", "mimeType": JSON_MIME},
    {"uriTemplate": "unit://{category}", "name": "unit-category", "title": "Unit table of a measurement category",
     "description": "Conversion factors to the category's base unit.", "mimeType": JSON_MIME},
    {"uriTemplate": "glossary://term/{term}", "name": "glossary-term", "title": "Glossary term (built-in or user-defined)",
     "description": "URL-encode spaces as %20.", "mimeType": JSON_MIME},
    {"uriTemplate": "blob://{name}", "name": "blob", "title": "Binary blob by name",
     "description": "Returned as a base64 `blob` content item.", "mimeType": "application/octet-stream"},
    {"uriTemplate": "memory://entry/{id}", "name": "memory-entry", "title": "Memory entry by id",
     "description": "One stored memory.", "mimeType": JSON_MIME},
    {"uriTemplate": "memory://tag/{tag}", "name": "memory-by-tag", "title": "Memories with a tag",
     "description": "Newest memories carrying a tag (URL-encode the tag).", "mimeType": JSON_MIME},
)

STATIC_DOCS = {
    "static://about": ("about", "About this server", "text/markdown", datasets.ABOUT_TEXT),
    "static://dataset-notice": ("dataset-notice", "Dataset provenance notice", "text/plain", datasets.DATASET_NOTE + "\n"),
    "static://schema/country": ("country-schema", "Shape of a country record", JSON_MIME, json.dumps(
        {"type": "object", "required": ["name", "iso2", "iso3", "capital", "region", "population_millions", "area_thousand_km2", "languages", "currency"],
         "properties": {"name": "string", "iso2": "string(2)", "iso3": "string(3)", "capital": "string", "region": "string",
                        "population_millions": "number (approx)", "area_thousand_km2": "number (approx)",
                        "languages": "array<string>", "currency": "string(3)"}}, indent=2, sort_keys=True)),
}

_ISO2 = re.compile(r"^country://([A-Za-z]{2})$")
_ELEMENT = re.compile(r"^element://([A-Za-z]{1,2}|[0-9]{1,3})$")
_UNIT = re.compile(r"^unit://([a-z]{1,32})$")
_TERM = re.compile(r"^glossary://term/([^/?#]{1,200})$")
_BLOB = re.compile(r"^blob://([A-Za-z0-9._-]{1,64})$")
_ENTRY = re.compile(r"^memory://entry/([0-9]{1,18})$")
_TAGURI = re.compile(r"^memory://tag/([^/?#]{1,96})$")
DATA_SETS = ("countries", "elements", "units", "glossary")


def term_uri(term):
    return "glossary://term/" + quote(term, safe="")


def now_iso(clock):
    return datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ResourceSpace:
    def __init__(self, memory, userdata, audit_view, clock=time.time):
        self.memory, self.user, self.audit_view, self.clock = memory, userdata, audit_view, clock

    # ------------------------------------------------------------------------- classify
    def classify(self, uri):
        """Return (kind, scope, arg) for a URI that has a valid *shape*, else None. Existence is checked by ``read``."""
        if uri in STATIC_DOCS:
            return "static", SCOPE_DATA, uri
        if uri.startswith("data://") and uri[7:] in DATA_SETS:
            return "dataset", SCOPE_DATA, uri[7:]
        m = _ISO2.match(uri)
        if m:
            return "country", SCOPE_DATA, m.group(1).upper()
        m = _ELEMENT.match(uri)
        if m:
            return "element", SCOPE_DATA, m.group(1)
        m = _UNIT.match(uri)
        if m:
            return "unit", SCOPE_DATA, m.group(1)
        if uri == "glossary://user":
            return "glossary_user", SCOPE_DATA, None
        if uri == "glossary://all":
            return "glossary_all", SCOPE_DATA, None
        m = _TERM.match(uri)
        if m:
            return "term", SCOPE_DATA, unquote(m.group(1))
        if uri in ("dyn://counter", "dyn://clock"):
            return uri[6:], SCOPE_DATA, None
        m = _BLOB.match(uri)
        if m:
            return "blob", SCOPE_DATA, m.group(1)
        if uri == "memory://index":
            return "memory_index", SCOPE_MEMORY, None
        m = _ENTRY.match(uri)
        if m:
            return "entry", SCOPE_MEMORY, int(m.group(1))
        m = _TAGURI.match(uri)
        if m:
            return "tag", SCOPE_MEMORY, unquote(m.group(1))
        if uri == "audit://chain":
            return "audit", SCOPE_AUDIT, None
        return None

    # ------------------------------------------------------------------------- listing
    def list_all(self, has_scope):
        out = []
        if has_scope(SCOPE_DATA):
            for uri, (name, title, mime, _) in STATIC_DOCS.items():
                out.append({"uri": uri, "name": name, "title": title, "mimeType": mime, "description": "Static document."})
            for ds in DATA_SETS:
                out.append({"uri": "data://" + ds, "name": "dataset-" + ds, "title": "Whole %s dataset" % ds, "mimeType": JSON_MIME,
                            "description": "Static dataset (approximate, illustrative)."})
            for c in datasets.COUNTRIES:
                out.append({"uri": "country://" + c["iso2"], "name": "country-" + c["iso2"].lower(), "title": c["name"], "mimeType": JSON_MIME})
            for e in datasets.ELEMENTS:
                out.append({"uri": "element://" + e["symbol"], "name": "element-" + e["symbol"].lower(), "title": "%s (Z=%d)" % (e["name"], e["number"]),
                            "mimeType": JSON_MIME})
            for cat in datasets.UNIT_CATEGORIES:
                out.append({"uri": "unit://" + cat, "name": "units-" + cat, "title": "Units: " + cat, "mimeType": JSON_MIME})
            for g in self.user.all_terms():
                out.append({"uri": term_uri(g["term"]), "name": "term-" + g["term"].replace(" ", "-"), "title": g["term"], "mimeType": JSON_MIME,
                            "description": "Glossary term" + ("" if g["builtin"] else " (user-defined)")})
            out.append({"uri": "glossary://user", "name": "glossary-user", "title": "User-defined glossary terms (dynamic)", "mimeType": JSON_MIME})
            out.append({"uri": "glossary://all", "name": "glossary-all", "title": "Built-in plus user glossary (dynamic)", "mimeType": JSON_MIME})
            out.append({"uri": "dyn://counter", "name": "dyn-counter", "title": "Persistent counter (changes with counter_bump)", "mimeType": JSON_MIME})
            out.append({"uri": "dyn://clock", "name": "dyn-clock", "title": "Server clock (different on every read)", "mimeType": JSON_MIME})
            for name, b in datasets.BLOBS.items():
                out.append({"uri": "blob://" + name, "name": "blob-" + name.replace(".", "-"), "title": b["title"], "mimeType": b["mime"],
                            "size": len(b["data"])})
        if has_scope(SCOPE_MEMORY):
            out.append({"uri": "memory://index", "name": "memory-index", "title": "Memory index", "mimeType": JSON_MIME,
                        "description": "Counts, tag histogram and recent entries."})
            _, entries = self.memory.list(limit=100)
            for e in entries:
                out.append({"uri": "memory://entry/%d" % e["id"], "name": "memory-%d" % e["id"], "description": "Memory #%d" % e["id"], "mimeType": JSON_MIME})
        if has_scope(SCOPE_AUDIT):
            out.append({"uri": "audit://chain", "name": "audit-chain", "title": "Audit chain status", "mimeType": JSON_MIME,
                        "description": "Hash-chain verification result, contract counters and recent audit entries."})
        return out

    # ------------------------------------------------------------------------- reading
    def read(self, kind, arg, uri):
        """Return a ``contents`` item, or None when the (well-formed) URI names nothing that exists."""
        def text(body, mime=JSON_MIME):
            return {"uri": uri, "mimeType": mime, "text": body if isinstance(body, str) else json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False)}

        if kind == "static":
            _, _, mime, body = STATIC_DOCS[arg]
            return text(body, mime)
        if kind == "dataset":
            if arg == "countries":
                return text({"note": datasets.DATASET_NOTE, "countries": list(datasets.COUNTRIES)})
            if arg == "elements":
                return text({"note": datasets.DATASET_NOTE, "elements": list(datasets.ELEMENTS)})
            if arg == "units":
                return text({"note": datasets.DATASET_NOTE, "categories": {c: datasets.unit_table(c) for c in datasets.UNIT_CATEGORIES}})
            return text({"note": datasets.DATASET_NOTE, "glossary": list(datasets.GLOSSARY)})
        if kind == "country":
            c = datasets.COUNTRY_BY_ISO2.get(arg)
            return None if c is None else text(c)
        if kind == "element":
            e = datasets.ELEMENT_BY_SYMBOL.get(arg)
            if e is None and arg.isdigit():
                e = next((x for x in datasets.ELEMENTS if x["number"] == int(arg)), None)
            return None if e is None else text(e)
        if kind == "unit":
            return text(datasets.unit_table(arg)) if arg in datasets.UNITS else None
        if kind == "term":
            g = self.user.get_term(arg)
            return None if g is None else text(g)
        if kind == "glossary_user":
            return text({"terms": self.user.user_terms()})
        if kind == "glossary_all":
            return text({"terms": self.user.all_terms()})
        if kind == "counter":
            return text(self.user.counter())
        if kind == "clock":
            return text({"utc": now_iso(self.clock), "epoch": round(self.clock(), 3)})
        if kind == "blob":
            b = datasets.BLOBS.get(arg)
            if b is None:
                return None
            return {"uri": uri, "mimeType": b["mime"], "blob": base64.b64encode(b["data"]).decode("ascii")}
        if kind == "memory_index":
            return text(self.memory.index())
        if kind == "entry":
            e = self.memory.get(arg)
            return None if e is None else text(e)
        if kind == "tag":
            _, entries = self.memory.list(limit=50, tag=arg)
            return text({"tag": arg, "entries": entries})
        if kind == "audit":
            return text(self.audit_view())
        return None

    # ------------------------------------------------------------------------- which URIs change when
    @staticmethod
    def uris_for_term(term):
        try:
            t = normalise_term(term)
        except UserDataError:
            return []
        return [term_uri(t), "glossary://user", "glossary://all"]
