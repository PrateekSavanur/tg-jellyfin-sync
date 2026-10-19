"""SQLite job log: dedup, restart recovery and catch-up position."""

import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    msg_id      INTEGER PRIMARY KEY,
    doc_id      INTEGER NOT NULL,
    filename    TEXT,
    size        INTEGER,
    status      TEXT NOT NULL,   -- queued | downloading | done | unsorted | rejected | duplicate | failed
    dest        TEXT,
    error       TEXT,
    status_msg  INTEGER,         -- the bot's progress message in the channel
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_doc ON jobs(doc_id);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    @classmethod
    async def open(cls, path: Path) -> "Store":
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await db.commit()
        return cls(db)

    async def add(self, msg_id: int, doc_id: int, filename: str | None, size: int) -> bool:
        """Inserts a queued job; False if this message was already seen."""
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO jobs (msg_id, doc_id, filename, size, status, updated_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?)",
            (msg_id, doc_id, filename, size, time.time()),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def set(self, msg_id: int, status: str, dest: str | None = None, error: str | None = None) -> None:
        await self.db.execute(
            "UPDATE jobs SET status = ?, dest = COALESCE(?, dest), error = ?, updated_at = ? WHERE msg_id = ?",
            (status, dest, error, time.time(), msg_id),
        )
        await self.db.commit()

    async def set_status_msg(self, msg_id: int, status_msg: int) -> None:
        await self.db.execute("UPDATE jobs SET status_msg = ? WHERE msg_id = ?", (status_msg, msg_id))
        await self.db.commit()

    async def get(self, msg_id: int) -> aiosqlite.Row | None:
        async with self.db.execute("SELECT * FROM jobs WHERE msg_id = ?", (msg_id,)) as cur:
            return await cur.fetchone()

    async def imported_dest(self, doc_id: int, exclude_msg: int) -> str | None:
        """Destination of an earlier successful import of the same Telegram document."""
        async with self.db.execute(
            "SELECT dest FROM jobs WHERE doc_id = ? AND msg_id != ? AND status = 'done' LIMIT 1",
            (doc_id, exclude_msg),
        ) as cur:
            row = await cur.fetchone()
            return row["dest"] if row else None

    async def pending(self) -> list[int]:
        """Jobs interrupted by a restart."""
        async with self.db.execute(
            "SELECT msg_id FROM jobs WHERE status IN ('queued', 'downloading') ORDER BY msg_id"
        ) as cur:
            return [r["msg_id"] for r in await cur.fetchall()]

    async def last_seen(self) -> int | None:
        async with self.db.execute("SELECT value FROM kv WHERE key = 'last_msg_id'") as cur:
            row = await cur.fetchone()
            return int(row["value"]) if row else None

    async def bump_last_seen(self, msg_id: int) -> None:
        await self.db.execute(
            "INSERT INTO kv (key, value) VALUES ('last_msg_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = MAX(CAST(value AS INTEGER), excluded.value)",
            (msg_id,),
        )
        await self.db.commit()

    async def close(self) -> None:
        await self.db.close()
