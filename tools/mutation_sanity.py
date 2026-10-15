#!/usr/bin/env python3
"""Mutation sanity check: prove the test-suite is not vacuous.

For each deliberate bug below, copy the repo to a temp dir, inject the bug, run the unit tests with
``-f`` (fail fast) and require that they FAIL. A mutant that survives is reported and makes the script exit 1.

    python3 tools/mutation_sanity.py [--jobs N]
"""

import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ["tests.test_schema", "tests.test_jsonrpc", "tests.test_auth", "tests.test_protocol",
           "tests.test_persistence_audit", "tests.test_http_e2e"]

MUTANTS = [
    ("M01 ignore token expiry", "mcpres/auth.py", 'if now >= claims["exp"] + self.leeway:', "if False:", {}),
    ("M02 ignore audience", "mcpres/auth.py", "if self.audience not in auds:", "if False:", {}),
    ("M03 skip signature check", "mcpres/auth.py", "if not hmac.compare_digest(expected, signature):", "if False:", {}),
    ("M04 accept any JWT alg", "mcpres/auth.py", 'header.get("alg") != ALG', "False", {}),
    ("M05 ignore issuer", "mcpres/auth.py", 'if claims.get("iss") != self.issuer:', "if False:", {}),
    ("M06 no per-method scope enforcement", "mcpres/server.py", "if not self._has_scope(scope):", "if False:", {}),
    ("M07 allow requests before initialize", "mcpres/server.py", "if method not in PRE_INIT_METHODS and not was_initialized:", "if False:", {}),
    ("M08 always negotiate preferred version", "mcpres/server.py", "chosen = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PREFERRED_PROTOCOL_VERSION", "chosen = PREFERRED_PROTOCOL_VERSION", {}),
    ("M09 audit: ignore prev-link", "mcpres/audit.py", 'if entry["prev"] != prev:', "if False:", {}),
    ("M10 audit: ignore entry hash", "mcpres/audit.py", 'if entry_hash(entry) != entry["hash"]:', "if False:", {}),
    ("M11 booleans accepted as ids", "mcpres/jsonrpc.py", "isinstance(value, int) and not isinstance(value, bool)", "isinstance(value, int)", {}),
    ("M12 respond to notifications (contracts off)", "mcpres/jsonrpc.py", "        if is_note:\n            return None\n        return make_result(rid, result)", "        return make_result(rid, result)", {"MCP_FORGE_CONTRACTS": "0"}),
    ("M13 memory not persistent", "mcpres/memory.py", "sqlite3.connect(self.path, timeout=5.0)", 'sqlite3.connect(":memory:")', {}),
    ("M14 schema ignores additionalProperties:false", "mcpres/schema.py", "if ap is False:", "if False:", {}),
    ("M15 schema treats bool as integer", "mcpres/schema.py", '        if isinstance(value, bool):\n            return False\n        if isinstance(value, int):', "        if isinstance(value, int):", {}),
    ("M16 HTTP session not bound to subject", "mcpres/transport_http.py", "if app.auth and sub != (session.claims or {}).get(\"sub\"):", "if False:", {}),
    ("M17 batch always answers (even all-notification)", "mcpres/jsonrpc.py", "return responses or None", "return responses", {}),
    ("M18 id not echoed (wrong id in results)", "mcpres/jsonrpc.py", "return {\"jsonrpc\": \"2.0\", \"id\": msg_id, \"result\": result}", "return {\"jsonrpc\": \"2.0\", \"id\": 0, \"result\": result}", {"MCP_FORGE_CONTRACTS": "0"}),
]


def run_one(m):
    name, rel, old, new, extra_env = m
    tmp = tempfile.mkdtemp(prefix="mutant-")
    try:
        dst = os.path.join(tmp, "repo")
        shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".mcpres", "evidence"))
        path = os.path.join(dst, rel)
        with open(path) as fh:
            src = fh.read()
        if src.count(old) != 1:
            return name, "BAD-MUTANT", "pattern occurs %d times in %s" % (src.count(old), rel)
        with open(path, "w") as fh:
            fh.write(src.replace(old, new))
        env = dict(os.environ, PYTHONPATH=dst, **extra_env)
        r = subprocess.run([sys.executable, "-m", "unittest", "-f", *MODULES], cwd=dst, env=env,
                           capture_output=True, text=True, timeout=600)
        killed = r.returncode != 0
        first = next((l for l in r.stderr.splitlines() if l.startswith(("FAIL:", "ERROR:"))), "")
        return name, "KILLED" if killed else "SURVIVED", first[:110]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
        results = list(ex.map(run_one, MUTANTS))
    for name, status, info in results:
        print("%-52s %-9s %s" % (name, status, info))
    killed = sum(1 for _, s, _ in results if s == "KILLED")
    print("\nmutants killed: %d/%d" % (killed, len(results)))
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
