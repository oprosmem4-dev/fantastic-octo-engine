"""
bot/mirror_runner.py — запуск всех зеркальных ботов из БД.

Каждые 60 секунд проверяет список зеркал в БД:
  - новые зеркала → запускает
  - удалённые зеркала → останавливает
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from database import create_all_tables, SessionLocal
from models import MirrorBot
from bot.middlewares import AuthMiddleware
from bot.handlers import start, accounts, tasks, admin, mirror
from bot.handlers import payment as payment_handler

log = logging.getLogger(__name__)

# Словарь запущенных зеркал: {mirror_id: (bot, dp, task)}
_running_mirrors: dict[int, asyncio.Task] = {}


async def start_mirror(mirror: MirrorBot):
    """Запустить одно зеркало."""
    log.info("Запускаю зеркало @%s (user=%d)", mirror.bot_username, mirror.user_id)

    bot = Bot(token=mirror.token)
    dp  = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    # Включаем флаг IS_MIRROR — блокирует оплату в зеркалах
    payment_handler.IS_MIRROR = True

    dp.include_router(start.router)
    dp.include_router(accounts.router)
    dp.include_router(tasks.router)
    dp.include_router(payment_handler.router)
    dp.include_router(admin.router)
    dp.include_router(mirror.router)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e:
        log.error("Ошибка зеркала %d: %s", mirror.id, e)
    finally:
        await bot.session.close()


async def watch_mirrors():
    """
    Цикл наблюдения за зеркалами в БД.
    Каждые 60 секунд синхронизирует запущенные боты с БД.
    """
    await create_all_tables()

    while True:
        async with SessionLocal() as db:
            result = await db.execute(
                select(MirrorBot).where(MirrorBot.is_active == True)
            )
            db_mirrors = {m.id: m for m in result.scalars().all()}

        # Останавливаем удалённые/отключённые
        for mid in list(_running_mirrors.keys()):
            if mid not in db_mirrors:
                log.info("Останавливаю зеркало %d", mid)
                _running_mirrors[mid].cancel()
                del _running_mirrors[mid]

        # Запускаем новые
        for mid, m in db_mirrors.items():
            if mid not in _running_mirrors:
                task = asyncio.create_task(start_mirror(m))
                _running_mirrors[mid] = task

        await asyncio.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [MIRROR] %(levelname)s: %(message)s"
    )
    asyncio.run(watch_mirrors())
