#!/usr/bin/env python3
"""Verify that the file / test / conformance-check references written in the docs actually exist.

    python3 tools/check_doc_refs.py            # checks README.md, AUDIT.md, ORIGINALITY.md, SECURITY.md

Recognised backticked tokens:
    path/to/file.ext            -> the file must exist
    path/to/test_x.py::name     -> the file must contain `def name` or `class name`
    check:ABC-001               -> the package conformance module must contain @check("ABC-001"
A broken reference makes the script exit 1 - the requirement->evidence maps cannot silently rot.
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ("README.md", "AUDIT.md", "ORIGINALITY.md", "SECURITY.md")
FILE_RE = re.compile(r"^[A-Za-z0-9_./-]+\.(py|sh|md|json|sha256|txt)$")


def main():
    conf = ""
    for path in glob.glob(os.path.join(ROOT, "mcp*", "conformance.py")):
        conf = open(path).read()
    bad, total = [], 0
    for doc in DOCS:
        p = os.path.join(ROOT, doc)
        if not os.path.exists(p):
            continue
        for token in re.findall(r"`([^`\n]+)`", open(p).read()):
            token = token.strip()
            if token.startswith("check:"):
                total += 1
                if '@check("%s"' % token[6:] not in conf:
                    bad.append("%s: unknown conformance check %s" % (doc, token))
                continue
            m = token.split("::")
            if not FILE_RE.match(m[0]) or "*" in m[0] or m[0].startswith(("http", "/")):
                continue
            if m[0].startswith(("evidence/", "dist/")) or "<" in m[0] or m[0] in ("MANIFEST.sha256", "keys.json"):
                continue
            total += 1
            f = os.path.join(ROOT, m[0])
            if not os.path.isfile(f):
                bad.append("%s: missing file %s" % (doc, m[0]))
                continue
            if len(m) == 2:
                text = open(f).read()
                if not re.search(r"(def|class) %s\b" % re.escape(m[1]), text):
                    bad.append("%s: %s has no def/class %s" % (doc, m[0], m[1]))
    for b in bad:
        print("BROKEN REFERENCE:", b)
    print("doc reference check: %d references, %d broken" % (total, len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
