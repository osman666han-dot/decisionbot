import csv
import io
import json
import time

from aiogram import Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import BufferedInputFile, Message

import config
from db import DB, EVENT_TYPES, REFUSAL_TYPES
from entropy import Engine

router = Router()


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(config.ADMIN_ID) and message.from_user is not None and message.from_user.id == config.ADMIN_ID


def _pair(events: dict, key: str) -> str:
    today, total = events.get(key, (0, 0))
    return f"{today} / {total}"


@router.message(Command("stats"), IsAdmin())
async def cmd_stats(message: Message, db: DB) -> None:
    s = await db.stats()
    ev = s["events"]
    conv = f"{s['pass_users']} из {s['fail_users']}"
    if s["fail_users"]:
        conv += f" ({round(100 * s['pass_users'] / s['fail_users'])}%)"
    lines = [
        "<b>Статистика</b>",
        f"Пользователи: {s['users_total']} (сегодня +{s['users_today']}, за 7 дней +{s['users_week']})",
        f"Вопросы с ответом: сегодня {s['throws_today']}, всего {s['throws_total']}",
        "",
        "<b>Нажатия (сегодня / всего)</b>",
    ]
    lines += [f"{label}: {_pair(ev, key)}" for key, label in EVENT_TYPES]
    lines += [
        "",
        "<b>Подписка</b>",
        f"Получили отказ → потом подписались: {conv}",
        "",
        "<b>Отказы по причинам (сегодня / всего)</b>",
    ]
    lines += [f"{label}: {_pair(ev, key)}" for key, label in REFUSAL_TYPES]
    await message.answer("\n".join(lines))


def _csv_bytes(header: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(list(r))
    return buf.getvalue().encode("utf-8-sig")  # BOM, чтобы Excel не ломал кириллицу


@router.message(Command("export"), IsAdmin())
async def cmd_export(message: Message, db: DB) -> None:
    throws = []
    for r in await db.export_throws():
        options = " | ".join(json.loads(r["options"]))
        throws.append(
            [r["id"], r["created_at"], r["user_id"], r["username"], r["question"], options,
             None if r["chosen_index"] is None else r["chosen_index"] + 1,
             r["chosen_text"], r["source"], r["hash"], r["status"]]
        )
    users = [list(r) for r in await db.export_users()]
    await message.answer_document(
        BufferedInputFile(
            _csv_bytes(
                ["id", "created_at_utc", "user_id", "username", "question", "options",
                 "answer_no", "answer_text", "source", "hash", "status"],
                throws,
            ),
            "throws.csv",
        )
    )
    await message.answer_document(
        BufferedInputFile(
            _csv_bytes(["id", "username", "first_seen_utc", "last_seen_utc"], users), "users.csv"
        )
    )


@router.message(Command("sources"), IsAdmin())
async def cmd_sources(message: Message, engine: Engine) -> None:
    lines = ["<b>Источники живых событий</b>"]
    now = time.time()
    for s in engine.sources:
        if s.last_event_at:
            age = f"последнее событие {int(now - s.last_event_at)} с назад"
        else:
            age = "событий ещё не было"
        state = "🟢 подключён" if s.connected else "🔴 нет соединения"
        lines.append(f"{s.name} ({s.detail}): {state}, {age}, всего {s.count}")
    await message.answer("\n".join(lines))
