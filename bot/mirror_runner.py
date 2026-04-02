from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from sqlalchemy import select

from config import BOT_TOKEN
from database import SessionLocal
from models import MirrorBot

logger = logging.getLogger(__name__)

_running_bots: dict[int, asyncio.Task] = {}


async def run_mirror_bot(token: str, user_id: int) -> None:
    from aiogram import Dispatcher, Router, F
    from aiogram.filters import CommandStart
    from aiogram.types import Message
    from aiogram.fsm.storage.memory import MemoryStorage

    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer("🤖 Зеркальный бот работает!")

    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Mirror bot error (user {user_id}): {e}")
    finally:
        await bot.session.close()


async def sync_mirrors() -> None:
    while True:
        try:
            async with SessionLocal() as db:
                result = await db.execute(
                    select(MirrorBot).where(MirrorBot.is_active == True)
                )
                mirrors = list(result.scalars().all())

            active_ids = {m.user_id for m in mirrors}

            for user_id, task in list(_running_bots.items()):
                if user_id not in active_ids:
                    task.cancel()
                    del _running_bots[user_id]

            for mirror in mirrors:
                if mirror.user_id not in _running_bots:
                    task = asyncio.create_task(run_mirror_bot(mirror.token, mirror.user_id))
                    _running_bots[mirror.user_id] = task

        except Exception as e:
            logger.error(f"Mirror sync error: {e}")

        await asyncio.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(sync_mirrors())
