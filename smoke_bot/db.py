"""Хранилище данных бота на SQLite."""

import sqlite3
import time
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    per_day      INTEGER NOT NULL,
    pack_price   REAL    NOT NULL,
    pack_size    INTEGER NOT NULL DEFAULT 20,
    daily_limit  INTEGER,
    started_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    kind     TEXT    NOT NULL,  -- 'smoke' или 'craving' (удержался)
    ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events (user_id, kind, ts);
"""


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def get_user(self, user_id: int):
        return self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

    def save_user(self, user_id: int, per_day: int, pack_price: float, pack_size: int):
        with self.conn:
            self.conn.execute(
                """INSERT INTO users (user_id, per_day, pack_price, pack_size, started_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       per_day = excluded.per_day,
                       pack_price = excluded.pack_price,
                       pack_size = excluded.pack_size""",
                (user_id, per_day, pack_price, pack_size, int(time.time())),
            )

    def set_limit(self, user_id: int, limit: int | None):
        with self.conn:
            self.conn.execute(
                "UPDATE users SET daily_limit = ? WHERE user_id = ?", (limit, user_id)
            )

    def add_event(self, user_id: int, kind: str, ts: int | None = None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO events (user_id, kind, ts) VALUES (?, ?, ?)",
                (user_id, kind, ts or int(time.time())),
            )
            return cur.lastrowid

    def delete_event(self, user_id: int, event_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM events WHERE id = ? AND user_id = ?", (event_id, user_id)
            )
            return cur.rowcount > 0

    def count(self, user_id: int, kind: str, since: int, until: int | None = None) -> int:
        until = until or 2**62
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE user_id = ? AND kind = ? AND ts >= ? AND ts < ?",
            (user_id, kind, since, until),
        ).fetchone()[0]

    def last_smoke(self, user_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(ts) FROM events WHERE user_id = ? AND kind = 'smoke'", (user_id,)
        ).fetchone()
        return row[0]

    def smoke_times(self, user_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT ts FROM events WHERE user_id = ? AND kind = 'smoke' ORDER BY ts",
            (user_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def reset(self, user_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM events WHERE user_id = ?", (user_id,))
            self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

    def close(self):
        with closing(self.conn):
            pass
