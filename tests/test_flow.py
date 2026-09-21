"""Сценарный тест хендлеров с поддельной сессией Telegram: python -m tests.test_flow"""
import asyncio
import json
import os
import tempfile

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetChatMember
from aiogram.types import Update

import config
import texts
from bot import TouchUser
from db import DB
from entropy import Engine
from handlers import admin, user
from tests.test_core import Fake

ADMIN = 999


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []          # (имя метода, dict полей)
        self.subscribed = set()
        self.member_error = False
        self._mid = 100

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        if False:
            yield b""

    def out(self, name):
        return [c for c in self.calls if c[0] == name]

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append((name, method.model_dump()))
        if isinstance(method, GetChatMember):
            if self.member_error:
                raise TelegramBadRequest(method=method, message="chat not found")
            st = "member" if method.user_id in self.subscribed else "left"
            res = {"status": st, "user": {"id": method.user_id, "is_bot": False, "first_name": "x"}}
        elif name == "AnswerCallbackQuery":
            res = True
        elif name in ("SendMessage", "EditMessageText", "SendDocument", "EditMessageReplyMarkup"):
            self._mid += 1
            res = {"message_id": self._mid, "date": 0, "chat": {"id": 1, "type": "private"},
                   "text": getattr(method, "text", None) or "x"}
            if name == "SendDocument":
                res["document"] = {"file_id": "FID1", "file_unique_id": "u1"}
        else:
            res = True
        return self.check_response(bot, method, 200, json.dumps({"ok": True, "result": res})).result


def usr(uid):
    return {"id": uid, "is_bot": False, "first_name": "T", "username": f"u{uid}"}


def msg_update(n, uid, text):
    m = {"message_id": n, "date": 0, "chat": {"id": uid, "type": "private"}, "from": usr(uid), "text": text}
    if text.startswith("/"):
        m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.model_validate({"update_id": n, "message": m})


def cb_update(n, uid, data):
    return Update.model_validate({"update_id": n, "callback_query": {
        "id": str(n), "from": usr(uid), "chat_instance": "c", "data": data,
        "message": {"message_id": 50, "date": 0, "chat": {"id": uid, "type": "private"}, "text": "old"}}})


async def main():
    tmp = tempfile.mkdtemp()
    config.ADMIN_ID = ADMIN
    config.DAILY_LIMIT = 3
    config.LISTEN_TIMEOUT = 0.5
    user._manual_sha = None

    db = DB(os.path.join(tmp, "t.db"))
    await db.init()
    good = Engine([Fake("fast", 0.02)])
    await good.start()
    sess = FakeSession()
    bot = Bot("123456:ABCDEF", session=sess, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage(), db=db, engine=good)
    dp.message.outer_middleware(TouchUser())
    dp.callback_query.outer_middleware(TouchUser())
    dp.include_router(admin.router)
    dp.include_router(user.router)
    n = [0]

    async def send(u):
        await dp.feed_update(bot, u)

    def nxt():
        n[0] += 1
        return n[0]

    def last_text(name="SendMessage"):
        return sess.out(name)[-1][1]["text"]

    U = 1
    # 1. /start
    await send(msg_update(nxt(), U, "/start"))
    assert last_text() == texts.START
    kb = sess.out("SendMessage")[-1][1]["reply_markup"]["inline_keyboard"]
    assert [b[0]["callback_data"] for b in kb] == ["manual", "ask"]

    # 2. не подписан: мануал -> отказ с двумя кнопками
    await send(cb_update(nxt(), U, "manual"))
    assert last_text() == texts.NOT_SUBSCRIBED
    kb = sess.out("SendMessage")[-1][1]["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["url"] == "https://t.me/kolokotol" and kb[1][0]["callback_data"] == "recheck:manual"
    assert not sess.out("SendDocument")
    # «Я подписался», а подписки нет
    await send(cb_update(nxt(), U, "recheck:manual"))
    al = sess.out("AnswerCallbackQuery")[-1][1]
    assert al["show_alert"] and al["text"] == texts.STILL_NOT_SUBSCRIBED
    assert not sess.out("SendDocument")

    # 3. подписался -> мануал приходит (заливка), второй раз по file_id
    sess.subscribed.add(U)
    await send(cb_update(nxt(), U, "recheck:manual"))
    docs = sess.out("SendDocument")
    assert len(docs) == 1 and docs[0][1]["caption"] == texts.MANUAL_CAPTION
    await send(cb_update(nxt(), U, "manual"))
    docs = sess.out("SendDocument")
    assert len(docs) == 2 and docs[1][1]["document"] == "FID1", docs[1][1]["document"]

    # 4. вопрос: сначала «Задать вопрос», потом ошибки ввода, потом бросок
    await send(cb_update(nxt(), U, "ask"))
    assert last_text() == texts.ASK
    await send(msg_update(nxt(), U, "Что выпить?\nЧай\nКофе"))
    assert last_text() == texts.TOO_FEW
    await send(msg_update(nxt(), U, "Q\n" + "\n".join(f"o{i}" for i in range(11))))
    assert last_text() == texts.TOO_MANY
    await send(msg_update(nxt(), U, "Что выпить?\n1. Чай\n2. Кофе\n3. <b>Газировка</b>"))
    assert last_text() == texts.LISTENING
    ans = sess.out("EditMessageText")[-1][1]
    assert ans["text"].startswith("Твой ответ: <b>") and "&lt;b&gt;" in ans["text"] or "Чай" in ans["text"] or "Кофе" in ans["text"], ans["text"]
    cbs = [b[0]["callback_data"] for b in ans["reply_markup"]["inline_keyboard"]]
    assert cbs == ["ask", "manual"]
    print("ответ:", ans["text"])

    # 5. вопрос без нажатия «Задать вопрос» (состояние потеряно) тоже принимается
    await send(msg_update(nxt(), U, "Куда пойти?\nВ кино\nВ парк\nДомой"))
    assert sess.out("EditMessageText")[-1][1]["text"].startswith("Твой ответ")
    # 6. третий бросок ок, четвёртый упирается в лимит (DAILY_LIMIT=3)
    await send(msg_update(nxt(), U, "Ещё?\nда\nнет\nможет"))
    assert sess.out("EditMessageText")[-1][1]["text"].startswith("Твой ответ")
    await send(msg_update(nxt(), U, "Ещё?\nда\nнет\nможет"))
    assert last_text() == texts.LIMIT

    # 7. посторонний текст без вопроса -> подсказка с меню
    await send(msg_update(nxt(), 2, "привет"))
    assert last_text() == texts.MENU_HINT

    # 8. потоки молчат: SILENT и лимит не тратится
    dead = Engine([Fake("dead", 999)])
    await dead.start()
    dp.workflow_data["engine"] = dead
    U3 = 3
    sess.subscribed.add(U3)
    await send(cb_update(nxt(), U3, "ask"))
    await send(msg_update(nxt(), U3, "Q\na\nb\nc"))
    assert sess.out("EditMessageText")[-1][1]["text"] == texts.SILENT
    assert await db.count_ok_throws(U3, __import__("db").today()) == 0
    dp.workflow_data["engine"] = good

    # 9. админка: чужой /stats игнорируется, админский работает
    before = len(sess.calls)
    await send(msg_update(nxt(), U, "/stats"))
    assert len(sess.calls) == before
    await send(msg_update(nxt(), ADMIN, "/stats"))
    st = last_text()
    assert "Статистика" in st and "Пользователи: 4" in st and "Вопросы с ответом: сегодня 3" in st, st
    assert "Получили отказ → потом подписались: 1 из 1 (100%)" in st, st
    print(st)
    await send(msg_update(nxt(), ADMIN, "/export"))
    docs = sess.out("SendDocument")[-2:]
    assert {d[1]["document"].filename for d in docs} == {"throws.csv", "users.csv"}
    csv_bytes = [d[1]["document"] for d in docs if d[1]["document"].filename == "throws.csv"][0].data
    assert "Что выпить?".encode() in csv_bytes
    await send(msg_update(nxt(), ADMIN, "/sources"))
    assert "fast" in last_text()

    # 10. ошибка проверки подписки: пропускаем и предупреждаем админа
    sess.member_error = True
    U4 = 4
    await send(cb_update(nxt(), U4, "ask"))
    assert last_text() == texts.ASK
    assert any(c[1].get("chat_id") == ADMIN and "Проверка подписки" in c[1]["text"] for c in sess.out("SendMessage"))

    await good.stop(); await dead.stop(); await db.close()
    print("ALL FLOW OK")


asyncio.run(main())
