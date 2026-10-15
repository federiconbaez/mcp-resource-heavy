#!/usr/bin/env python3
"""Per-file report: how many lines of each variant file are inherited (unchanged) from mcp-base (= the user's mcp-forge).

    python3 adaptation_report.py <variant-dir> [<base-dir>]

Method: base files are read with the package name rewritten (mcpforge -> variant package, 'mcp-forge' kept as is), then
difflib.SequenceMatcher (autojunk off) counts the variant lines that sit inside matching blocks. Blank lines are ignored.
This is a *line-level overlap* measure between two repos of the same author; it is NOT a similarity check against third parties.
"""
import difflib
import os
import sys


def pkg_of(root):
    for d in sorted(os.listdir(root)):
        if os.path.isfile(os.path.join(root, d, "__main__.py")) and os.path.isfile(os.path.join(root, d, "server.py")):
            return d


def lines(path, rename=None):
    out = []
    for l in open(path, errors="replace").read().splitlines():
        if rename:
            l = l.replace(rename[0], rename[1])
        if l.strip():
            out.append(l.rstrip())
    return out


def main():
    var = os.path.abspath(sys.argv[1])
    base = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(os.path.dirname(var), "mcp-base")
    vp, bp = pkg_of(var), pkg_of(base)
    tot_n = tot_same = 0
    rows = []
    for rel in sorted(os.listdir(os.path.join(var, vp))):
        if not rel.endswith(".py"):
            continue
        v = lines(os.path.join(var, vp, rel))
        bpath = os.path.join(base, bp, rel)
        if os.path.exists(bpath):
            b = lines(bpath, (bp, vp))
            sm = difflib.SequenceMatcher(None, b, v, autojunk=False)
            same = sum(m.size for m in sm.get_matching_blocks())
        else:
            same = 0
        rows.append((vp + "/" + rel, len(v), same))
        tot_n += len(v)
        tot_same += same
    print("%-34s %6s %10s %6s" % ("file", "lines", "inherited", "%"))
    for name, n, same in rows:
        print("%-34s %6d %10d %5.0f%%" % (name, n, same, 100.0 * same / n if n else 0))
    print("%-34s %6d %10d %5.0f%%" % ("TOTAL package code", tot_n, tot_same, 100.0 * tot_same / tot_n if tot_n else 0))


if __name__ == "__main__":
    main()
