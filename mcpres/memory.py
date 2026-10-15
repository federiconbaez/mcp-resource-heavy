"""Persistent memory backed by sqlite3 (stdlib). One short-lived connection per operation,
so it is safe across threads and across server restarts / processes."""

import json
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import closing

SCHEMA_VERSION = 1


class MemoryStoreError(Exception):
    """Domain error that tools surface as isError results."""


def fold(text):
    """Case- and accent-insensitive form (so 'Canción' matches 'cancion')."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).casefold()


def _like_escape(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class MemoryStore:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with closing(self._conn()) as conn:
            with conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    content_fold TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL DEFAULT '')""")
                conn.execute("""CREATE TABLE IF NOT EXISTS memory_tags (
                    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (memory_id, tag))""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tag ON memory_tags(tag)")
                conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    # -- helpers --------------------------------------------------------------
    def _tags_for(self, conn, ids):
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        out = {i: [] for i in ids}
        for row in conn.execute("SELECT memory_id, tag FROM memory_tags WHERE memory_id IN (%s) ORDER BY tag" % marks, list(ids)):
            out[row["memory_id"]].append(row["tag"])
        return out

    def _rows_to_entries(self, conn, rows):
        tags = self._tags_for(conn, [r["id"] for r in rows])
        return [{"id": r["id"], "content": r["content"], "tags": tags[r["id"]],
                 "importance": r["importance"], "created_at": r["created_at"], "created_by": r["created_by"]}
                for r in rows]

    # -- operations -------------------------------------------------------------
    def store(self, content, tags=(), importance=3, created_by=""):
        tags = sorted(set(tags))
        with closing(self._conn()) as conn:
            with conn:
                cur = conn.execute(
                    "INSERT INTO memories (content, content_fold, importance, created_at, created_by) VALUES (?,?,?,?,?)",
                    (content, fold(content), importance, _now(), created_by))
                mid = cur.lastrowid
                conn.executemany("INSERT INTO memory_tags (memory_id, tag) VALUES (?,?)", [(mid, t) for t in tags])
            return self.get(mid, conn)

    def get(self, mid, conn=None):
        own = conn is None
        conn = conn or self._conn()
        try:
            rows = conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchall()
            return self._rows_to_entries(conn, rows)[0] if rows else None
        finally:
            if own:
                conn.close()

    def forget(self, mid):
        with closing(self._conn()) as conn:
            with conn:
                cur = conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
                return cur.rowcount > 0

    def count(self):
        with closing(self._conn()) as conn:
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def list(self, limit=20, offset=0, tag=None):
        with closing(self._conn()) as conn:
            if tag:
                where, args = "WHERE id IN (SELECT memory_id FROM memory_tags WHERE tag = ?)", [tag]
            else:
                where, args = "", []
            total = conn.execute("SELECT COUNT(*) FROM memories " + where, args).fetchone()[0]
            rows = conn.execute("SELECT * FROM memories %s ORDER BY id DESC LIMIT ? OFFSET ?" % where,
                                args + [limit, offset]).fetchall()
            return total, self._rows_to_entries(conn, rows)

    def tag_counts(self):
        with closing(self._conn()) as conn:
            return {r["tag"]: r["n"] for r in conn.execute(
                "SELECT tag, COUNT(*) AS n FROM memory_tags GROUP BY tag ORDER BY n DESC, tag LIMIT 50")}

    def search(self, query, limit=5, tag=None):
        terms = [t for t in re.split(r"\s+", fold(query).strip()) if t][:8]
        if not terms:
            raise MemoryStoreError("query has no searchable terms")
        clauses, args = [], []
        for t in terms:
            like = "%" + _like_escape(t) + "%"
            clauses.append("(content_fold LIKE ? ESCAPE '\\' OR id IN (SELECT memory_id FROM memory_tags WHERE tag LIKE ? ESCAPE '\\'))")
            args += [like, like]
        if tag:
            clauses.append("id IN (SELECT memory_id FROM memory_tags WHERE tag = ?)")
            args.append(tag)
        sql = "SELECT * FROM memories WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT 500"
        with closing(self._conn()) as conn:
            rows = conn.execute(sql, args).fetchall()
            entries = self._rows_to_entries(conn, rows)
        scored = []
        for row, entry in zip(rows, entries):
            hits = sum(row["content_fold"].count(t) for t in terms)
            tag_hits = sum(1 for t in terms for g in entry["tags"] if t in fold(g))
            entry["score"] = round(hits + 2 * tag_hits + 0.25 * row["importance"], 3)
            scored.append(entry)
        scored.sort(key=lambda e: (-e["score"], -e["id"]))
        return scored[:limit]

    def index(self):
        total, recent = self.list(limit=10)
        return {"total": total, "tags": self.tag_counts(),
                "recent": [{"id": e["id"], "created_at": e["created_at"], "preview": e["content"][:80]} for e in recent]}
