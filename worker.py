"""
worker/worker.py — воркер рассылок.

Как работает:
  1. Каждые 30 секунд загружает активные задачи из БД
  2. Для каждой задачи создаёт (или обновляет) job в APScheduler
  3. Job отправляет сообщения через Telethon-аккаунты
  4. Обрабатывает ошибки (FloodWait, бан, нет доступа)
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import selectinload
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import FloodWaitError, UserBannedInChannelError, ChatWriteForbiddenError

from database import SessionLocal, create_all_tables
from models import Task, TaskAccount, Account, Log
from services.account_service import make_client

log = logging.getLogger(__name__)

# APScheduler — управляет интервальными задачами
scheduler = AsyncIOScheduler(timezone="UTC")

# Отслеживаем какие задачи уже загружены и с каким интервалом
# {task_id: interval_minutes}
_loaded_tasks: dict[int, int] = {}


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
                # Новая задача — добавляем
                scheduler.add_job(
                    run_task,
                    "interval",
                    minutes=task.interval_minutes,
                    id=job_id,
                    args=[task.id],
                    next_run_time=datetime.now(timezone.utc),  # запустить сразу
                    replace_existing=True,
                )
                _loaded_tasks[task.id] = task.interval_minutes
                log.info("Добавлена задача %d (каждые %d мин.)", task.id, task.interval_minutes)

            elif existing_interval != task.interval_minutes:
                # Изменился интервал — перепланируем
                scheduler.reschedule_job(job_id, trigger="interval", minutes=task.interval_minutes)
                _loaded_tasks[task.id] = task.interval_minutes
                log.info("Обновлён интервал задачи %d → %d мин.", task.id, task.interval_minutes)


# ── Выполнение одной задачи ───────────────────────────────────────────────────

async def run_task(task_id: int):
    """
    Выполнить рассылку для задачи.
    Открывает сессию БД, получает все аккаунты задачи и шлёт сообщения.
    """
    async with SessionLocal() as db:
    # Получаем задачу
        result = await db.execute(
            select(Task)
            .options(selectinload(Task.user))
            .where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()

        if not task or not task.is_active:
            return  # задача удалена или остановлена
        # Проверяем что у пользователя есть доступ
        if not task.user.has_access:
            log.info("Задача %d: пользователь %d без доступа, пропускаем", task_id, task.user_id)
            return

        log.info("Запускаю задачу %d (%s)", task_id, task.name)

        # Получаем все аккаунты задачи
        result = await db.execute(
            select(TaskAccount).where(TaskAccount.task_id == task_id)
        )
        task_accounts = result.scalars().all()

        if not task_accounts:
            log.warning("Задача %d: нет аккаунтов", task_id)
            return

        # Отправляем через каждый аккаунт в его чаты
        for ta in task_accounts:
            await send_via_account(db, ta, task.message)

        # Обновляем время последнего запуска
        task.last_run_at = datetime.now(timezone.utc)
        await db.commit()


async def send_via_account(db: AsyncSession, ta: TaskAccount, message: str):
    """
    Отправить сообщение через один аккаунт в его список чатов.
    """
    # Получаем аккаунт из БД
    result = await db.execute(select(Account).where(Account.id == ta.account_id))
    account = result.scalar_one_or_none()

    if not account or not account.is_active or account.is_banned:
        log.warning("Аккаунт %d недоступен", ta.account_id)
        return

    # Список чатов для этого аккаунта
    try:
        chat_ids: list[str] = json.loads(ta.chat_ids)
    except Exception:
        return

    if not chat_ids:
        return

    # Создаём Telethon-клиент
    client = make_client(account)
    try:
        await client.connect()

        if not await client.is_user_authorized():
            log.warning("Аккаунт %s не авторизован", account.phone)
            return

        # Загружаем диалоги, чтобы заполнить кэш сущностей Telethon.
        # StringSession использует MemorySession — кэш пуст при каждом
        # новом подключении, поэтому get_entity(int) без этого вызова
        # всегда падает с ValueError для числовых ID чатов.
        await client.get_dialogs()

        # Шлём в каждый чат
        for chat_id in chat_ids:
            await send_to_chat(db, client, account, ta.task_id, chat_id, message)
            # Небольшая пауза между сообщениями чтобы не получить FloodWait
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
    message: str,
):
    """
    Отправить одно сообщение в один чат.
    Обрабатывает FloodWait и другие ошибки.
    """
    success = False
    error_text = None

    try:
        # Пытаемся получить entity (чат/канал)
        entity = await resolve_entity(client, chat_id)
        if entity is None:
            error_text = "не удалось найти чат"
        else:
            await client.send_message(entity, message)
            success = True
            log.info("✓ [%s] → %s", account.phone, chat_id)

    except FloodWaitError as e:
        # Telegram просит подождать — ждём и пробуем снова
        log.warning("FloodWait %d сек. для %s", e.seconds, account.phone)
        await asyncio.sleep(e.seconds)
        try:
            entity = await resolve_entity(client, chat_id)
            if entity:
                await client.send_message(entity, message)
                success = True
        except Exception as retry_err:
            error_text = str(retry_err)

    except (UserBannedInChannelError, ChatWriteForbiddenError) as e:
        # Бан или нет прав писать
        error_text = f"нет доступа: {type(e).__name__}"
        log.warning("Нет доступа к %s через %s", chat_id, account.phone)

    except Exception as e:
        error_text = str(e)
        log.error("Ошибка отправки в %s: %s", chat_id, e)

    # Логируем результат в БД
    db.add(Log(
        task_id=task_id,
        account_id=account.id,
        chat_id=chat_id,
        success=success,
        error=error_text,
    ))
    await db.commit()


async def resolve_entity(client, chat_id: str):
    """Найти чат по ID или username."""
    # Попытка 1: напрямую как строка (с @ если username)
    try:
        # Если не число — добавляем @ для username
        if not chat_id.lstrip('-').isdigit():
            return await client.get_entity(f"@{chat_id}")
        return await client.get_entity(chat_id)
    except Exception:
        pass

    # Попытка 2: как число
    try:
        return await client.get_entity(int(chat_id))
    except Exception:
        pass

    # Попытка 3: добавить -100 для каналов
    try:
        n = int(chat_id)
        if n > 0:
            return await client.get_entity(int(f"-100{n}"))
    except Exception:
        pass

    return None

# ── Фоновая проверка аккаунтов ────────────────────────────────────────────────

async def check_accounts():
    """
    Периодически проверять аккаунты на бан.
    Запускается раз в час.
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

    # Проверка аккаунтов раз в час
    scheduler.add_job(check_accounts, "interval", hours=1, id="__check_accs__")

    scheduler.start()
    log.info("Воркер запущен.")

    # Первый запуск сразу
    await sync_tasks()

    # Держим процесс живым
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [WORKER] %(levelname)s: %(message)s"
    )
    asyncio.run(main())
