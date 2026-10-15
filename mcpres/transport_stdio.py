"""stdio transport: newline-delimited JSON-RPC (one message or batch per line).

stdout carries protocol messages only; diagnostics go to stderr. EOF on stdin ends the loop.
Server notifications (resources/updated, resources/list_changed) are written right after the response that caused them,
but only once the client has sent notifications/initialized.
"""

import sys

from .jsonrpc import INTERNAL_ERROR, INVALID_REQUEST, dumps, make_error

MAX_LINE = 1 << 20  # 1 MiB per message


def serve_stdio(server, token=None, stdin=None, stdout=None, max_line=MAX_LINE):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    session = server.new_session("stdio", token)

    def emit(text):
        stdout.write(text.encode("ascii") + b"\n")
        stdout.flush()

    def flush_notifications():
        for note in session.drain_notifications():
            emit(dumps(note))

    while True:
        raw = stdin.readline(max_line + 1)
        if not raw:
            session.close()
            return 0  # EOF
        if len(raw) > max_line and not raw.endswith(b"\n"):
            chunk = raw
            while chunk and not chunk.endswith(b"\n"):  # drain the oversized remainder
                chunk = stdin.readline(65536)
            server.log_event("request_too_large", {"limit": max_line}, "stdio", session.id)
            emit(dumps(make_error(None, INVALID_REQUEST, "Invalid Request", {"reason": "message exceeds %d bytes" % max_line})))
            continue
        line = raw.rstrip(b"\r\n")
        if not line.strip():
            continue
        try:
            out = session.handle_bytes(line)
        except Exception as exc:  # noqa: BLE001 - must never kill the loop
            print("mcp-resource-heavy: internal failure: %r" % (exc,), file=sys.stderr)
            out = dumps(make_error(None, INTERNAL_ERROR, "Internal error"))
        try:
            if out is not None:
                emit(out)
            flush_notifications()  # after the response, so a client sees "result" before the "updated" it triggered
        except BrokenPipeError:
            session.close()
            return 0
