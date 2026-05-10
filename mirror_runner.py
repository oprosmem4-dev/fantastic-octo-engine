"""
bot/mirror_runner.py — запуск всех зеркальных ботов из БД.

Архитектура:
  - Каждое зеркало запускается в отдельном asyncio.Task
  - При падении — автоматический перезапуск через RESTART_DELAY секунд
  - Graceful shutdown по SIGINT/SIGTERM
  - Один общий Dispatcher + роутеры, но отдельный Bot-объект для каждого зеркала
    → экономия памяти (роутеры не дублируются)
  - Watch-loop каждые 30 секунд синхронизирует запущенные зеркала с БД

Масштабирование:
  - До ~500 зеркал на одном инстансе нормально (asyncio, не потоки)
  - При необходимости можно запустить несколько mirror_runner на разных машинах,
    разбив по диапазону user_id (задаётся через MIRROR_SHARD_* в .env)
"""
import asyncio
import logging
import signal
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from config import MIRROR_RESTART_DELAY, MIRROR_SHARD_MIN, MIRROR_SHARD_MAX
from database import create_all_tables, SessionLocal
from models import MirrorBot
from bot.middlewares import AuthMiddleware
from bot.handlers import start, accounts, tasks, admin, mirror
from bot.handlers import payment as payment_handler

log = logging.getLogger(__name__)

# ── Общий Dispatcher для всех зеркал ─────────────────────────────────────────
# Роутеры создаются один раз и шарятся между всеми Bot-инстансами.
# Это экономит ~5-10 МБ RAM на каждое зеркало.

_shared_dp: Optional[Dispatcher] = None


def _build_shared_dp() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    # Флаг IS_MIRROR блокирует оплату во всех зеркалах
    payment_handler.IS_MIRROR = True

    dp.include_router(start.router)
    dp.include_router(accounts.router)
    dp.include_router(tasks.router)
    dp.include_router(payment_handler.router)
    dp.include_router(admin.router)
    dp.include_router(mirror.router)

    return dp


def get_shared_dp() -> Dispatcher:
    global _shared_dp
    if _shared_dp is None:
        _shared_dp = _build_shared_dp()
    return _shared_dp


# ── Реестр запущенных зеркал ──────────────────────────────────────────────────

# mirror_id → asyncio.Task
_running: dict[int, asyncio.Task] = {}

# mirror_id → количество рестартов (для логов)
_restart_count: dict[int, int] = {}

# Флаг остановки
_shutdown = False


# ── Запуск одного зеркала ─────────────────────────────────────────────────────

async def _run_mirror_forever(mirror_id: int, token: str, username: str, user_id: int):
    """
    Запускает одно зеркало и перезапускает его при падении.
    Выходит только если _shutdown=True или зеркало удалено из БД.
    """
    global _shutdown

    while not _shutdown:
        try:
            log.info("▶️  Запуск зеркала @%s (mirror_id=%d, user=%d)", username, mirror_id, user_id)

            bot = Bot(token=token)
            dp  = get_shared_dp()

            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                # Не останавливать polling при исключении — пусть падает наверх
            )

        except asyncio.CancelledError:
            log.info("⏹  Зеркало @%s остановлено (CancelledError)", username)
            break

        except Exception as e:
            if _shutdown:
                break

            count = _restart_count.get(mirror_id, 0) + 1
            _restart_count[mirror_id] = count
            log.error(
                "❌ Зеркало @%s упало (попытка %d): %s — перезапуск через %ds",
                username, count, e, MIRROR_RESTART_DELAY,
            )
            await asyncio.sleep(MIRROR_RESTART_DELAY)

        finally:
            try:
                await bot.session.close()
            except Exception:
                pass


async def _start_mirror(m: MirrorBot):
    """Создать asyncio.Task для зеркала."""
    task = asyncio.create_task(
        _run_mirror_forever(m.id, m.token, m.bot_username or "?", m.user_id),
        name=f"mirror_{m.id}",
    )
    _running[m.id] = task
    _restart_count.setdefault(m.id, 0)


async def _stop_mirror(mirror_id: int):
    """Отменить задачу зеркала."""
    task = _running.pop(mirror_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _restart_count.pop(mirror_id, None)
    log.info("⏹  Зеркало mirror_id=%d остановлено", mirror_id)


# ── Watch-loop ────────────────────────────────────────────────────────────────

async def watch_mirrors():
    """
    Каждые 30 секунд синхронизирует запущенные зеркала с БД.
    Поддерживает шардирование по user_id для горизонтального масштабирования.
    """
    global _shutdown

    await create_all_tables()
    log.info(
        "Mirror runner стартовал. Shard: user_id [%s, %s]",
        MIRROR_SHARD_MIN or "0",
        MIRROR_SHARD_MAX or "∞",
    )

    while not _shutdown:
        try:
            async with SessionLocal() as db:
                q = select(MirrorBot).where(MirrorBot.is_active == True)

                # Шардирование — опционально
                if MIRROR_SHARD_MIN is not None:
                    q = q.where(MirrorBot.user_id >= MIRROR_SHARD_MIN)
                if MIRROR_SHARD_MAX is not None:
                    q = q.where(MirrorBot.user_id < MIRROR_SHARD_MAX)

                result  = await db.execute(q)
                db_mirrors = {m.id: m for m in result.scalars().all()}

            # Останавливаем удалённые / деактивированные
            for mid in list(_running.keys()):
                if mid not in db_mirrors:
                    await _stop_mirror(mid)

            # Запускаем новые (ещё не запущенные)
            for mid, m in db_mirrors.items():
                if mid not in _running:
                    await _start_mirror(m)
                elif _running[mid].done():
                    # Задача завершилась неожиданно — перезапускаем
                    log.warning("Задача mirror_id=%d мертва — перезапускаю", mid)
                    _running.pop(mid)
                    await _start_mirror(m)

            # Логируем статус раз в 5 минут (каждые 10 итераций по 30 сек)
            if int(asyncio.get_event_loop().time()) % 300 < 30:
                alive  = sum(1 for t in _running.values() if not t.done())
                dead   = sum(1 for t in _running.values() if t.done())
                total_restarts = sum(_restart_count.values())
                log.info(
                    "📊 Зеркала: %d активных, %d проблемных, %d рестартов всего",
                    alive, dead, total_restarts,
                )

        except Exception as e:
            log.error("Ошибка в watch_mirrors: %s", e)

        await asyncio.sleep(30)

    # Graceful shutdown — останавливаем все зеркала
    log.info("🛑 Останавливаю все зеркала...")
    await asyncio.gather(
        *[_stop_mirror(mid) for mid in list(_running.keys())],
        return_exceptions=True,
    )
    log.info("✅ Все зеркала остановлены.")


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
    global _shutdown

    # Обработка сигналов для graceful shutdown
    loop = asyncio.get_running_loop()

    def _handle_signal():
        global _shutdown
        _shutdown = True
        log.info("Получен сигнал завершения.")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler
            pass

    await watch_mirrors()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [MIRROR] %(levelname)s: %(message)s",
    )
    asyncio.run(main())
