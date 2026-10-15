#!/usr/bin/env python3
"""Refresh the machine-generated block of ORIGINALITY.md (between the GENERATED markers).

    python3 tools/gen_originality.py            # rewrite the block in place
    python3 tools/gen_originality.py --print    # print the block only

The block contains, from a LIVE run at generation time: the AST stdlib-only check output (with per-file line counts),
the git history, a forbidden-identifier grep, and a sha256 manifest of the *source* files (package, tests, tools, demo).
Docs and evidence are deliberately outside that source manifest (they embed it, so listing them would be circular);
they are covered by MANIFEST.sha256 instead.
"""
import hashlib
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEGIN, END = "<!-- GENERATED:BEGIN -->", "<!-- GENERATED:END -->"
SKIP = {".git", "__pycache__"}


def sh(*cmd):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).rstrip("\n")


def source_files():
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith(".mcp") and d != "evidence")
        rel_base = os.path.relpath(base, ROOT)
        for f in sorted(files):
            rel = os.path.normpath(os.path.join(rel_base, f))
            if not f.endswith((".py", ".sh")):
                continue
            out.append(rel)
    return sorted(out)


def sha(path):
    return hashlib.sha256(open(os.path.join(ROOT, path), "rb").read()).hexdigest()


def block():
    parts = []
    rc, out = sh(sys.executable, "tools/check_stdlib_only.py")
    parts.append("### AST stdlib-only check (live run, exit code %d)\n\n```\n%s\n```\n" % (rc, out))
    rep = os.path.join(ROOT, "evidence", "adaptation_report.txt")
    if os.path.exists(rep):
        parts.append("### Lines inherited from mcp-base / mcp-forge, per file (`evidence/adaptation_report.txt`)\n\n```\n%s\n```\n" % open(rep).read().rstrip())
    rc, out = sh("git", "log", "--reverse", "--format=%h  %an  %aI  %s")
    rc2, count = sh("git", "rev-list", "--count", "HEAD")
    parts.append("### Git history (%s commits at generation time; later commits only touch docs/evidence)\n\n```\n%s\n```\n" % (count, out))
    pat = re.compile(r"modelcontextprotocol|fastmcp|from mcp\b|import mcp\b", re.I)
    hits = []
    for rel in source_files():
        for n, line in enumerate(open(os.path.join(ROOT, rel), errors="replace"), 1):
            if pat.search(line):
                hits.append("%s:%d: %s" % (rel, n, line.strip()[:100]))
    parts.append("### SDK-identifier scan of source files (`modelcontextprotocol|fastmcp|from mcp|import mcp`)\n\n```\n%s\n```\n" % (
        "\n".join(hits) or "no matches"))
    files = source_files()
    lines = ["%s  %s" % (sha(f), f) for f in files]
    total = sum(sum(1 for _ in open(os.path.join(ROOT, f), errors="replace")) for f in files)
    parts.append("### Source sha256 manifest (%d files, %d lines incl. blanks/comments/tests)\n\n```\n%s\n```\n" % (len(files), total, "\n".join(lines)))
    return "\n".join(parts)


def main():
    text = block()
    if "--print" in sys.argv:
        print(text)
        return 0
    path = os.path.join(ROOT, "ORIGINALITY.md")
    doc = open(path).read()
    if BEGIN not in doc or END not in doc:
        print("ORIGINALITY.md has no GENERATED markers", file=sys.stderr)
        return 1
    head, rest = doc.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    open(path, "w").write(head + BEGIN + "\n" + text + "\n" + END + tail)
    print("ORIGINALITY.md generated block refreshed (%d bytes)" % len(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
