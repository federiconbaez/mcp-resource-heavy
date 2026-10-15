"""Hash-chained, append-only audit log (JSON Lines, SHA-256).

Each line is a JSON object:

    {"seq": n, "ts": ..., "transport": ..., "session": ..., "sub": ..., "dir": "request"|"response"|"event",
     "size": ..., "payload": <text, truncated>, "payload_sha256": ..., "prev": <hash of previous line>, "hash": h}

where ``h = SHA256(canonical_json(entry without "hash"))`` and ``prev`` of the
first line is 64 zeros. Any edit, reordering or removal of a line that is not
at the very end breaks the chain, which ``verify`` detects. Truncating the
*tail* is not detectable from the file alone; pass ``expect_head`` /
``expect_count`` (an externally stored anchor) to ``verify`` to catch that.
"""

import hashlib
import json
import os
import threading
import time

try:  # POSIX advisory lock so several server processes can share one log
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

GENESIS = "0" * 64
MAX_PAYLOAD_CHARS = 16384


def canonical(entry):
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def entry_hash(entry):
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def _read_last_line(fh):
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    if size == 0:
        return b""
    block = 4096
    data = b""
    pos = size
    while pos > 0:
        step = min(block, pos)
        pos -= step
        fh.seek(pos)
        data = fh.read(step) + data
        stripped = data.rstrip(b"\n")
        if b"\n" in stripped:
            return stripped.rsplit(b"\n", 1)[1]
    return data.rstrip(b"\n")


class AuditLog:
    def __init__(self, path, fsync=True):
        self.path = path
        self.fsync = fsync
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        open(path, "ab").close()

    def append(self, direction, payload, transport="", session="", sub=""):
        text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "transport": transport,
            "session": session,
            "sub": sub,
            "dir": direction,
            "size": len(text),
            "payload": text[:MAX_PAYLOAD_CHARS],
            "payload_sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        }
        with self._lock:
            with open(self.path, "a+b") as fh:
                if fcntl:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    last = _read_last_line(fh)
                    if last:
                        prev_entry = json.loads(last)
                        entry["seq"] = prev_entry["seq"] + 1
                        entry["prev"] = prev_entry["hash"]
                    else:
                        entry["seq"] = 1
                        entry["prev"] = GENESIS
                    entry["hash"] = entry_hash(entry)
                    fh.seek(0, os.SEEK_END)
                    fh.write(canonical(entry) + b"\n")
                    fh.flush()
                    if self.fsync:
                        os.fsync(fh.fileno())
                finally:
                    if fcntl:
                        fcntl.flock(fh, fcntl.LOCK_UN)
        return entry

    def tail(self, n=10):
        entries = []
        with open(self.path, "rb") as fh:
            lines = fh.read().splitlines()
        for line in lines[-n:]:
            try:
                entries.append(json.loads(line))
            except ValueError:
                entries.append({"corrupt_line": line[:80].decode("utf-8", "replace")})
        return entries

    def verify(self, expect_head=None, expect_count=None):
        return verify_file(self.path, expect_head, expect_count)


def verify_file(path, expect_head=None, expect_count=None):
    """Return a dict: {ok, count, head, error, line}. Never raises on bad content."""
    prev = GENESIS
    count = 0
    try:
        fh = open(path, "rb")
    except OSError as exc:
        return {"ok": False, "count": 0, "head": GENESIS, "error": "cannot open: %s" % exc, "line": 0}
    with fh:
        for lineno, raw in enumerate(fh, 1):
            if not raw.endswith(b"\n"):
                return _fail(count, prev, lineno, "last line is truncated (no newline)")
            try:
                entry = json.loads(raw)
            except ValueError:
                return _fail(count, prev, lineno, "line is not valid JSON")
            if not isinstance(entry, dict) or not all(k in entry for k in ("seq", "prev", "hash")):
                return _fail(count, prev, lineno, "line lacks seq/prev/hash")
            if entry["seq"] != count + 1:
                return _fail(count, prev, lineno, "sequence gap: expected %d got %r" % (count + 1, entry["seq"]))
            if entry["prev"] != prev:
                return _fail(count, prev, lineno, "prev hash does not match previous entry")
            if entry_hash(entry) != entry["hash"]:
                return _fail(count, prev, lineno, "entry hash mismatch (content modified)")
            if raw.rstrip(b"\n") != canonical(entry):
                return _fail(count, prev, lineno, "non-canonical serialization")
            prev = entry["hash"]
            count += 1
    if expect_count is not None and count != expect_count:
        return _fail(count, prev, count, "entry count %d != anchored count %d (tail truncation/extension?)" % (count, expect_count))
    if expect_head is not None and prev != expect_head:
        return _fail(count, prev, count, "head hash != anchored head")
    return {"ok": True, "count": count, "head": prev, "error": None, "line": None}


def _fail(count, prev, lineno, msg):
    return {"ok": False, "count": count, "head": prev, "error": msg, "line": lineno}
