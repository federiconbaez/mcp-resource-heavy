"""Keyset cursor pagination.

A cursor is base64url(JSON {"v":1,"k":<list kind>,"a":<key of the last item already returned>}) - opaque to clients.
Keyset (not offset) paging means items inserted or deleted between two page fetches never cause duplicates of
items that were already returned or skip items that were present for the whole walk.
"""

import base64
import binascii
import json

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 500


class CursorError(ValueError):
    pass


def encode_cursor(kind, last_key):
    raw = json.dumps({"v": 1, "k": kind, "a": last_key}, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor, kind):
    if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
        raise CursorError("cursor must be a non-empty string of at most 1024 characters")
    if not all(ch.isalnum() or ch in "-_" for ch in cursor):
        raise CursorError("cursor has characters outside the base64url alphabet")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        obj = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise CursorError("cursor is not a cursor issued by this server")
    if not (isinstance(obj, dict) and set(obj) == {"v", "k", "a"} and obj["v"] == 1 and isinstance(obj["a"], str)):
        raise CursorError("cursor is not a cursor issued by this server")
    if obj["k"] != kind:
        raise CursorError("cursor was issued for a different list (%r), not %r" % (obj["k"], kind))
    return obj["a"]


def paginate(items, key, cursor, page_size, kind):
    """``items``: any iterable; ``key(item)``: unique sortable string. Returns (page, nextCursor or None)."""
    ordered = sorted(items, key=key)
    start = 0
    if cursor is not None:
        after = decode_cursor(cursor, kind)
        # first item strictly after the cursor key (binary search over the sorted keys)
        lo, hi = 0, len(ordered)
        while lo < hi:
            mid = (lo + hi) // 2
            if key(ordered[mid]) <= after:
                lo = mid + 1
            else:
                hi = mid
        start = lo
    page = ordered[start:start + page_size]
    more = start + page_size < len(ordered)
    return page, (encode_cursor(kind, key(page[-1])) if more and page else None)
