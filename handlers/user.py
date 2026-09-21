import hashlib
import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
import texts
from db import DB, today
from entropy import Engine
from parsing import Parsed, parse

log = logging.getLogger("user")
router = Router()

_busy: set[int] = set()
_last_alert = 0.0
_manual_sha: str | None = None


class Ask(StatesGroup):
    waiting = State()


# ---------------- клавиатуры ----------------
def start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_MANUAL, callback_data="manual")],
            [InlineKeyboardButton(text=texts.BTN_ASK, callback_data="ask")],
        ]
    )


def sub_kb(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_SUBSCRIBE, url=config.CHANNEL_URL)],
            [InlineKeyboardButton(text=texts.BTN_SUBSCRIBED, callback_data=f"recheck:{action}")],
        ]
    )


def after_manual_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.BTN_ASK, callback_data="ask")]]
    )


def after_answer_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_MORE, callback_data="ask")],
            [InlineKeyboardButton(text=texts.BTN_MANUAL, callback_data="manual")],
        ]
    )


# ---------------- подписка ----------------
_MEMBER = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}


async def _alert_admin(bot: Bot, text: str) -> None:
    global _last_alert
    if not config.ADMIN_ID or time.time() - _last_alert < 1800:
        return
    _last_alert = time.time()
    try:
        await bot.send_message(config.ADMIN_ID, text)
    except TelegramAPIError:
        pass


async def is_subscribed(bot: Bot, db: DB, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(config.CHANNEL, user_id)
    except TelegramAPIError as e:
        log.error("Проверка подписки не удалась: %s", e)
        await db.log_event(user_id, "sub_check_error")
        await _alert_admin(
            bot,
            f"⚠️ Проверка подписки не работает: {e}\n"
            f"Проверь, что бот добавлен админом в {config.CHANNEL}. Пока пропускаю всех.",
        )
        return True  # при технической ошибке не блокируем людей
    if m.status in _MEMBER:
        return True
    return m.status == ChatMemberStatus.RESTRICTED and bool(getattr(m, "is_member", False))


async def gate(bot: Bot, db: DB, user_id: int, target: Message, action: str) -> bool:
    if await is_subscribed(bot, db, user_id):
        await db.mark_converted(user_id)
        return True
    await db.log_event(user_id, "sub_check_failed")
    await target.answer(texts.NOT_SUBSCRIBED, reply_markup=sub_kb(action))
    return False


# ---------------- действия ----------------
def _manual_hash() -> str:
    global _manual_sha
    if _manual_sha is None:
        with open(config.MANUAL_PATH, "rb") as f:
            _manual_sha = hashlib.sha1(f.read()).hexdigest()[:16]
    return _manual_sha


async def do_manual(target: Message, db: DB, user_id: int) -> None:
    key = f"manual_file_id:{_manual_hash()}"
    file_id = await db.kv_get(key)
    sent = None
    if file_id:
        try:
            sent = await target.answer_document(
                file_id, caption=texts.MANUAL_CAPTION, reply_markup=after_manual_kb()
            )
        except TelegramAPIError:
            sent = None  # file_id устарел, зальём заново
    if sent is None:
        doc = FSInputFile(config.MANUAL_PATH, filename="Приниматель_решений_инструкция.pdf")
        sent = await target.answer_document(
            doc, caption=texts.MANUAL_CAPTION, reply_markup=after_manual_kb()
        )
        if sent.document:
            await db.kv_set(key, sent.document.file_id)
    await db.log_event(user_id, "manual_sent")


async def do_ask(target: Message, state: FSMContext) -> None:
    await state.set_state(Ask.waiting)
    await target.answer(texts.ASK)


async def run_throw(
    message: Message, bot: Bot, db: DB, engine: Engine, state: FSMContext, parsed: Parsed
) -> None:
    uid = message.from_user.id
    if uid in _busy:
        await message.answer(texts.BUSY)
        return
    _busy.add(uid)
    try:
        if not await gate(bot, db, uid, message, "ask"):
            await state.clear()
            return
        if await db.count_ok_throws(uid, today()) >= config.DAILY_LIMIT:
            await db.log_event(uid, "limit_hit")
            await state.clear()
            await message.answer(texts.LIMIT)
            return

        seq = await db.next_throw_seq(uid)
        request_id = f"{uid}:{seq}"
        waiting = await message.answer(texts.LISTENING)
        result = await engine.throw(request_id, len(parsed.options))

        if result is None:
            await db.log_event(uid, "sources_silent")
            await db.save_throw(uid, request_id, parsed.question, parsed.options, "silent")
            await _edit_or_send(waiting, message, texts.SILENT)
            return  # состояние остаётся: можно просто прислать вопрос ещё раз

        await db.save_throw(
            uid, request_id, parsed.question, parsed.options, "ok",
            chosen_index=result.index, source=result.source, events=result.events,
            t0_ns=result.t0_ns, digest=result.digest,
        )
        answer = texts.ANSWER.format(
            n=result.index + 1, text=html.escape(parsed.options[result.index])
        )
        await _edit_or_send(waiting, message, answer, after_answer_kb())
        await state.clear()
    finally:
        _busy.discard(uid)


async def _edit_or_send(waiting: Message, origin: Message, text: str, kb=None) -> None:
    try:
        await waiting.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        await origin.answer(text, reply_markup=kb)


async def _reject(message: Message, parsed: Parsed) -> None:
    text = {"few": texts.TOO_FEW, "many": texts.TOO_MANY, "long": texts.TOO_LONG}[parsed.error]
    await message.answer(text)


# ---------------- хендлеры ----------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: DB) -> None:
    await state.clear()
    await db.log_event(message.from_user.id, "start")
    await message.answer(texts.START, reply_markup=start_kb())


@router.callback_query(F.data == "manual")
async def cb_manual(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    uid = cb.from_user.id
    await db.log_event(uid, "manual_click")
    if await gate(bot, db, uid, cb.message, "manual"):
        await do_manual(cb.message, db, uid)


@router.callback_query(F.data == "ask")
async def cb_ask(cb: CallbackQuery, bot: Bot, db: DB, state: FSMContext) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    uid = cb.from_user.id
    await db.log_event(uid, "ask_click")
    if await gate(bot, db, uid, cb.message, "ask"):
        await do_ask(cb.message, state)


@router.callback_query(F.data.startswith("recheck:"))
async def cb_recheck(cb: CallbackQuery, bot: Bot, db: DB, state: FSMContext) -> None:
    uid = cb.from_user.id
    action = cb.data.split(":", 1)[1]
    if not await is_subscribed(bot, db, uid):
        await db.log_event(uid, "sub_check_failed")
        await cb.answer(texts.STILL_NOT_SUBSCRIBED, show_alert=True)
        return
    await cb.answer()
    await db.mark_converted(uid)
    if not isinstance(cb.message, Message):
        return
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if action == "manual":
        await do_manual(cb.message, db, uid)
    else:
        await do_ask(cb.message, state)


@router.message(Ask.waiting, F.text, ~F.text.startswith("/"))
async def on_question(
    message: Message, bot: Bot, db: DB, engine: Engine, state: FSMContext
) -> None:
    parsed = parse(message.text)
    if not parsed.ok:
        await _reject(message, parsed)
        return
    await run_throw(message, bot, db, engine, state, parsed)


@router.message(F.text, ~F.text.startswith("/"))
async def on_text_without_state(
    message: Message, bot: Bot, db: DB, engine: Engine, state: FSMContext
) -> None:
    """Состояние могло потеряться после перезапуска: если пришёл нормальный вопрос, принимаем его."""
    parsed = parse(message.text)
    if parsed.ok:
        await run_throw(message, bot, db, engine, state, parsed)
    else:
        await message.answer(texts.MENU_HINT, reply_markup=start_kb())
