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
CREATE TABLE IF NOT EXISTS expenses (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    category  TEXT    NOT NULL,
    amount    REAL    NOT NULL,
    note      TEXT    NOT NULL DEFAULT '',
    ts        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expenses_user_ts ON expenses (user_id, ts);
CREATE TABLE IF NOT EXISTS diary (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    mood     INTEGER NOT NULL,           -- 1..5
    trigger  TEXT    NOT NULL,           -- ключ из DIARY_TRIGGERS
    note     TEXT    NOT NULL DEFAULT '',
    smoked   INTEGER NOT NULL DEFAULT 0  -- сколько отмечено за день на момент записи
);
CREATE INDEX IF NOT EXISTS idx_diary_user_ts ON diary (user_id, ts);
"""

# Колонки, добавленные позже: дописываются в существующую базу при запуске
MIGRATIONS = {
    "product": "TEXT NOT NULL DEFAULT 'cig'",
    "currency": "TEXT NOT NULL DEFAULT '₽'",
    "tz": "TEXT",
    "plan_days": "INTEGER",     # снижать лимит на 1 каждые N дней
    "limit_set_at": "INTEGER",  # когда задан лимит (отсчёт для плана)
}

USER_FIELDS = {
    "per_day", "pack_price", "pack_size", "daily_limit", "started_at", *MIGRATIONS,
}


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        for name, ddl in MIGRATIONS.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        self.conn.commit()

    def get_user(self, user_id: int):
        return self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

    def save_user(self, user_id: int, product: str, per_day: int, pack_price: float, pack_size: int):
        with self.conn:
            self.conn.execute(
                """INSERT INTO users (user_id, product, per_day, pack_price, pack_size, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       product = excluded.product,
                       per_day = excluded.per_day,
                       pack_price = excluded.pack_price,
                       pack_size = excluded.pack_size""",
                (user_id, product, per_day, pack_price, pack_size, int(time.time())),
            )

    def update_user(self, user_id: int, **fields):
        unknown = set(fields) - USER_FIELDS
        if unknown:
            raise ValueError(f"Неизвестные поля: {unknown}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.conn:
            self.conn.execute(
                f"UPDATE users SET {assignments} WHERE user_id = ?",
                (*fields.values(), user_id),
            )

    def add_event(self, user_id: int, kind: str, ts: int | None = None) -> int:
        return self.add_events(user_id, kind, ts)[0]

    def add_events(self, user_id: int, kind: str, ts: int | None = None, n: int = 1) -> tuple[int, int]:
        """Добавляет n одинаковых событий, возвращает (первый id, последний id)."""
        ts = ts or int(time.time())
        with self.conn:
            ids = [
                self.conn.execute(
                    "INSERT INTO events (user_id, kind, ts) VALUES (?, ?, ?)",
                    (user_id, kind, ts),
                ).lastrowid
                for _ in range(n)
            ]
        return ids[0], ids[-1]

    def delete_events(self, user_id: int, first_id: int, last_id: int) -> int:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM events WHERE user_id = ? AND id BETWEEN ? AND ?",
                (user_id, first_id, last_id),
            )
            return cur.rowcount

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

    def smoke_times(self, user_id: int, since: int = 0) -> list[int]:
        rows = self.conn.execute(
            "SELECT ts FROM events WHERE user_id = ? AND kind = 'smoke' AND ts >= ? ORDER BY ts",
            (user_id, since),
        ).fetchall()
        return [r[0] for r in rows]

    def recent_events(self, user_id: int, kind: str, limit: int = 10):
        return self.conn.execute(
            "SELECT id, ts FROM events WHERE user_id = ? AND kind = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (user_id, kind, limit),
        ).fetchall()

    # ---------- траты ----------

    def add_expense(self, user_id: int, category: str, amount: float, note: str = "") -> int:
        with self.conn:
            return self.conn.execute(
                "INSERT INTO expenses (user_id, category, amount, note, ts) VALUES (?, ?, ?, ?, ?)",
                (user_id, category, amount, note, int(time.time())),
            ).lastrowid

    def delete_expense(self, user_id: int, expense_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id)
            )
            return cur.rowcount > 0

    def expense_sum(self, user_id: int, since: int = 0) -> float:
        return self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE user_id = ? AND ts >= ?",
            (user_id, since),
        ).fetchone()[0]

    def expense_by_category(self, user_id: int, since: int = 0):
        return self.conn.execute(
            """SELECT category, SUM(amount) AS total FROM expenses
               WHERE user_id = ? AND ts >= ? GROUP BY category ORDER BY total DESC""",
            (user_id, since),
        ).fetchall()

    def expense_times(self, user_id: int, category: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT ts FROM expenses WHERE user_id = ? AND category = ? ORDER BY ts",
            (user_id, category),
        ).fetchall()
        return [r[0] for r in rows]

    def first_expense_ts(self, user_id: int) -> int | None:
        return self.conn.execute(
            "SELECT MIN(ts) FROM expenses WHERE user_id = ?", (user_id,)
        ).fetchone()[0]

    def recent_expenses(self, user_id: int, limit: int = 10):
        return self.conn.execute(
            "SELECT id, category, amount, note, ts FROM expenses WHERE user_id = ? "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    # ---------- дневник ----------

    def add_diary(self, user_id: int, mood: int, trigger: str, note: str, smoked: int) -> int:
        with self.conn:
            return self.conn.execute(
                "INSERT INTO diary (user_id, ts, mood, trigger, note, smoked) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, int(time.time()), mood, trigger, note, smoked),
            ).lastrowid

    def delete_diary(self, user_id: int, entry_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM diary WHERE id = ? AND user_id = ?", (entry_id, user_id)
            )
            return cur.rowcount > 0

    def recent_diary(self, user_id: int, limit: int = 7):
        return self.conn.execute(
            "SELECT * FROM diary WHERE user_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    def diary_all(self, user_id: int):
        return self.conn.execute(
            "SELECT * FROM diary WHERE user_id = ? ORDER BY ts", (user_id,)
        ).fetchall()

    def reset(self, user_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM expenses WHERE user_id = ?", (user_id,))
            self.conn.execute("DELETE FROM diary WHERE user_id = ?", (user_id,))
            self.conn.execute("DELETE FROM events WHERE user_id = ?", (user_id,))
            self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

    def close(self):
        with closing(self.conn):
            pass
