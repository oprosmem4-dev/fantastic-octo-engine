"""
worker/worker.py — воркер рассылок.

Как работает:
  1. Каждые 30 секунд загружает активные задачи из БД
  2. Для каждой задачи создаёт (или обновляет) job в APScheduler
  3. Job отправляет сообщения через Telethon-аккаунты
  4. Обрабатывает ошибки (FloodWait, бан, нет доступа)

НОВОЕ: каждые 30 минут запускает run_full_restriction_check() из
restriction_service — проверяет заморозку, спамблок и доступ к чатам.
При ошибках отправки в чат также вызывается check_account_on_send_error().
"""
import asyncio
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy.orm import selectinload
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import FloodWaitError, UserBannedInChannelError, ChatWriteForbiddenError
from telethon.tl import types as tl_types
from aiogram import Bot
from config import BOT_TOKEN
from database import SessionLocal, create_all_tables
from models import Task, TaskAccount, Account, Log
from services.account_service import make_client
from services.restriction_service import check_account_on_send_error

log = logging.getLogger(__name__)

# APScheduler — управляет интервальными задачами
scheduler = AsyncIOScheduler(timezone="UTC")

# Отслеживаем какие задачи уже загружены и с каким интервалом
# {task_id: interval_minutes}
_loaded_tasks: dict[int, int] = {}

# Флаг чтобы не запускать несколько concurrent проверок ограничений
_restriction_check_running = False


# ── Главный цикл ──────────────────────────────────────────────────────────────

async def sync_tasks():
    """
    Синхронизировать задачи из БД с APScheduler.
    Вызывается каждые 30 секунд.
    """
    async with SessionLocal() as db:
        result = await db.execute(
            select(Task).where(Task.is_active == True)
        )
        active_tasks = result.scalars().all()
        active_ids = {t.id for t in active_tasks}

        # Удаляем задачи которые стали неактивными
        for task_id in list(_loaded_tasks.keys()):
            if task_id not in active_ids:
                job_id = f"task_{task_id}"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                del _loaded_tasks[task_id]
                log.info("Удалена задача %d из планировщика", task_id)

        # Добавляем или обновляем активные задачи
        for task in active_tasks:
            job_id = f"task_{task.id}"
            existing_interval = _loaded_tasks.get(task.id)

            if existing_interval is None:
                scheduler.add_job(
                    run_task,
                    "interval",
                    minutes=task.interval_minutes,
                    id=job_id,
                    args=[task.id],
                    next_run_time=datetime.now(timezone.utc),
                    replace_existing=True,
                )
                _loaded_tasks[task.id] = task.interval_minutes
                log.info("Добавлена задача %d (каждые %d мин.)", task.id, task.interval_minutes)

            elif existing_interval != task.interval_minutes:
                scheduler.reschedule_job(job_id, trigger="interval", minutes=task.interval_minutes)
                _loaded_tasks[task.id] = task.interval_minutes
                log.info("Обновлён интервал задачи %d → %d мин.", task.id, task.interval_minutes)


# ── Периодическая проверка ограничений ────────────────────────────────────────

async def check_restrictions():
    """
    Запускает полную проверку ограничений аккаунтов (каждые 30 минут).
    Защита от параллельного запуска через флаг.
    """
    global _restriction_check_running
    if _restriction_check_running:
        log.debug("Проверка ограничений уже запущена, пропускаем")
        return

    _restriction_check_running = True
    try:
        from services.restriction_service import run_full_restriction_check
        await run_full_restriction_check()
    except Exception as e:
        log.error("Ошибка в check_restrictions: %s", e)
    finally:
        _restriction_check_running = False


# ── Выполнение одной задачи ───────────────────────────────────────────────────

async def run_task(task_id: int):
    """
    Выполнить рассылку для задачи.
    Открывает сессию БД, получает все аккаунты задачи и шлёт сообщения.
    """
    async with SessionLocal() as db:
        result = await db.execute(
            select(Task)
            .options(selectinload(Task.user))
            .where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()

        if not task or not task.is_active:
            return

        if not task.user.has_access:
            log.info("Задача %d: пользователь %d без доступа, пропускаем", task_id, task.user_id)
            return

        log.info("Запускаю задачу %d (%s)", task_id, task.name)

        result = await db.execute(
            select(TaskAccount).where(TaskAccount.task_id == task_id)
        )
        task_accounts = result.scalars().all()

        if not task_accounts:
            log.warning("Задача %d: нет аккаунтов", task_id)
            return

        for ta in task_accounts:
            await send_via_account(db, ta, task)

        task.last_run_at = datetime.now(timezone.utc)
        await db.commit()


async def send_via_account(db: AsyncSession, ta: TaskAccount, task: Task):
    """
    Отправить сообщение через один аккаунт в его список чатов.
    """
    result = await db.execute(select(Account).where(Account.id == ta.account_id))
    account = result.scalar_one_or_none()

    if not account or not account.is_active or account.is_banned or account.status != "ok":
        log.warning("Аккаунт %d недоступен (status=%s)", ta.account_id,
                    getattr(account, "status", "?") if account else "not found")
        return

    try:
        chat_ids: list[str] = json.loads(ta.chat_ids)
    except Exception:
        return

    if not chat_ids:
        return

    # ✅ Извлекаем переменные ДО try-блока
    message_text = task.message or ""
    try:
        photo_file_ids = json.loads(task.photo_file_ids or "[]")
    except Exception:
        photo_file_ids = []
    try:
        format_entities_json = json.loads(task.format_entities or "[]")
    except Exception:
        format_entities_json = []

    client = make_client(account)
    try:
        await client.connect()

        if not await client.is_user_authorized():
            log.warning("Аккаунт %s не авторизован", account.phone)
            return

        await client.get_dialogs()

        for chat_id in chat_ids:
            await send_to_chat(
                db, client, account, ta.task_id, chat_id,
                message_text=message_text,
                photo_file_ids=photo_file_ids,
                entities_json=format_entities_json,
            )
            await asyncio.sleep(2)

    except Exception as e:
        log.error("Ошибка аккаунта %s: %s", account.phone, e)
    finally:
        await client.disconnect()


async def send_to_chat(
    db: AsyncSession,
    client,
    account: Account,
    task_id: int,
    chat_id: str,
    message_text: str,
    photo_file_ids: list[str],
    entities_json: list[dict],
):
    """
    Отправить одно сообщение в один чат.

    При ошибках:
    - FloodWait → ждём и повторяем
    - UserBannedInChannel / ChatWriteForbidden → запускаем проверку ограничений
    - Прочие ошибки → запускаем проверку ограничений (может быть спамблок)
    """
    success = False
    error_text = None

    try:
        entity = await resolve_entity(client, chat_id)
        if entity is None:
            error_text = "не удалось найти чат"
        else:
            entities = _to_telethon_entities(entities_json)
            if photo_file_ids:
                await client.send_file(
                    entity,
                    file=photo_file_ids,
                    caption=message_text or "",
                    formatting_entities=entities if entities else None,
                )
            else:
                await client.send_message(
                    entity,
                    message_text or "",
                    formatting_entities=entities if entities else None,
                )
            success = True
            log.info("✓ [%s] → %s", account.phone, chat_id)

    except FloodWaitError as e:
        log.warning("FloodWait %d сек. для %s", e.seconds, account.phone)
        await asyncio.sleep(e.seconds)
        try:
            entity = await resolve_entity(client, chat_id)
            if entity:
                entities = _to_telethon_entities(entities_json)
                if photo_file_ids:
                    await client.send_file(
                        entity,
                        file=photo_file_ids,
                        caption=message_text or "",
                        formatting_entities=entities if entities else None,
                    )
                else:
                    await client.send_message(
                        entity,
                        message_text or "",
                        formatting_entities=entities if entities else None,
                    )
                success = True
        except Exception as retry_err:
            error_text = str(retry_err)
            # FloodWait в ретрае — не триггерим проверку ограничений
            # (не запускаем asyncio.create_task тут)

    except (UserBannedInChannelError, ChatWriteForbiddenError) as e:
        error_text = f"нет доступа: {type(e).__name__}"
        log.warning("Нет доступа к %s через %s", chat_id, account.phone)
        # ── Запускаем проверку ограничений в фоне ────────────────────────────
        asyncio.create_task(
            check_account_on_send_error(account.id, task_id, chat_id, e)
        )

    except Exception as e:
        error_text = str(e)
        log.error("Ошибка отправки в %s: %s", chat_id, e)
        # ── Запускаем проверку ограничений в фоне (может быть спамблок) ──────
        asyncio.create_task(
            check_account_on_send_error(account.id, task_id, chat_id, e)
        )

    db.add(Log(
        task_id=task_id,
        account_id=account.id,
        chat_id=chat_id,
        success=success,
        error=error_text,
    ))
    await db.commit()


def _to_telethon_entities(entities_json: list[dict]) -> list:
    """JSON entities → telethon.tl.types.MessageEntity*"""
    out = []
    for e in entities_json or []:
        t = (e.get("type") or "").lower()
        offset = int(e.get("offset", 0))
        length = int(e.get("length", 0))
        try:
            if t == "bold":
                out.append(tl_types.MessageEntityBold(offset=offset, length=length))
            elif t == "italic":
                out.append(tl_types.MessageEntityItalic(offset=offset, length=length))
            elif t == "underline":
                out.append(tl_types.MessageEntityUnderline(offset=offset, length=length))
            elif t in {"strikethrough", "strike"}:
                out.append(tl_types.MessageEntityStrike(offset=offset, length=length))
            elif t == "spoiler":
                out.append(tl_types.MessageEntitySpoiler(offset=offset, length=length))
            elif t == "code":
                out.append(tl_types.MessageEntityCode(offset=offset, length=length))
            elif t == "pre":
                out.append(tl_types.MessageEntityPre(offset=offset, length=length, language=""))
            elif t in {"blockquote", "quote"}:
                out.append(tl_types.MessageEntityBlockquote(offset=offset, length=length))
            elif t == "text_link":
                url = e.get("url")
                if url:
                    out.append(tl_types.MessageEntityTextUrl(offset=offset, length=length, url=url))
        except Exception:
            pass
    return out


async def resolve_entity(client, chat_id: str):
    """Найти чат по ID или username."""
    try:
        if not chat_id.lstrip('-').isdigit():
            return await client.get_entity(f"@{chat_id}")
        return await client.get_entity(chat_id)
    except Exception:
        pass
    try:
        return await client.get_entity(int(chat_id))
    except Exception:
        pass
    try:
        n = int(chat_id)
        if n > 0:
            return await client.get_entity(int(f"-100{n}"))
    except Exception:
        pass
    return None


# ── Фоновая проверка авторизации аккаунтов ────────────────────────────────────

async def check_accounts():
    """
    Периодически проверять аккаунты на бан/разлогин (каждый час).
    Более поверхностная проверка чем run_full_restriction_check.
    """
    async with SessionLocal() as db:
        result = await db.execute(
            select(Account).where(Account.is_active == True, Account.is_banned == False)
        )
        accounts = result.scalars().all()

        for account in accounts:
            client = make_client(account)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    account.is_banned = True
                    account.status = "frozen"
                    log.warning("Аккаунт %s забанен или разлогинен", account.phone)
                await client.disconnect()
            except Exception as e:
                log.error("Ошибка проверки %s: %s", account.phone, e)

        await db.commit()


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
    await create_all_tables()

    # Синхронизация задач каждые 30 секунд
    scheduler.add_job(sync_tasks, "interval", seconds=30, id="__sync__")

    # Проверка авторизации аккаунтов раз в час
    scheduler.add_job(check_accounts, "interval", hours=1, id="__check_accs__")

    # Проверка ограничений (заморозка + спамблок + доступ к чатам) каждые 30 минут
    scheduler.add_job(check_restrictions, "interval", minutes=30, id="__restrictions__")

    scheduler.start()
    log.info("Воркер запущен.")

    # Первые запуски
    await sync_tasks()
    # Проверку ограничений запускаем с задержкой, чтобы воркер успел стартовать
    asyncio.get_event_loop().call_later(60, lambda: asyncio.create_task(check_restrictions()))

    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [WORKER] %(levelname)s: %(message)s"
    )
    asyncio.run(main())
