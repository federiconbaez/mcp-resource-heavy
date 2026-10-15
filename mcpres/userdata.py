"""Small persistent store for the *mutable* resources: a counter and a user-extensible glossary (SQLite)."""

import os
import sqlite3
import threading
import time
from contextlib import closing

from . import datasets

MAX_USER_TERMS = 500
TERM_RE_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789 -_"


class UserDataError(Exception):
    pass


def normalise_term(term):
    t = " ".join(str(term).strip().lower().split())
    if not t or len(t) > 48 or any(ch not in TERM_RE_CHARS for ch in t):
        raise UserDataError("term must be 1-48 chars of a-z, 0-9, space, '-' or '_'")
    return t


class UserData:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self._lock = threading.Lock()
        with closing(self._conn()) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS glossary_user (term TEXT PRIMARY KEY, definition TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS counter (id INTEGER PRIMARY KEY CHECK (id = 1), value INTEGER NOT NULL, bumps INTEGER NOT NULL);
                INSERT OR IGNORE INTO counter (id, value, bumps) VALUES (1, 0, 0);
            """)
            c.commit()
        os.chmod(path, 0o600)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # counter
    def counter(self):
        with closing(self._conn()) as c:
            r = c.execute("SELECT value, bumps FROM counter WHERE id = 1").fetchone()
        return {"value": r["value"], "bumps": r["bumps"]}

    def bump(self, by):
        with self._lock, closing(self._conn()) as c:
            c.execute("UPDATE counter SET value = value + ?, bumps = bumps + 1 WHERE id = 1", (by,))
            c.commit()
        return self.counter()

    # user glossary
    def user_terms(self):
        with closing(self._conn()) as c:
            return [{"term": r["term"], "definition": r["definition"], "created": r["created"], "builtin": False}
                    for r in c.execute("SELECT * FROM glossary_user ORDER BY term")]

    def add_term(self, term, definition):
        t = normalise_term(term)
        definition = str(definition).strip()
        if not definition or len(definition) > 600:
            raise UserDataError("definition must be 1-600 characters")
        if t in datasets.GLOSSARY_BY_TERM:
            raise UserDataError("%r is a built-in term and cannot be redefined" % t)
        with self._lock, closing(self._conn()) as c:
            n = c.execute("SELECT COUNT(*) FROM glossary_user").fetchone()[0]
            exists = c.execute("SELECT 1 FROM glossary_user WHERE term = ?", (t,)).fetchone()
            if not exists and n >= MAX_USER_TERMS:
                raise UserDataError("user glossary is full (%d terms)" % MAX_USER_TERMS)
            c.execute("INSERT INTO glossary_user (term, definition, created) VALUES (?,?,?) "
                      "ON CONFLICT(term) DO UPDATE SET definition = excluded.definition", (t, definition, self.clock()))
            c.commit()
        return {"term": t, "definition": definition, "created_new": not exists}

    def remove_term(self, term):
        t = normalise_term(term)
        with self._lock, closing(self._conn()) as c:
            n = c.execute("DELETE FROM glossary_user WHERE term = ?", (t,)).rowcount
            c.commit()
        return n > 0

    def get_term(self, term):
        try:
            t = normalise_term(term)
        except UserDataError:
            return None
        if t in datasets.GLOSSARY_BY_TERM:
            return dict(datasets.GLOSSARY_BY_TERM[t], builtin=True)
        with closing(self._conn()) as c:
            r = c.execute("SELECT * FROM glossary_user WHERE term = ?", (t,)).fetchone()
        return None if r is None else {"term": r["term"], "definition": r["definition"], "builtin": False}

    def all_terms(self):
        merged = [dict(g, builtin=True) for g in datasets.GLOSSARY] + self.user_terms()
        return sorted(merged, key=lambda g: g["term"])
