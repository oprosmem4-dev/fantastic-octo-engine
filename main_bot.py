"""
bot/main_bot.py — запуск главного бота.

Собирает все роутеры, подключает middleware, запускает polling.
Этот файл запускается как: python bot/main_bot.py
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from database import create_all_tables
from bot.middlewares import AuthMiddleware
from bot.handlers import start, accounts, tasks, payment, admin, mirror

log = logging.getLogger(__name__)


async def main():
    # Создаём таблицы при первом запуске
    await create_all_tables()

    bot = Bot(token=BOT_TOKEN)
    # MemoryStorage — FSM хранится в памяти (для продакшена можно RedisStorage)
    dp = Dispatcher(storage=MemoryStorage())

    # Подключаем middleware ко всем входящим событиям
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    # Подключаем роутеры
    dp.include_router(start.router)
    dp.include_router(accounts.router)
    dp.include_router(tasks.router)
    dp.include_router(payment.router)
    dp.include_router(admin.router)
    dp.include_router(mirror.router)

    log.info("Главный бот запущен...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [BOT] %(levelname)s: %(message)s"
    )
    asyncio.run(main())
