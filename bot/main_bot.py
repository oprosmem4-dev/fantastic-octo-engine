from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import accounts, admin, mirror, payment, start, tasks
from bot.middlewares import AuthMiddleware
from config import BOT_TOKEN, OWNER_ID
from database import engine
from models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def on_startup(bot: Bot) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created / verified.")
    await bot.send_message(OWNER_ID, "🤖 Бот запущен!")


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    dp.include_router(tasks.router)
    dp.include_router(start.router)
    dp.include_router(accounts.router)
    dp.include_router(payment.router)
    dp.include_router(admin.router)
    dp.include_router(mirror.router)

    dp.startup.register(on_startup)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
