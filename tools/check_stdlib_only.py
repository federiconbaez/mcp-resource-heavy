#!/usr/bin/env python3
"""Prove (mechanically) that this repository imports nothing but the Python standard library.

Method: parse every .py file with ``ast`` (nothing is imported or executed), collect every
import statement, and classify each top-level module name as

    stdlib  - listed in ``sys.stdlib_module_names`` of the interpreter running this script
    local   - a package/module that exists inside this repository
    THIRD-PARTY - anything else  -> the check FAILS

It also fails on dynamic-import / code-execution escape hatches (``__import__``, ``importlib``,
``exec``, ``eval``), on dependency manifests (requirements.txt, pyproject.toml, setup.py, ...) and on
vendored directories. It prints line counts and, when a .git directory is present, the commit history.

Usage:  python3 tools/check_stdlib_only.py [--root DIR] [--json]
Exit code 0 = stdlib-only, 1 = violation.
"""

import argparse
import ast
import json
import os
import subprocess
import sys

SKIP_DIRS = {".git", "__pycache__", ".mcpres", "node_modules", ".venv", "venv", "dist"}
DEP_MANIFESTS = {"requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
                 "poetry.lock", "environment.yml", "package.json"}
DYNAMIC_CALLS = {"__import__", "exec", "eval"}


def iter_py(root):
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(base, f)


def local_names(root):
    names = set()
    for entry in os.listdir(root):
        full = os.path.join(root, entry)
        if entry in SKIP_DIRS:
            continue
        if os.path.isdir(full) and any(f.endswith(".py") for _, _, fs in os.walk(full) for f in fs):
            names.add(entry)
        elif entry.endswith(".py"):
            names.add(entry[:-3])
    return names


def analyse_file(path, locals_, stdlib):
    with open(path, "rb") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=path)
    imports, problems = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [(a.name.split(".")[0], a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                imports.append(("local", "." * node.level + (node.module or ""), node.lineno))
                continue
            mods = [((node.module or "").split(".")[0], node.module or "", node.lineno)]
        else:
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
                if name in DYNAMIC_CALLS:
                    problems.append("%s:%d dynamic code/import call %s()" % (path, node.lineno, name))
            continue
        for top, full, lineno in mods:
            if top == "importlib" or top == "pkgutil":
                problems.append("%s:%d imports %s (dynamic import machinery)" % (path, lineno, top))
            if top in stdlib:
                imports.append(("stdlib", full, lineno))
            elif top in locals_:
                imports.append(("local", full, lineno))
            else:
                imports.append(("THIRD-PARTY", full, lineno))
                problems.append("%s:%d third-party import %r" % (path, lineno, full))
    text = src.decode("utf-8")
    lines = text.splitlines()
    blank = sum(1 for l in lines if not l.strip())
    comment = sum(1 for l in lines if l.strip().startswith("#"))
    return {"imports": imports, "problems": problems,
            "lines": {"total": len(lines), "blank": blank, "comment": comment, "code": len(lines) - blank - comment}}


def git_history(root):
    if not os.path.isdir(os.path.join(root, ".git")):
        return None
    try:
        out = subprocess.run(["git", "-c", "safe.directory=*", "log", "--reverse", "--format=%h|%an|%aI|%s", "--shortstat"],
                             cwd=root, capture_output=True, text=True, timeout=30)
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    stdlib = set(sys.stdlib_module_names)
    locals_ = local_names(root)
    files, problems = {}, []
    for path in iter_py(root):
        rel = os.path.relpath(path, root)
        res = analyse_file(path, locals_, stdlib)
        files[rel] = res
        problems += [p.replace(path, rel) for p in res["problems"]]
    for base, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in fs:
            if f in DEP_MANIFESTS:
                problems.append("dependency manifest present: %s" % os.path.relpath(os.path.join(base, f), root))
        for d in dirs:
            if d in ("vendor", "third_party", "site-packages", "lib"):
                problems.append("vendored directory present: %s" % os.path.relpath(os.path.join(base, d), root))
    std_used = sorted({i[1].split(".")[0] for r in files.values() for i in r["imports"] if i[0] == "stdlib"})
    totals = {k: sum(r["lines"][k] for r in files.values()) for k in ("total", "blank", "comment", "code")}
    report = {"python": sys.version.split()[0], "root": root, "files": len(files), "lines": totals,
              "stdlib_modules_used": std_used, "third_party_imports": [p for p in problems if "third-party" in p],
              "problems": problems, "ok": not problems}
    if args.json:
        print(json.dumps(dict(report, per_file={k: v["lines"] for k, v in files.items()}), indent=2))
        return 0 if report["ok"] else 1
    print("STDLIB-ONLY CHECK (ast-based; nothing was imported or executed)")
    print("python %s, %d stdlib modules known to this interpreter" % (report["python"], len(stdlib)))
    print("")
    print("%-34s %6s %6s  %s" % ("file", "code", "total", "imports (stdlib/local)"))
    for rel, r in files.items():
        std = sorted({i[1].split(".")[0] for i in r["imports"] if i[0] == "stdlib"})
        loc = sorted({i[1] for i in r["imports"] if i[0] == "local"})
        print("%-34s %6d %6d  %s%s" % (rel, r["lines"]["code"], r["lines"]["total"], ",".join(std),
                                       ("  | local: " + ",".join(loc)) if loc else ""))
    print("")
    print("files: %d   lines: total=%d code=%d comment=%d blank=%d" % (
        len(files), totals["total"], totals["code"], totals["comment"], totals["blank"]))
    print("stdlib modules used (%d): %s" % (len(std_used), ", ".join(std_used)))
    hist = git_history(root)
    if hist:
        print("")
        print("GIT HISTORY (oldest first):")
        print(hist.rstrip())
    print("")
    if problems:
        print("RESULT: FAIL")
        for p in problems:
            print("  - " + p)
        return 1
    print("RESULT: PASS - zero third-party imports, no dynamic imports/exec/eval, no dependency manifests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
