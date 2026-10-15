#!/usr/bin/env python3
"""Bounded model check of the subscription state machine.  Usage: model_check.py [--depth N] [--json]"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcpres import modelcheck  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    stats = modelcheck.check(args.depth)
    stats["seconds"] = round(time.time() - t0, 1)
    stats["alphabet_ops"] = list(modelcheck.ALPHABET)
    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        print("SUBSCRIPTION MODEL CHECK  alphabet=%d  depth=%d  sequences=%d  nodes=%d  violations=%d  (%.1fs)" % (
            stats["alphabet"], stats["depth"], stats["sequences"], stats["nodes"], len(stats["violations"]), stats["seconds"]))
        for v in stats["violations"][:5]:
            print("  VIOLATION", v)
        print("RESULT:", "PASS" if stats["ok"] else "FAIL")
    return 0 if stats["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
