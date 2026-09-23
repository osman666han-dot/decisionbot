import json
import os
from datetime import datetime, timedelta, timezone

import aiosqlite

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT,
  first_seen TEXT NOT NULL,
  first_day TEXT NOT NULL,
  last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  ts TEXT NOT NULL,
  day TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(type, user_id);
CREATE TABLE IF NOT EXISTS throws(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  day TEXT NOT NULL,
  request_id TEXT NOT NULL,
  question TEXT NOT NULL,
  options TEXT NOT NULL,
  chosen_index INTEGER,
  chosen_text TEXT,
  source TEXT,
  events TEXT,
  t0_ns INTEGER,
  hash TEXT,
  status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_throws_user_day ON throws(user_id, day, status);
CREATE TABLE IF NOT EXISTS kv(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

EVENT_TYPES = [
    ("start", "Старт"),
    ("manual_click", "Мануал: нажатий"),
    ("manual_sent", "Мануал: отправлено"),
    ("ask_click", "Вопрос: нажатий"),
]
REFUSAL_TYPES = [
    ("sources_silent", "Потоки молчали"),
    ("sub_check_error", "Ошибки проверки подписки"),
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(config.TZ).date().isoformat()


def days_ago(n: int) -> str:
    return (datetime.now(config.TZ).date() - timedelta(days=n)).isoformat()


class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _one(self, sql: str, *args):
        cur = await self.conn.execute(sql, args)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def _all(self, sql: str, *args):
        cur = await self.conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    # ---------- пользователи и события ----------
    async def touch_user(self, user_id: int, username: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO users(id, username, first_seen, first_day, last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, last_seen=excluded.last_seen",
            (user_id, username, now_utc(), today(), now_utc()),
        )
        await self.conn.commit()

    async def log_event(self, user_id: int, type_: str) -> None:
        await self.conn.execute(
            "INSERT INTO events(user_id, type, ts, day) VALUES(?,?,?,?)",
            (user_id, type_, now_utc(), today()),
        )
        await self.conn.commit()

    async def mark_converted(self, user_id: int) -> None:
        """Один раз отмечает человека, который получил отказ по подписке, а потом подписался."""
        await self.conn.execute(
            "INSERT INTO events(user_id, type, ts, day) "
            "SELECT ?, 'sub_check_passed', ?, ? "
            "WHERE EXISTS (SELECT 1 FROM events WHERE user_id=? AND type='sub_check_failed') "
            "AND NOT EXISTS (SELECT 1 FROM events WHERE user_id=? AND type='sub_check_passed')",
            (user_id, now_utc(), today(), user_id, user_id),
        )
        await self.conn.commit()

    # ---------- броски ----------
    async def count_ok_throws(self, user_id: int, day: str) -> int:
        row = await self._one(
            "SELECT COUNT(*) FROM throws WHERE user_id=? AND day=? AND status='ok'", user_id, day
        )
        return row[0]

    async def next_throw_seq(self, user_id: int) -> int:
        row = await self._one("SELECT COUNT(*) FROM throws WHERE user_id=?", user_id)
        return row[0] + 1

    async def save_throw(
        self,
        user_id: int,
        request_id: str,
        question: str,
        options: list[str],
        status: str,
        chosen_index: int | None = None,
        source: str | None = None,
        events: list[str] | None = None,
        t0_ns: int | None = None,
        digest: str | None = None,
    ) -> None:
        chosen_text = options[chosen_index] if chosen_index is not None else None
        await self.conn.execute(
            "INSERT INTO throws(user_id, created_at, day, request_id, question, options, "
            "chosen_index, chosen_text, source, events, t0_ns, hash, status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, now_utc(), today(), request_id, question,
                json.dumps(options, ensure_ascii=False), chosen_index, chosen_text, source,
                json.dumps(events, ensure_ascii=False) if events is not None else None,
                t0_ns, digest, status,
            ),
        )
        await self.conn.commit()

    # ---------- kv (кэш file_id мануала) ----------
    async def kv_get(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM kv WHERE key=?", key)
        return row[0] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.conn.commit()

    # ---------- статистика и экспорт ----------
    async def stats(self) -> dict:
        t, w = today(), days_ago(6)
        out: dict = {}
        out["users_total"] = (await self._one("SELECT COUNT(*) FROM users"))[0]
        out["users_today"] = (await self._one("SELECT COUNT(*) FROM users WHERE first_day=?", t))[0]
        out["users_week"] = (await self._one("SELECT COUNT(*) FROM users WHERE first_day>=?", w))[0]
        out["throws_today"] = (
            await self._one("SELECT COUNT(*) FROM throws WHERE status='ok' AND day=?", t)
        )[0]
        out["throws_total"] = (await self._one("SELECT COUNT(*) FROM throws WHERE status='ok'"))[0]
        rows = await self._all(
            "SELECT type, COUNT(*) AS total, SUM(CASE WHEN day=? THEN 1 ELSE 0 END) AS today "
            "FROM events GROUP BY type",
            t,
        )
        out["events"] = {r["type"]: (r["today"] or 0, r["total"]) for r in rows}
        out["fail_users"] = (
            await self._one("SELECT COUNT(DISTINCT user_id) FROM events WHERE type='sub_check_failed'")
        )[0]
        out["pass_users"] = (
            await self._one("SELECT COUNT(DISTINCT user_id) FROM events WHERE type='sub_check_passed'")
        )[0]
        return out

    async def export_throws(self):
        return await self._all(
            "SELECT t.id, t.created_at, t.user_id, u.username, t.question, t.options, "
            "t.chosen_index, t.chosen_text, t.source, t.request_id, t.t0_ns, t.events, t.hash, t.status "
            "FROM throws t LEFT JOIN users u ON u.id=t.user_id ORDER BY t.id"
        )

    async def last_throws(self, limit: int = 3):
        return await self._all(
            "SELECT id, created_at, request_id, options, chosen_index, chosen_text, source, "
            "events, t0_ns, hash FROM throws WHERE status='ok' ORDER BY id DESC LIMIT ?",
            limit,
        )

    async def export_users(self):
        return await self._all("SELECT id, username, first_seen, last_seen FROM users ORDER BY id")
