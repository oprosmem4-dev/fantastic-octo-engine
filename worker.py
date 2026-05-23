"""
worker/worker.py — воркер рассылок.

ИЗМЕНЕНИЯ:
  - Пул Telethon-клиентов: клиенты создаются один раз и остаются подключёнными.
    Нет connect/disconnect на каждую отправку → аккаунты выглядят как живые.
  - Rate limiter на уровне аккаунта: asyncio.Semaphore(1) + минимальный
    cooldown MIN_SEND_INTERVAL секунд между отправками одного аккаунта.
  - Мгновенная замена аккаунта при ошибке отправки: если сообщение не ушло
    из-за бана/заморозки/forbidden — сразу ищем другой системный аккаунт и
    отправляем через него. Пользователь не замечает перерыва.
  - Тестовая отправка "." убрана везде — первая реальная отправка пользователя
    является и проверкой доступа. При ошибке → мгновенная замена.
  - sends_last_hour: при каждой отправке инкрементируется счётчик аккаунта.
    Каждый час — сброс. При sync_tasks учитывается при балансировке.
  - Keepalive job каждые 5 минут: проверяет живость клиентов в пуле.
"""
import asyncio
import io
import json
import logging
import random
import time
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from telethon.errors import (
    FloodWaitError,
    UserBannedInChannelError,
    ChatWriteForbiddenError,
    UserDeactivatedError,
    AuthKeyUnregisteredError,
)
from telethon.tl import types as tl_types
from aiogram import Bot

from config import BOT_TOKEN, MIN_SEND_INTERVAL
from database import SessionLocal, create_all_tables
from models import Task, TaskAccount, Account, Log
from services.account_service import make_client

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")

# ── Пул клиентов ─────────────────────────────────────────────────────────────
# account_id → TelegramClient (постоянное подключение)
_client_pool: dict[int, object] = {}
_pool_lock = asyncio.Lock()

# ── Rate limiter ──────────────────────────────────────────────────────────────
# account_id → asyncio.Semaphore(1)  (одна отправка за раз с этого аккаунта)
_account_semaphores: dict[int, asyncio.Semaphore] = {}
# account_id → monotonic time последней отправки
_account_last_send: dict[int, float] = {}

# ── Отслеживание jobs ─────────────────────────────────────────────────────────
_loaded_jobs: dict[str, int] = {}

# ── zero-width символы для невидимой рандомизации ────────────────────────────
_ZW_CHARS = ['\u200b', '\u200c', '\u200d']


def _randomize_text(text: str) -> str:
    if not text:
        return text
    pos  = random.randint(0, len(text))
    char = random.choice(_ZW_CHARS)
    return text[:pos] + char + text[pos:]


# ── Пул клиентов: управление ─────────────────────────────────────────────────

async def get_client(account: Account):
    """
    Вернуть подключённый Telethon-клиент из пула.
    Если клиента нет или он отвалился — создать новый.
    """
    async with _pool_lock:
        client = _client_pool.get(account.id)
        if client is not None and client.is_connected():
            return client

        # Создаём / переподключаем
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

        client = make_client(account)
        try:
            await client.connect()
            await asyncio.sleep(1)
            if not await client.is_user_authorized():
                log.warning("Аккаунт %s не авторизован", account.phone)
                await client.disconnect()
                _client_pool.pop(account.id, None)
                return None
            _client_pool[account.id] = client
            log.info("Клиент %s подключён (пул)", account.phone)
            return client
        except Exception as e:
            log.error("Не удалось подключить %s: %s", account.phone, e)
            try:
                await client.disconnect()
            except Exception:
                pass
            return None


async def remove_client(account_id: int):
    """Убрать клиент из пула (при бане/заморозке)."""
    async with _pool_lock:
        client = _client_pool.pop(account_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


async def keepalive_clients():
    """
    Каждые 5 минут проверяет все клиенты в пуле.
    Отвалившиеся — реконнектит или удаляет.
    """
    async with _pool_lock:
        dead = [aid for aid, c in _client_pool.items() if not c.is_connected()]

    for aid in dead:
        async with SessionLocal() as db:
            result = await db.execute(
                select(Account).where(Account.id == aid, Account.is_active == True,
                                      Account.is_banned == False, Account.status == "ok")
            )
            acc = result.scalar_one_or_none()
        if acc:
            log.info("Keepalive: реконнект %s", acc.phone)
            await get_client(acc)
        else:
            async with _pool_lock:
                _client_pool.pop(aid, None)


# ── Rate limiter ──────────────────────────────────────────────────────────────

def _get_semaphore(account_id: int) -> asyncio.Semaphore:
    if account_id not in _account_semaphores:
        _account_semaphores[account_id] = asyncio.Semaphore(1)
    return _account_semaphores[account_id]


async def _wait_rate_limit(account_id: int):
    """Ждать MIN_SEND_INTERVAL секунд с момента последней отправки этого аккаунта."""
    last = _account_last_send.get(account_id, 0)
    wait = MIN_SEND_INTERVAL - (time.monotonic() - last)
    if wait > 0:
        await asyncio.sleep(wait)


def _mark_sent(account_id: int):
    _account_last_send[account_id] = time.monotonic()


# ── Счётчик отправок (для балансировки) ──────────────────────────────────────

async def _increment_sends(db: AsyncSession, account: Account):
    """Инкрементировать счётчик отправок, сбрасывать раз в час."""
    now = datetime.now(timezone.utc)
    if account.sends_reset_at is None or (now - account.sends_reset_at).total_seconds() > 3600:
        account.sends_last_hour = 0
        account.sends_reset_at  = now
    account.sends_last_hour += 1


async def reset_sends_counters():
    """Cron каждый час: сбросить sends_last_hour у всех аккаунтов."""
    async with SessionLocal() as db:
        result = await db.execute(select(Account))
        accounts = result.scalars().all()
        for acc in accounts:
            acc.sends_last_hour = 0
            acc.sends_reset_at  = datetime.now(timezone.utc)
        await db.commit()
    log.info("Счётчики sends_last_hour сброшены.")


# ── Синхронизация планировщика ────────────────────────────────────────────────

async def sync_tasks():
    """
    Каждые 30 сек читает активные задачи из БД и синхронизирует APScheduler.
    Единица планирования — (task_id, account_id, chat_id).
    """
    async with SessionLocal() as db:
        result = await db.execute(
            select(Task)
            .options(selectinload(Task.accounts), selectinload(Task.user))
            .where(Task.is_active == True)
        )
        active_tasks = result.scalars().all()

        desired_jobs: dict[str, tuple] = {}

        for task in active_tasks:
            if not task.user.has_access:
                continue
            for ta in task.accounts:
                try:
                    chat_ids = json.loads(ta.chat_ids or "[]")
                except Exception:
                    chat_ids = []
                for chat_id in chat_ids:
                    job_id = _make_job_id(task.id, ta.account_id, chat_id)
                    desired_jobs[job_id] = (task.id, ta.account_id, chat_id, task.interval_minutes)

        # Удаляем лишние jobs
        for job_id in list(_loaded_jobs.keys()):
            if job_id not in desired_jobs:
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                del _loaded_jobs[job_id]

        # Добавляем новые / обновляем интервалы
        for job_id, (task_id, account_id, chat_id, interval) in desired_jobs.items():
            existing_interval = _loaded_jobs.get(job_id)
            if existing_interval is None:
                scheduler.add_job(
                    send_chat_job,
                    "interval",
                    minutes=interval,
                    id=job_id,
                    args=[task_id, account_id, chat_id],
                    next_run_time=datetime.now(timezone.utc),
                    replace_existing=True,
                )
                _loaded_jobs[job_id] = interval
            elif existing_interval != interval:
                scheduler.reschedule_job(job_id, trigger="interval", minutes=interval)
                _loaded_jobs[job_id] = interval


def _make_job_id(task_id: int, account_id: int, chat_id: str) -> str:
    safe = chat_id.replace("@", "at_").replace("-", "m_")
    return f"chat_{task_id}_{account_id}_{safe}"


# ── Основной job: отправка в один чат ────────────────────────────────────────

async def send_chat_job(task_id: int, account_id: int, chat_id: str):
    """
    Отправляет сообщение в один чат.
    При ошибке доступа — мгновенно ищет замену среди системных аккаунтов
    и отправляет через неё. Пользователь не замечает перерыва.
    """
    async with SessionLocal() as db:
        result = await db.execute(
            select(Task)
            .options(selectinload(Task.user))
            .where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task or not task.is_active or not task.user.has_access:
            return

        result = await db.execute(
            select(Account).where(Account.id == account_id)
        )
        account = result.scalar_one_or_none()
        if not account or not account.is_active or account.is_banned or account.status != "ok":
            # Основной аккаунт недоступен — пробуем найти замену сразу
            await _try_failover(db, task, account_id, chat_id)
            return

        # Данные сообщения
        message_text = task.message or ""
        try:
            photo_file_ids   = json.loads(task.photo_file_ids or "[]")
        except Exception:
            photo_file_ids   = []
        try:
            format_entities  = json.loads(task.format_entities or "[]")
        except Exception:
            format_entities  = []

    # Rate limiting: одна отправка за раз с этого аккаунта + cooldown
    sem = _get_semaphore(account_id)
    async with sem:
        await _wait_rate_limit(account_id)

        client = await get_client(account)
        if client is None:
            # Клиент недоступен — failover
            async with SessionLocal() as db:
                result = await db.execute(select(Task).options(selectinload(Task.user)).where(Task.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    await _try_failover(db, task, account_id, chat_id)
            return

        success, error, need_failover = await _send_with_client(
            client, account, task_id, chat_id,
            message_text, photo_file_ids, format_entities,
        )

        _mark_sent(account_id)

        async with SessionLocal() as db:
            db.add(Log(
                task_id=task_id,
                account_id=account.id,
                chat_id=chat_id,
                success=success,
                error=error,
            ))

            if success:
                await _increment_sends(db, account)
                await db.commit()

                # Перепланируем с новым random offset
                random_offset = random.randint(0, 90)
                job_id = _make_job_id(task_id, account_id, chat_id)
                if scheduler.get_job(job_id):
                    interval = _loaded_jobs.get(job_id, 60)
                    next_run = datetime.now(timezone.utc) + timedelta(
                        minutes=interval, seconds=random_offset
                    )
                    scheduler.reschedule_job(
                        job_id, trigger="interval", minutes=interval, start_date=next_run
                    )
                log.info("✓ [%s] → %s (+%ds)", account.phone, chat_id, random_offset)

            else:
                await db.commit()
                if need_failover:
                    result = await db.execute(
                        select(Task).options(selectinload(Task.user)).where(Task.id == task_id)
                    )
                    task = result.scalar_one_or_none()
                    if task:
                        await _try_failover(db, task, account_id, chat_id, error)


# ── Отправка через клиент ─────────────────────────────────────────────────────

async def _send_with_client(
    client,
    account: Account,
    task_id: int,
    chat_id: str,
    message_text: str,
    photo_file_ids: list[str],
    entities_json: list[dict],
) -> tuple[bool, str | None, bool]:
    """
    Выполняет отправку.
    Возвращает (success, error_text, need_failover).
    need_failover=True — нужно сразу переключить на другой аккаунт.
    """
    send_text    = _randomize_text(message_text)
    photo_bytes  = []
    if photo_file_ids:
        photo_bytes = await _download_photos(photo_file_ids)

    try:
        entity = await _resolve_entity(client, chat_id)
        if entity is None:
            return False, "entity_not_found", False

        entities = _to_telethon_entities(entities_json)

        if photo_bytes:
            for buf in photo_bytes:
                buf.seek(0)
            files = photo_bytes if len(photo_bytes) > 1 else photo_bytes[0]
            await client.send_file(
                entity,
                file=files,
                caption=send_text or "",
                formatting_entities=entities or None,
            )
        else:
            await client.send_message(
                entity,
                send_text or "",
                formatting_entities=entities or None,
            )
        return True, None, False

    except FloodWaitError as e:
        wait = min(e.seconds, 60)
        log.warning("FloodWait %ds для %s в %s — ждём", e.seconds, account.phone, chat_id)
        await asyncio.sleep(wait)

        # Повторная попытка после ожидания
        try:
            retry_text  = _randomize_text(message_text)
            photo_retry = []
            if photo_file_ids:
                photo_retry = await _download_photos(photo_file_ids)
            entity = await _resolve_entity(client, chat_id)
            if entity:
                entities = _to_telethon_entities(entities_json)
                if photo_retry:
                    for buf in photo_retry:
                        buf.seek(0)
                    files = photo_retry if len(photo_retry) > 1 else photo_retry[0]
                    await client.send_file(entity, file=files, caption=retry_text or "",
                                           formatting_entities=entities or None)
                else:
                    await client.send_message(entity, retry_text or "",
                                              formatting_entities=entities or None)
                return True, None, False
        except Exception as retry_e:
            return False, str(retry_e)[:200], False

    except (UserBannedInChannelError, ChatWriteForbiddenError) as e:
        err = f"{type(e).__name__}: {e}"
        log.warning("Нет доступа %s → %s: %s", account.phone, chat_id, err)
        return False, err, True  # ← нужен failover

    except (UserDeactivatedError, AuthKeyUnregisteredError) as e:
        err = f"account_frozen: {type(e).__name__}"
        log.error("Аккаунт %s заморожен/деактивирован", account.phone)
        # Помечаем в БД
        asyncio.create_task(_mark_account_frozen(account.id))
        await remove_client(account.id)
        return False, err, True  # ← нужен failover

    except Exception as e:
        err_str = str(e)
        err_low = err_str.lower()
        is_access_err = any(k in err_low for k in [
            "banned", "forbidden", "deactivated", "not allowed",
            "restricted", "kicked", "channel_private",
        ])
        log.error("Ошибка отправки %s → %s: %s", account.phone, chat_id, err_str)
        return False, err_str[:200], is_access_err


# ── Мгновенная замена аккаунта (failover) ────────────────────────────────────

async def _try_failover(
    db: AsyncSession,
    task: Task,
    failed_account_id: int,
    chat_id: str,
    error: str | None = None,
):
    """
    Ищет другой системный аккаунт и сразу отправляет через него.
    Перепривязывает чат в БД к новому аккаунту.
    Уведомляет пользователя только если замена не найдена.
    """
    from services.restriction_service import handle_chat_restriction

    # Ищем замену: системный аккаунт с наименьшей нагрузкой, у которого есть клиент в пуле
    result = await db.execute(
        select(Account).where(
            Account.is_system == True,
            Account.is_active == True,
            Account.is_banned == False,
            Account.status == "ok",
            Account.id != failed_account_id,
        ).order_by(Account.sends_last_hour.asc(), Account.chats_count.asc())
    )
    candidates = result.scalars().all()

    # Предпочитаем уже подключённых клиентов в пуле (без задержки connect)
    pool_candidates = [c for c in candidates if c.id in _client_pool]
    ordered = pool_candidates + [c for c in candidates if c.id not in pool_candidates]

    message_text    = task.message or ""
    photo_file_ids  = []
    format_entities = []
    try:
        photo_file_ids  = json.loads(task.photo_file_ids or "[]")
        format_entities = json.loads(task.format_entities or "[]")
    except Exception:
        pass

    for new_acc in ordered[:5]:  # не перебираем больше 5
        client = await get_client(new_acc)
        if client is None:
            continue

        sem = _get_semaphore(new_acc.id)
        async with sem:
            await _wait_rate_limit(new_acc.id)
            success, err, _ = await _send_with_client(
                client, new_acc, task.id, chat_id,
                message_text, photo_file_ids, format_entities,
            )
            _mark_sent(new_acc.id)

        if success:
            # Перепривязываем чат в БД
            await _reassign_chat(db, task.id, failed_account_id, new_acc.id, chat_id)
            await _increment_sends(db, new_acc)
            await db.commit()

            log.info(
                "✓ Failover: чат %s переведён с acc#%d на %s (task=%d)",
                chat_id, failed_account_id, new_acc.phone, task.id,
            )
            # Обновляем job в планировщике
            old_job_id = _make_job_id(task.id, failed_account_id, chat_id)
            new_job_id = _make_job_id(task.id, new_acc.id, chat_id)
            interval   = _loaded_jobs.get(old_job_id, task.interval_minutes)

            if scheduler.get_job(old_job_id):
                scheduler.remove_job(old_job_id)
            _loaded_jobs.pop(old_job_id, None)

            scheduler.add_job(
                send_chat_job,
                "interval",
                minutes=interval,
                id=new_job_id,
                args=[task.id, new_acc.id, chat_id],
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=interval),
                replace_existing=True,
            )
            _loaded_jobs[new_job_id] = interval

            db.add(Log(task_id=task.id, account_id=new_acc.id,
                       chat_id=chat_id, success=True, error="failover_success"))
            await db.commit()
            return

    # Замена не найдена — уведомляем пользователя
    bot = Bot(token=BOT_TOKEN)
    try:
        await handle_chat_restriction(
            db=db,
            account=await db.get(Account, failed_account_id),
            task_id=task.id,
            chat_id=chat_id,
            reason=error or "no_replacement",
            bot=bot,
        )
    except Exception as e:
        log.error("Ошибка handle_chat_restriction: %s", e)
    finally:
        await bot.session.close()


async def _reassign_chat(
    db: AsyncSession,
    task_id: int,
    old_account_id: int,
    new_account_id: int,
    chat_id: str,
):
    """Перепривязать chat_id с одного TaskAccount на другой."""
    # Убрать из старого
    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task_id,
            TaskAccount.account_id == old_account_id,
        )
    )
    old_ta = result.scalar_one_or_none()
    if old_ta:
        old_ids = json.loads(old_ta.chat_ids or "[]")
        old_ids = [x for x in old_ids if str(x) != str(chat_id)]
        if old_ids:
            old_ta.chat_ids = json.dumps(old_ids)
        else:
            await db.delete(old_ta)

    # Добавить в новый
    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task_id,
            TaskAccount.account_id == new_account_id,
        )
    )
    new_ta = result.scalar_one_or_none()
    if new_ta:
        new_ids = json.loads(new_ta.chat_ids or "[]")
        if str(chat_id) not in [str(x) for x in new_ids]:
            new_ids.append(str(chat_id))
        new_ta.chat_ids = json.dumps(new_ids)
    else:
        from models import TaskAccount as TA
        db.add(TA(task_id=task_id, account_id=new_account_id,
                  chat_ids=json.dumps([str(chat_id)])))

    # Обновить chats_count
    result_old = await db.execute(select(Account).where(Account.id == old_account_id))
    acc_old = result_old.scalar_one_or_none()
    if acc_old:
        acc_old.chats_count = max(0, acc_old.chats_count - 1)

    result_new = await db.execute(select(Account).where(Account.id == new_account_id))
    acc_new = result_new.scalar_one_or_none()
    if acc_new:
        acc_new.chats_count += 1


async def _mark_account_frozen(account_id: int):
    """Пометить аккаунт как заморожен в БД."""
    async with SessionLocal() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        acc = result.scalar_one_or_none()
        if acc and acc.status == "ok":
            acc.status    = "frozen"
            acc.is_banned = True
            acc.is_active = False
            await db.commit()
            log.warning("Аккаунт %s помечен как frozen", acc.phone)


# ── Вспомогательные функции ──────────────────────────────────────────────────

async def _download_photos(file_ids: list[str]) -> list[io.BytesIO]:
    if not file_ids:
        return []
    bot = Bot(token=BOT_TOKEN)
    results = []
    try:
        for i, file_id in enumerate(file_ids):
            try:
                tg_file = await bot.get_file(file_id)
                buf = io.BytesIO()
                await bot.download_file(tg_file.file_path, destination=buf)
                buf.seek(0)
                buf.name = f"photo_{i}.jpg"
                results.append(buf)
            except Exception as e:
                log.error("Не удалось скачать file_id %s: %s", file_id[:20], e)
    finally:
        await bot.session.close()
    return results


def _normalize_chat_id(chat_id: str) -> str:
    s = chat_id.strip()
    if s.startswith("@"):
        return f"@{s.lstrip('@')}"
    return s


async def _resolve_entity(client, chat_id: str):
    chat_id = _normalize_chat_id(chat_id)
    if chat_id.startswith("@"):
        try:
            return await client.get_entity(chat_id)
        except Exception:
            return None
    if chat_id.lstrip("-").isdigit():
        numeric = int(chat_id)
        try:
            return await client.get_entity(numeric)
        except Exception:
            pass
        if numeric > 0:
            try:
                return await client.get_entity(int(f"-100{numeric}"))
            except Exception:
                pass
        return None
    try:
        return await client.get_entity(chat_id)
    except Exception:
        return None


def _to_telethon_entities(entities_json: list[dict]) -> list:
    out = []
    for e in entities_json or []:
        t      = (e.get("type") or "").lower()
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


# ── Фоновые проверки ─────────────────────────────────────────────────────────

async def check_accounts():
    """Каждый час: проверяет авторизацию всех аккаунтов в пуле."""
    async with SessionLocal() as db:
        result = await db.execute(
            select(Account).where(Account.is_active == True, Account.is_banned == False)
        )
        accounts = result.scalars().all()

    for account in accounts:
        client = _client_pool.get(account.id)
        if client and client.is_connected():
            try:
                if not await client.is_user_authorized():
                    await _mark_account_frozen(account.id)
                    await remove_client(account.id)
            except Exception as e:
                log.error("check_accounts %s: %s", account.phone, e)


async def check_restrictions():
    """Каждые 30 минут: полная проверка ограничений."""
    try:
        from services.restriction_service import run_full_restriction_check
        await run_full_restriction_check()
    except Exception as e:
        log.error("check_restrictions: %s", e)


# ── Прогрев пула: подключить все системные аккаунты при старте ───────────────

async def warmup_pool():
    """При старте воркера — подключить все активные системные аккаунты."""
    async with SessionLocal() as db:
        result = await db.execute(
            select(Account).where(
                Account.is_system == True,
                Account.is_active == True,
                Account.is_banned == False,
                Account.status == "ok",
            )
        )
        accounts = result.scalars().all()

    log.info("Прогрев пула: %d системных аккаунтов...", len(accounts))
    for acc in accounts:
        await get_client(acc)
        await asyncio.sleep(0.5)  # небольшая пауза между коннектами
    log.info("Прогрев пула завершён. Подключено: %d", len(_client_pool))


# ── Точка входа ──────────────────────────────────────────────────────────────

async def main():
    await create_all_tables()

    # Прогреваем пул клиентов
    await warmup_pool()

    scheduler.add_job(sync_tasks,          "interval", seconds=30,  id="__sync__")
    scheduler.add_job(keepalive_clients,   "interval", minutes=5,   id="__keepalive__")
    scheduler.add_job(check_accounts,      "interval", hours=1,     id="__check_accs__")
    scheduler.add_job(reset_sends_counters,"interval", hours=1,     id="__reset_sends__")
    scheduler.add_job(check_restrictions,  "interval", minutes=30,  id="__restrictions__")

    scheduler.start()
    log.info("Воркер запущен (pool + rate-limit + instant failover).")

    await sync_tasks()

    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [WORKER] %(levelname)s: %(message)s",
    )
    asyncio.run(main())
