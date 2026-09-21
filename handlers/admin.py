import csv
import html
import io
import json
import time

from aiogram import Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import BufferedInputFile, Message

import config
from db import DB, EVENT_TYPES, REFUSAL_TYPES
from entropy import Engine
from entropy import mixer
from entropy.picker import pick

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
             r["chosen_text"], r["source"], r["request_id"], r["t0_ns"],
             " || ".join(json.loads(r["events"])) if r["events"] else "", r["hash"], r["status"]]
        )
    users = [list(r) for r in await db.export_users()]
    await message.answer_document(
        BufferedInputFile(
            _csv_bytes(
                ["id", "created_at_utc", "user_id", "username", "question", "options",
                 "answer_no", "answer_text", "source", "request_id", "t0_ns", "events", "hash", "status"],
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


@router.message(Command("last"), IsAdmin())
async def cmd_last(message: Message, db: DB) -> None:
    rows = await db.last_throws(3)
    if not rows:
        await message.answer("Бросков пока нет.")
        return
    for r in rows:
        options = json.loads(r["options"])
        events = json.loads(r["events"])
        base = mixer.build(r["request_id"], r["t0_ns"], r["source"], events)
        idx, digest = pick(base, len(options))
        same = idx == r["chosen_index"] and digest == r["hash"]
        lines = [
            f"<b>Бросок #{r['id']}</b> ({r['created_at']} UTC)",
            f"Источник: {r['source']}",
            "События, пришедшие после нажатия:",
        ]
        lines += [f"<code>{html.escape(e)}</code>" for e in events]
        lines += [
            f"Хэш: <code>{r['hash']}</code>",
            f"Ответ: {r['chosen_index'] + 1}. {html.escape(r['chosen_text'])} из {len(options)}",
            "Пересчёт из этих событий: " + ("✅ совпал" if same else "❌ НЕ совпал"),
        ]
        await message.answer("\n".join(lines))
    await message.answer(
        "Как проверить руками: транзакцию (btc:...) найди по хэшу на mempool.space/tx/<хэш>; "
        "правку (wiki:...) найди в истории статьи по названию, автору и времени; "
        "сделку (binance/coinbase/kraken:...) сверь по ID, цене и времени."
    )
