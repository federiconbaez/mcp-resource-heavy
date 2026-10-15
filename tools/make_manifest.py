#!/usr/bin/env python3
"""Write / verify a sha256sum-compatible manifest of every file in the repository.

    python3 tools/make_manifest.py            # writes evidence/MANIFEST.sha256
    python3 tools/make_manifest.py --check    # verifies the working tree against it (exit 1 on any difference)

The manifest lists every file except .git/, __pycache__/, .mcpres/ and the manifest itself.
Verify independently with:  cd <repo> && sha256sum -c evidence/MANIFEST.sha256
"""

import argparse
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join("evidence", "MANIFEST.sha256")
SKIP_DIRS = {".git", "__pycache__", ".mcpres"}


def files(root):
    out = []
    for base, dirs, fs in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(fs):
            rel = os.path.relpath(os.path.join(base, f), root)
            if rel == MANIFEST or f.endswith(".pyc"):
                continue
            out.append(rel)
    return sorted(out)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    manifest_path = os.path.join(root, MANIFEST)
    if args.check:
        expected = {}
        with open(manifest_path) as fh:
            for line in fh:
                h, _, rel = line.rstrip("\n").partition("  ")
                expected[rel] = h
        actual = {rel: digest(os.path.join(root, rel)) for rel in files(root)}
        bad = [r for r in expected if actual.get(r) != expected[r]]
        extra = [r for r in actual if r not in expected]
        for r in bad:
            print("MISMATCH/MISSING: " + r)
        for r in extra:
            print("NOT IN MANIFEST: " + r)
        print("manifest check: %d files listed, %d bad, %d extra" % (len(expected), len(bad), len(extra)))
        return 1 if bad or extra else 0
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    rels = files(root)
    with open(manifest_path, "w") as fh:
        for rel in rels:
            fh.write("%s  %s\n" % (digest(os.path.join(root, rel)), rel))
    print("wrote %s (%d files)" % (MANIFEST, len(rels)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
