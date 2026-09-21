import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat

import config
from db import DB
from entropy import Engine, default_sources
from handlers import admin, user


class TouchUser(BaseMiddleware):
    """Записывает/обновляет пользователя при любом действии."""

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u is not None and not u.is_bot:
            await data["db"].touch_user(u.id, u.username)
        return await handler(event, data)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан (переменная окружения)")

    db = DB(config.DB_PATH)
    await db.init()
    engine = Engine(default_sources())
    await engine.start()

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage(), db=db, engine=engine)
    dp.message.outer_middleware(TouchUser())
    dp.callback_query.outer_middleware(TouchUser())
    dp.include_router(admin.router)
    dp.include_router(user.router)

    try:
        await bot.set_my_commands([BotCommand(command="start", description="Начать")])
        if config.ADMIN_ID:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Начать"),
                    BotCommand(command="stats", description="Статистика"),
                    BotCommand(command="export", description="Выгрузка CSV"),
                    BotCommand(command="sources", description="Состояние источников"),
                ],
                scope=BotCommandScopeChat(chat_id=config.ADMIN_ID),
            )
    except Exception as e:  # noqa: BLE001
        logging.warning("Не удалось выставить меню команд: %s", e)

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await engine.stop()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
