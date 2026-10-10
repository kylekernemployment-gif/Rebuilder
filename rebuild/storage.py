"""Saves + cached live data. Postgres when DATABASE_URL is set (needed on Render, whose disk is
wiped on every deploy), otherwise a local SQLite file. Uses its own tables, so it can share the
PC Pick Em' database without touching anything else."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS rebuild_saves (
        user_id   BIGINT PRIMARY KEY,
        guild_id  BIGINT,
        team      TEXT NOT NULL,
        season    INTEGER NOT NULL,
        titles    INTEGER NOT NULL DEFAULT 0,
        summary   TEXT,
        blob      TEXT NOT NULL,
        updated   BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS rebuild_cache (
        key      TEXT PRIMARY KEY,
        value    TEXT NOT NULL,
        updated  BIGINT NOT NULL
    )""",
]


class Store:
    def __init__(self, url: str | None = None, path: str = "rebuild.db"):
        self.url = url
        self.path = path
        self.pg = bool(url)
        self._lock = threading.Lock()
        self._conn = None

    def _connect(self):
        if self._conn is not None:
            return self._conn
        if self.pg:
            import psycopg
            self._conn = psycopg.connect(self.url, autocommit=True)
        else:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        return self._conn

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    def _exec(self, sql: str, params: tuple = (), fetch: str | None = None):
        with self._lock:
            for attempt in range(2):
                try:
                    conn = self._connect()
                    cur = conn.cursor()
                    cur.execute(self._sql(sql), params)
                    if fetch == "one":
                        return cur.fetchone()
                    if fetch == "all":
                        return cur.fetchall()
                    return None
                except Exception:
                    if not self.pg or attempt:
                        raise
                    try:  # dropped Postgres connection (Neon sleeps): reconnect once
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None

    async def run(self, sql: str, params: tuple = (), fetch: str | None = None):
        return await asyncio.to_thread(self._exec, sql, params, fetch)

    async def init(self) -> None:
        for stmt in SCHEMA:
            await self.run(stmt)

    # --- saves ---
    async def get_save(self, user_id: int) -> str | None:
        row = await self.run("SELECT blob FROM rebuild_saves WHERE user_id = ?", (user_id,), "one")
        return row[0] if row else None

    async def put_save(self, user_id: int, guild_id: int | None, team: str, season: int, titles: int,
                       summary: str, blob: str) -> None:
        now = int(time.time())
        await self.run(
            "INSERT INTO rebuild_saves (user_id, guild_id, team, season, titles, summary, blob, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET guild_id = excluded.guild_id, "
            "team = excluded.team, season = excluded.season, titles = excluded.titles, "
            "summary = excluded.summary, blob = excluded.blob, updated = excluded.updated",
            (user_id, guild_id, team, season, titles, summary, blob, now))

    async def delete_save(self, user_id: int) -> None:
        await self.run("DELETE FROM rebuild_saves WHERE user_id = ?", (user_id,))

    async def leaderboard(self, guild_id: int | None, limit: int = 15) -> list[tuple]:
        cols = "SELECT user_id, team, season, titles, summary FROM rebuild_saves "
        order = " ORDER BY titles DESC, season DESC, updated DESC LIMIT ?"
        if guild_id is None:
            return await self.run(cols + order, (limit,), "all") or []
        return await self.run(cols + "WHERE guild_id = ?" + order, (guild_id, limit), "all") or []

    # --- cache ---
    async def get_cache(self, key: str) -> tuple[str, int] | None:
        row = await self.run("SELECT value, updated FROM rebuild_cache WHERE key = ?", (key,), "one")
        return (row[0], int(row[1])) if row else None

    async def put_cache(self, key: str, value: str) -> None:
        await self.run(
            "INSERT INTO rebuild_cache (key, value, updated) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
            (key, value, int(time.time())))
