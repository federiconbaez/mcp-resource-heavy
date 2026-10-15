"""Command line interface:  python3 -m mcpres <command> [options]

    serve            run the MCP server (stdio default, or --transport http)
    mint-token       print a signed HS256 JWT for demos/tests
    verify-token     validate a token and print its claims
    client-secret    print the derived secret of a mock-issuer client
    audit-verify     verify the SHA-256 hash chain of the audit log
    audit-tail       print the last audit entries
    conformance      run the black-box protocol compliance suite (stdio + http)
"""

import argparse
import json
import os
import signal
import sys
import threading

from . import __version__
from .audit import verify_file
from .auth import (ALL_SCOPES, DEFAULT_AUDIENCE, DEFAULT_ISSUER, DEFAULT_CLIENTS, Authenticator, AuthError,
                   OidcIssuer, load_or_create_secret)
from .llm import LlmClient
from .server import McpServer


def _add_common(p):
    p.add_argument("--data-dir", default=os.environ.get("MCP_FORGE_HOME", ".mcpres"),
                   help="directory for memory.sqlite3, audit.log, oidc_secret (default: $MCP_FORGE_HOME or ./.mcpres)")
    p.add_argument("--issuer", default=os.environ.get("MCP_FORGE_ISSUER"),
                   help="expected/used token issuer (default: %s for stdio, the bound address for http)" % DEFAULT_ISSUER)
    p.add_argument("--audience", default=os.environ.get("MCP_FORGE_AUDIENCE", DEFAULT_AUDIENCE))


def build_parser():
    ap = argparse.ArgumentParser(prog="mcpres", description="Stdlib-only MCP server (simplified OIDC, not certified).")
    ap.add_argument("--version", action="version", version="mcp-resource-heavy " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the server")
    _add_common(s)
    s.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765, help="http port (0 = ephemeral)")
    s.add_argument("--page-size", type=int, default=25, help="items per page for resources/tools/prompts/templates lists (1-500, default 25)")
    s.add_argument("--no-auth", action="store_true", help="EXPLICITLY disable authentication (demo/dev only)")
    s.add_argument("--allow-origin", action="append", default=[], help="extra allowed Origin for http")

    m = sub.add_parser("mint-token", help="print a signed JWT")
    _add_common(m)
    m.add_argument("--sub", default="demo-user")
    m.add_argument("--scope", default=" ".join(ALL_SCOPES), help="space separated; default: all scopes")
    m.add_argument("--ttl", type=int, default=3600, help="seconds (use --ttl=-60 to mint an expired token)")
    m.add_argument("--aud", default=None, help="override audience (to produce a deliberately wrong-aud token)")
    m.add_argument("--show-claims", action="store_true", help="also print the claims to stderr")

    v = sub.add_parser("verify-token", help="validate a token")
    _add_common(v)
    v.add_argument("token")
    v.add_argument("--require-scope", action="append", default=[])

    c = sub.add_parser("client-secret", help="print derived secret of a mock-issuer client")
    _add_common(c)
    c.add_argument("client_id", choices=sorted(DEFAULT_CLIENTS))

    a = sub.add_parser("audit-verify", help="verify audit hash chain")
    _add_common(a)
    a.add_argument("--expect-head", help="externally anchored head hash (detects tail truncation)")
    a.add_argument("--expect-count", type=int, help="externally anchored entry count")
    a.add_argument("--file", help="audit file (default: <data-dir>/audit.log)")

    t = sub.add_parser("audit-tail", help="print last audit entries")
    _add_common(t)
    t.add_argument("-n", type=int, default=10)

    k = sub.add_parser("conformance", help="run protocol compliance suite")
    k.add_argument("--transport", choices=("stdio", "http", "both"), default="both")
    k.add_argument("--json", dest="json_path", help="write the JSON report here")
    k.add_argument("--quiet", action="store_true", help="only print the summary line")
    return ap


def _authenticator(args, default_issuer):
    secret = load_or_create_secret(args.data_dir)
    return Authenticator(secret, args.issuer or default_issuer, args.audience)


def cmd_serve(args):
    llm = LlmClient.from_env()
    auth = None if args.no_auth else _authenticator(args, None if args.transport == "http" else DEFAULT_ISSUER)
    server = McpServer(args.data_dir, auth, llm, page_size=args.page_size)
    note = "auth DISABLED (--no-auth)" if auth is None else "auth enabled"
    if args.transport == "stdio":
        from .transport_stdio import serve_stdio
        print(json.dumps({"event": "ready", "transport": "stdio", "auth": note, "llm": bool(llm),
                          "data_dir": os.path.abspath(args.data_dir)}), file=sys.stderr, flush=True)
        if auth is not None and not os.environ.get("MCP_TOKEN"):
            print("mcp-resource-heavy: warning: MCP_TOKEN not set; initialize will be rejected (use --no-auth for dev)",
                  file=sys.stderr, flush=True)
        return serve_stdio(server, os.environ.get("MCP_TOKEN"))
    from .transport_http import HttpTransport
    http = HttpTransport(server, args.host, args.port, allowed_origins=args.allow_origin)
    print(json.dumps({"event": "ready", "transport": "http", "url": http.base_url + "/mcp", "port": http.port,
                      "issuer": auth.issuer if auth else None, "audience": auth.audience if auth else None,
                      "auth": note, "llm": bool(llm), "data_dir": os.path.abspath(args.data_dir)}),
          file=sys.stderr, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=http.shutdown, daemon=True).start())
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_mint(args):
    auth = _authenticator(args, DEFAULT_ISSUER)
    scopes = args.scope.split()
    token = auth.mint(subject=args.sub, scopes=scopes, ttl=args.ttl, audience=args.aud)
    print(token)
    if args.show_claims:
        raw = token.split(".")[1]
        from .auth import b64url_decode
        print(json.dumps(json.loads(b64url_decode(raw)), indent=2, sort_keys=True), file=sys.stderr)
    return 0


def cmd_verify_token(args):
    auth = _authenticator(args, DEFAULT_ISSUER)
    try:
        claims = auth.verify(args.token, args.require_scope)
    except AuthError as exc:
        print(json.dumps({"valid": False, "error": exc.error, "description": exc.description}))
        return 1
    print(json.dumps({"valid": True, "claims": claims}, indent=2, sort_keys=True))
    return 0


def cmd_client_secret(args):
    auth = _authenticator(args, DEFAULT_ISSUER)
    print(OidcIssuer(auth).client_secret(args.client_id))
    return 0


def cmd_audit_verify(args):
    path = args.file or os.path.join(args.data_dir, "audit.log")
    res = verify_file(path, args.expect_head, args.expect_count)
    print(json.dumps(res, indent=2, sort_keys=True))
    print("AUDIT CHAIN OK: %d entries, head %s" % (res["count"], res["head"]) if res["ok"]
          else "AUDIT CHAIN BROKEN at line %s: %s" % (res["line"], res["error"]), file=sys.stderr)
    return 0 if res["ok"] else 1


def cmd_audit_tail(args):
    from .audit import AuditLog
    for e in AuditLog(os.path.join(args.data_dir, "audit.log")).tail(args.n):
        print(json.dumps(e, sort_keys=True))
    return 0


def cmd_conformance(args):
    from .conformance import run_cli
    return run_cli(args)


def main(argv=None):
    args = build_parser().parse_args(argv)
    handler = {"serve": cmd_serve, "mint-token": cmd_mint, "verify-token": cmd_verify_token,
               "client-secret": cmd_client_secret, "audit-verify": cmd_audit_verify,
               "audit-tail": cmd_audit_tail, "conformance": cmd_conformance}[args.cmd]
    try:
        return handler(args)
    except (ValueError, OSError) as exc:
        print("mcp-resource-heavy: error: %s" % exc, file=sys.stderr)
        return 2
