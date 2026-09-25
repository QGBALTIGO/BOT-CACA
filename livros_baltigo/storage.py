from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Callable, TypeVar

from .catalog import LANGUAGES, FORMATS
from .errors import UserError
from .models import Book, SearchPage, SearchSpec

T = TypeVar("T")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, language TEXT NOT NULL DEFAULT 'portuguese',
  extension TEXT NOT NULL DEFAULT 'any', created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS books (key TEXT PRIMARY KEY, data TEXT NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS favorites (
  user_id INTEGER NOT NULL REFERENCES users(id), book_key TEXT NOT NULL REFERENCES books(key),
  added INTEGER NOT NULL, PRIMARY KEY(user_id, book_key)
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, query TEXT NOT NULL,
  language TEXT NOT NULL, extension TEXT NOT NULL, expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS search_cache (key TEXT PRIMARY KEY, data TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS edition_choices (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, data TEXT NOT NULL, expires INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_choices_expiry ON edition_choices(expires);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  book_key TEXT NOT NULL REFERENCES books(key), day TEXT NOT NULL,
  status TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_user_day ON jobs(user_id, day, status);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires);
"""


class Store:
    """Transações curtas em threads, sem bloquear o loop de rede do bot."""

    def __init__(self, path: Path):
        self.path = path

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    async def run(self, fn: Callable[[sqlite3.Connection], T], write=False) -> T:
        # sqlite3.Connection.__exit__ não fecha a conexão: fechamento explícito.
        def closed_execute():
            conn = self._connect()
            try:
                with conn:
                    if write:
                        conn.execute("BEGIN IMMEDIATE")
                    return fn(conn)
            finally:
                conn.close()
        return await asyncio.to_thread(closed_execute)

    async def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        def initialize(conn):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute("UPDATE jobs SET status='interrupted' WHERE status IN ('queued','running')")
        await self.run(initialize)
        await self.prune()

    async def user(self, uid: int) -> dict:
        def action(conn):
            conn.execute("INSERT OR IGNORE INTO users(id,created) VALUES (?,?)", (uid, int(time.time())))
            return dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
        return await self.run(action, True)

    async def preferences(self, uid: int, field: str, value: str):
        valid = {"language": LANGUAGES, "extension": FORMATS}
        if field not in valid or value not in valid[field]:
            raise UserError("Filtro inválido.", "invalid_filter")
        await self.user(uid)
        await self.run(lambda c: c.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid)), True)

    async def save_books(self, books: list[Book]):
        now = int(time.time())
        def action(conn):
            conn.executemany("INSERT INTO books VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                             [(b.key, json.dumps(b.to_dict(), ensure_ascii=False), now) for b in books])
        await self.run(action, True)

    async def book(self, key: str) -> Book | None:
        def action(conn):
            row = conn.execute("SELECT data FROM books WHERE key=?", (key,)).fetchone()
            return Book(**json.loads(row[0])) if row else None
        return await self.run(action)

    async def favorite(self, uid: int, key: str, enabled: bool):
        await self.user(uid)
        def action(conn):
            if enabled:
                count = conn.execute("SELECT count(*) FROM favorites WHERE user_id=?", (uid,)).fetchone()[0]
                if count >= 1000:
                    raise UserError("Sua biblioteca atingiu 1.000 favoritos. Remova algum antes de adicionar.", "favorites_limit")
                conn.execute("INSERT OR IGNORE INTO favorites VALUES (?,?,?)", (uid, key, int(time.time())))
            else:
                conn.execute("DELETE FROM favorites WHERE user_id=? AND book_key=?", (uid, key))
        await self.run(action, True)

    async def is_favorite(self, uid: int, key: str) -> bool:
        return await self.run(lambda c: c.execute("SELECT 1 FROM favorites WHERE user_id=? AND book_key=?", (uid, key)).fetchone() is not None)

    async def shelf(self, uid: int, page: int, size: int, history=False) -> SearchPage:
        if page < 1 or page > 1000:
            raise UserError("Página inválida.", "invalid_page")
        def action(conn):
            if history:
                query = "SELECT b.data, max(j.created) AS rank FROM jobs j JOIN books b ON b.key=j.book_key WHERE j.user_id=? AND j.status='done' GROUP BY j.book_key ORDER BY rank DESC"
            else:
                query = "SELECT b.data FROM favorites f JOIN books b ON b.key=f.book_key WHERE f.user_id=? ORDER BY f.added DESC, f.book_key"
            rows = conn.execute(query + " LIMIT ? OFFSET ?", (uid, size + 1, (page - 1) * size)).fetchall()
            return SearchPage([Book(**json.loads(r[0])) for r in rows[:size]], page, len(rows) > size)
        return await self.run(action)

    async def session(self, uid: int, spec: SearchSpec, ttl: int) -> str:
        ident = secrets.token_hex(6)
        await self.run(lambda c: c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)", (
            ident, uid, spec.query, spec.language, spec.extension, int(time.time()) + ttl)), True)
        return ident

    async def get_session(self, uid: int, ident: str) -> SearchSpec | None:
        def action(conn):
            row = conn.execute("SELECT * FROM sessions WHERE id=? AND user_id=? AND expires>?", (ident, uid, int(time.time()))).fetchone()
            return SearchSpec(row["query"], row["language"], row["extension"]) if row else None
        return await self.run(action)

    async def cache_page(self, key: str, page: SearchPage, ttl: int):
        await self.save_books(page.books)
        payload = json.dumps({"books": [b.to_dict() for b in page.books], "page": page.page, "has_next": page.has_next, "total": page.total}, ensure_ascii=False)
        await self.run(lambda c: c.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?)", (key, payload, int(time.time()) + ttl)), True)

    async def cached_page(self, key: str) -> SearchPage | None:
        def action(conn):
            row = conn.execute("SELECT data FROM search_cache WHERE key=? AND expires>?", (key, int(time.time()))).fetchone()
            if not row:
                return None
            data = json.loads(row[0])
            return SearchPage([Book(**b) for b in data["books"]], data["page"], data["has_next"], data["total"])
        return await self.run(action)

    async def reserve_job(self, uid: int, key: str, day: str, limit: int) -> int:
        def action(conn):
            used = conn.execute("SELECT count(*) FROM jobs WHERE user_id=? AND day=? AND status IN ('queued','running','done','uncertain')", (uid, day)).fetchone()[0]
            if used >= limit:
                raise UserError("Você atingiu seu limite diário local. Consulte /limite.", "local_quota")
            cursor = conn.execute("INSERT INTO jobs(user_id,book_key,day,status,created) VALUES (?,?,?,'queued',?)", (uid, key, day, int(time.time())))
            return cursor.lastrowid
        return await self.run(action, True)

    async def job_status(self, ident: int, status: str):
        if status not in {"queued", "running", "done", "failed", "interrupted", "uncertain"}:
            raise ValueError("Invalid job status")
        await self.run(lambda c: c.execute("UPDATE jobs SET status=? WHERE id=?", (status, ident)), True)

    async def daily_usage(self, uid: int, day: str) -> int:
        return await self.run(lambda c: c.execute("SELECT count(*) FROM jobs WHERE user_id=? AND day=? AND status IN ('queued','running','done','uncertain')", (uid, day)).fetchone()[0])

    async def stats(self) -> dict:
        return await self.run(lambda c: {
            "users": c.execute("SELECT count(*) FROM users").fetchone()[0],
            "favorites": c.execute("SELECT count(*) FROM favorites").fetchone()[0],
            "delivered_30d": c.execute("SELECT count(*) FROM jobs WHERE status='done'").fetchone()[0],
        })

    async def prune(self):
        now = int(time.time())
        def action(conn):
            conn.execute("DELETE FROM sessions WHERE expires<?", (now,))
            conn.execute("DELETE FROM edition_choices WHERE expires<=?", (now,))
            conn.execute("DELETE FROM search_cache WHERE expires<?", (now,))
            conn.execute("DELETE FROM search_cache WHERE key NOT IN (SELECT key FROM search_cache ORDER BY expires DESC LIMIT 500)")
            conn.execute("DELETE FROM jobs WHERE created<? AND status NOT IN ('running','queued')", (now - 30 * 86400,))
            conn.execute("DELETE FROM books WHERE updated<? AND key NOT IN (SELECT book_key FROM favorites) AND key NOT IN (SELECT book_key FROM jobs)", (now - 30 * 86400,))
        await self.run(action, True)


    async def choice(self, uid: int, books: list[Book], ttl: int) -> str:
        """An expiring snapshot, scoped to its owner, not an index into a mutable cache."""
        ident = secrets.token_hex(6)
        payload = json.dumps([b.to_dict() for b in books], ensure_ascii=False)
        def action(conn):
            conn.execute("INSERT INTO edition_choices VALUES (?,?,?,?)", (ident, uid, payload, int(time.time()) + ttl))
            conn.execute("DELETE FROM edition_choices WHERE user_id=? AND id NOT IN (SELECT id FROM edition_choices WHERE user_id=? ORDER BY expires DESC, rowid DESC LIMIT 80)", (uid, uid))
        await self.run(action, True)
        return ident

    async def get_choice(self, uid: int, ident: str) -> list[Book] | None:
        def action(conn):
            row = conn.execute("SELECT data FROM edition_choices WHERE id=? AND user_id=? AND expires>?", (ident, uid, int(time.time()))).fetchone()
            return [Book(**b) for b in json.loads(row[0])] if row else None
        return await self.run(action)

    async def latest_session(self, uid: int) -> SearchSpec | None:
        def action(conn):
            row = conn.execute("SELECT query,language,extension FROM sessions WHERE user_id=? AND expires>? ORDER BY rowid DESC LIMIT 1", (uid, int(time.time()))).fetchone()
            return SearchSpec(*row) if row else None
        return await self.run(action)
