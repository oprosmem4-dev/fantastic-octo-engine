"""
worker/worker.py — воркер рассылок.

ИЗМЕНЕНИЯ (медиа-рефакторинг):
  - _get_media_for_send(): главная функция получения медиа для отправки.
    1. Проверяет кеш TaskMediaCache для данного аккаунта
    2. Если кеш есть — возвращает список file_id (строки) → отправка через send_file(file_id)
    3. Если кеша нет — читает байты из TaskMedia → send_file(BytesIO)
       → получает Telethon Document из отправленного сообщения
       → сохраняет file_id в TaskMediaCache
       → удаляет строки TaskMedia (байты больше не нужны)
  - _send_with_client(): использует _get_media_for_send() вместо Bot API скачивания
  - Убраны _download_photos(), _download_photos_via_telethon() — больше не нужны
"""
import asyncio
import io
import json
import logging
import random
import time
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, delete
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
from models import Task, TaskAccount, TaskMedia, TaskMediaCache, Account, Log
from services.account_service import make_client

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")

# ── Пул клиентов ──────────────────────────────────────────────────────────────
_client_pool: dict[int, object] = {}
_pool_lock = asyncio.Lock()

# ── Rate limiter ───────────────────────────────────────────────────────────────
_account_semaphores: dict[int, asyncio.Semaphore] = {}
_account_last_send:  dict[int, float] = {}

# ── Отслеживание jobs ─────────────────────────────────────────────────────────
_loaded_jobs: dict[str, int] = {}

# ── zero-width символы для невидимой рандомизации ─────────────────────────────
_ZW_CHARS = ['\u200b', '\u200c', '\u200d']


def _randomize_text(text: str) -> str:
    if not text:
        return text
    pos  = random.randint(0, len(text))
    char = random.choice(_ZW_CHARS)
    return text[:pos] + char + text[pos:]


# ── Пул клиентов: управление ──────────────────────────────────────────────────

async def get_client(account: Account):
    async with _pool_lock:
        client = _client_pool.get(account.id)
        if client is not None and client.is_connected():
            return client

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
    async with _pool_lock:
        client = _client_pool.pop(account_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


async def keepalive_clients():
    async with _pool_lock:
        dead = [aid for aid, c in _client_pool.items() if not c.is_connected()]

    for aid in dead:
        async with SessionLocal() as db:
            result = await db.execute(
                select(Account).where(
                    Account.id == aid,
                    Account.is_active == True,
                    Account.is_banned == False,
                    Account.status == "ok",
                )
            )
            acc = result.scalar_one_or_none()
        if acc:
            log.info("Keepalive: реконнект %s", acc.phone)
            await get_client(acc)
        else:
            async with _pool_lock:
                _client_pool.pop(aid, None)


# ── Rate limiter ───────────────────────────────────────────────────────────────

def _get_semaphore(account_id: int) -> asyncio.Semaphore:
    if account_id not in _account_semaphores:
        _account_semaphores[account_id] = asyncio.Semaphore(1)
    return _account_semaphores[account_id]


async def _wait_rate_limit(account_id: int):
    last = _account_last_send.get(account_id, 0)
    wait = MIN_SEND_INTERVAL - (time.monotonic() - last)
    if wait > 0:
        await asyncio.sleep(wait)


def _mark_sent(account_id: int):
    _account_last_send[account_id] = time.monotonic()


# ── Счётчик отправок ───────────────────────────────────────────────────────────

async def _increment_sends(db: AsyncSession, account: Account):
    now = datetime.now(timezone.utc)
    if account.sends_reset_at is None or (now - account.sends_reset_at).total_seconds() > 3600:
        account.sends_last_hour = 0
        account.sends_reset_at  = now
    account.sends_last_hour += 1


async def reset_sends_counters():
    async with SessionLocal() as db:
        result = await db.execute(select(Account))
        accounts = result.scalars().all()
        for acc in accounts:
            acc.sends_last_hour = 0
            acc.sends_reset_at  = datetime.now(timezone.utc)
        await db.commit()
    log.info("Счётчики sends_last_hour сброшены.")


# ── Медиа: получить данные для отправки ───────────────────────────────────────

async def _get_media_for_send(
    db: AsyncSession,
    task_id: int,
    account_id: int,
) -> tuple[list, bool]:
    """
    Вернуть медиа-данные для отправки.

    Алгоритм:
      1. Смотрим кеш TaskMediaCache для этого (task_id, account_id).
         Если есть — возвращаем список file_id строк. is_cached=True.
      2. Если кеша нет — читаем байты из TaskMedia.
         Возвращаем список BytesIO объектов. is_cached=False.

    Возвращает: (media_list, is_cached)
      - media_list: [] если нет медиа
      - is_cached=True  → элементы это строки file_id (Telethon)
      - is_cached=False → элементы это BytesIO объекты
    """
    # Проверяем кеш
    result = await db.execute(
        select(TaskMediaCache)
        .where(
            TaskMediaCache.task_id == task_id,
            TaskMediaCache.account_id == account_id,
        )
        .order_by(TaskMediaCache.index)
    )
    cache_rows = result.scalars().all()

    if cache_rows:
        return [row.file_id for row in cache_rows], True

    # Кеша нет — читаем байты
    result = await db.execute(
        select(TaskMedia)
        .where(TaskMedia.task_id == task_id)
        .order_by(TaskMedia.index)
    )
    media_rows = result.scalars().all()

    if not media_rows:
        return [], False

    bufs = []
    for row in media_rows:
        buf = io.BytesIO(row.data)
        buf.name = f"photo_{row.index}.jpg"
        bufs.append(buf)

    return bufs, False


async def _save_media_cache_and_cleanup(
    db: AsyncSession,
    task_id: int,
    account_id: int,
    sent_messages,  # результат client.send_file() — одно сообщение или список
):
    """
    После успешной отправки байт:
      1. Извлекаем file_id из отправленных сообщений
      2. Сохраняем в TaskMediaCache
      3. Удаляем строки из TaskMedia

    sent_messages может быть одним сообщением или списком (медиагруппа).
    """
    if not isinstance(sent_messages, (list, tuple)):
        sent_messages = [sent_messages]

    for idx, msg in enumerate(sent_messages):
        # Получаем file_id из документа или фото
        file_id = None
        if hasattr(msg, "media") and msg.media:
            media = msg.media
            if hasattr(media, "document") and media.document:
                # Сохраняем как строку id + access_hash чтобы Telethon мог резолвить
                doc = media.document
                file_id = f"doc:{doc.id}:{doc.access_hash}:{doc.file_reference.hex()}"
            elif hasattr(media, "photo") and media.photo:
                photo = media.photo
                # Берём наибольший размер
                sizes = getattr(photo, "sizes", [])
                if sizes:
                    biggest = max(
                        (s for s in sizes if hasattr(s, "size")),
                        key=lambda s: getattr(s, "size", 0),
                        default=sizes[-1],
                    )
                    file_id = f"photo:{photo.id}:{photo.access_hash}:{photo.file_reference.hex()}:{biggest.type}"

        if file_id:
            db.add(TaskMediaCache(
                task_id=task_id,
                account_id=account_id,
                index=idx,
                file_id=file_id,
            ))

    # Удаляем байты из TaskMedia — они больше не нужны
    await db.execute(
        delete(TaskMedia).where(TaskMedia.task_id == task_id)
    )
    log.info("Задача %d: байты фото удалены из TaskMedia, кеш сохранён для acc=%d",
             task_id, account_id)


async def _telethon_file_from_cache(client, file_id_str: str):
    """
    Преобразовать строку кеша обратно в объект который Telethon может отправить.
    Форматы:
      doc:id:access_hash:file_reference_hex
      photo:id:access_hash:file_reference_hex:thumb_type
    """
    try:
        parts = file_id_str.split(":")
        kind = parts[0]

        if kind == "doc":
            _, doc_id, access_hash, file_ref_hex = parts
            return tl_types.InputDocument(
                id=int(doc_id),
                access_hash=int(access_hash),
                file_reference=bytes.fromhex(file_ref_hex),
            )
        elif kind == "photo":
            _, photo_id, access_hash, file_ref_hex, thumb_type = parts
            return tl_types.InputPhoto(
                id=int(photo_id),
                access_hash=int(access_hash),
                file_reference=bytes.fromhex(file_ref_hex),
            )
    except Exception as e:
        log.warning("Не удалось восстановить file_id из кеша '%s': %s", file_id_str[:40], e)
    return None


# ── Синхронизация планировщика ─────────────────────────────────────────────────

async def sync_tasks():
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

        for job_id in list(_loaded_jobs.keys()):
            if job_id not in desired_jobs:
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                del _loaded_jobs[job_id]

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


# ── Основной job ───────────────────────────────────────────────────────────────

async def send_chat_job(task_id: int, account_id: int, chat_id: str):
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
            await _try_failover(db, task, account_id, chat_id)
            return

        message_text = task.message or ""
        has_media    = task.has_media

        try:
            format_entities = json.loads(task.format_entities or "[]")
        except Exception:
            format_entities = []

    sem = _get_semaphore(account_id)
    async with sem:
        await _wait_rate_limit(account_id)

        client = await get_client(account)
        if client is None:
            async with SessionLocal() as db:
                result = await db.execute(
                    select(Task).options(selectinload(Task.user)).where(Task.id == task_id)
                )
                task = result.scalar_one_or_none()
                if task:
                    await _try_failover(db, task, account_id, chat_id)
            return

        success, error, need_failover = await _send_with_client(
            client, account, task_id, chat_id,
            message_text, has_media, format_entities,
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


# ── Отправка через клиент ──────────────────────────────────────────────────────

async def _send_with_client(
    client,
    account: Account,
    task_id: int,
    chat_id: str,
    message_text: str,
    has_media: bool,
    entities_json: list[dict],
) -> tuple[bool, str | None, bool]:
    """
    Отправить сообщение в один чат.

    Логика медиа:
      1. Если has_media=False — отправляем только текст.
      2. Проверяем кеш TaskMediaCache для этого аккаунта.
         - Кеш есть → строим InputPhoto/InputDocument → send_file() без загрузки байт.
         - Кеша нет → читаем байты из TaskMedia → send_file(BytesIO)
           → сохраняем кеш → удаляем байты из TaskMedia.
    """
    send_text = _randomize_text(message_text)
    entities  = _to_telethon_entities(entities_json)

    try:
        entity = await _resolve_entity(client, chat_id)
        if entity is None:
            return False, "entity_not_found", False

        if not has_media:
            # Только текст
            await client.send_message(
                entity, send_text or "",
                formatting_entities=entities or None,
            )
            return True, None, False

        # ── Медиа: получаем данные для отправки ──────────────────────────────
        async with SessionLocal() as db:
            media_list, is_cached = await _get_media_for_send(db, task_id, account.id)

        if not media_list:
            # Медиа нет (байты удалены, кеш тоже пуст) — отправляем только текст
            log.warning("Задача %d: медиа не найдено, отправляем только текст", task_id)
            await client.send_message(
                entity, send_text or "",
                formatting_entities=entities or None,
            )
            return True, None, False

        if is_cached:
            # ── Кеш есть: строим Telethon-объекты из строк кеша ──────────────
            tl_files = []
            for file_id_str in media_list:
                tl_obj = await _telethon_file_from_cache(client, file_id_str)
                if tl_obj:
                    tl_files.append(tl_obj)

            if not tl_files:
                # Кеш испорчен — отправляем текст
                log.warning("Задача %d: кеш file_id не удалось восстановить", task_id)
                await client.send_message(
                    entity, send_text or "",
                    formatting_entities=entities or None,
                )
                return True, None, False

            if len(tl_files) == 1:
                await client.send_file(
                    entity, file=tl_files[0],
                    caption=send_text or "",
                    formatting_entities=entities or None,
                )
            else:
                await client.send_file(
                    entity, file=tl_files,
                    caption=send_text or "",
                    formatting_entities=entities or None,
                )
            return True, None, False

        else:
            # ── Кеша нет: первая отправка через байты ────────────────────────
            # Сбрасываем позицию BytesIO перед отправкой
            for buf in media_list:
                buf.seek(0)

            if len(media_list) == 1:
                sent = await client.send_file(
                    entity, file=media_list[0],
                    caption=send_text or "",
                    formatting_entities=entities or None,
                )
            else:
                sent = await client.send_file(
                    entity, file=media_list,
                    caption=send_text or "",
                    formatting_entities=entities or None,
                )

            # Сохраняем кеш и удаляем байты
            async with SessionLocal() as db:
                await _save_media_cache_and_cleanup(db, task_id, account.id, sent)
                await db.commit()

            return True, None, False

    except FloodWaitError as e:
        wait = min(e.seconds, 60)
        log.warning("FloodWait %ds для %s в %s", e.seconds, account.phone, chat_id)
        await asyncio.sleep(wait)

        # Повтор после ожидания (упрощённый — только текст чтобы не усложнять)
        try:
            retry_text = _randomize_text(message_text)
            entity     = await _resolve_entity(client, chat_id)
            if entity:
                await client.send_message(
                    entity, retry_text or "",
                    formatting_entities=_to_telethon_entities(entities_json) or None,
                )
                return True, None, False
        except Exception as retry_e:
            return False, str(retry_e)[:200], False

    except (UserBannedInChannelError, ChatWriteForbiddenError) as e:
        err = f"{type(e).__name__}: {e}"
        log.warning("Нет доступа %s → %s: %s", account.phone, chat_id, err)
        return False, err, True

    except (UserDeactivatedError, AuthKeyUnregisteredError) as e:
        err = f"account_frozen: {type(e).__name__}"
        log.error("Аккаунт %s заморожен/деактивирован", account.phone)
        asyncio.create_task(_mark_account_frozen(account.id))
        await remove_client(account.id)
        return False, err, True

    except Exception as e:
        err_str = str(e)
        err_low = err_str.lower()
        is_access_err = any(k in err_low for k in [
            "banned", "forbidden", "deactivated", "not allowed",
            "restricted", "kicked", "channel_private",
        ])
        log.error("Ошибка отправки %s → %s: %s", account.phone, chat_id, err_str)
        return False, err_str[:200], is_access_err

    return False, "unknown", False


# ── Failover ───────────────────────────────────────────────────────────────────

async def _try_failover(
    db: AsyncSession,
    task: Task,
    failed_account_id: int,
    chat_id: str,
    error: str | None = None,
):
    from services.restriction_service import handle_chat_restriction

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

    pool_candidates = [c for c in candidates if c.id in _client_pool]
    ordered = pool_candidates + [c for c in candidates if c.id not in pool_candidates]

    message_text    = task.message or ""
    has_media       = task.has_media
    format_entities = []
    try:
        format_entities = json.loads(task.format_entities or "[]")
    except Exception:
        pass

    for new_acc in ordered[:5]:
        client = await get_client(new_acc)
        if client is None:
            continue

        sem = _get_semaphore(new_acc.id)
        async with sem:
            await _wait_rate_limit(new_acc.id)
            success, err, _ = await _send_with_client(
                client, new_acc, task.id, chat_id,
                message_text, has_media, format_entities,
            )
            _mark_sent(new_acc.id)

        if success:
            await _reassign_chat(db, task.id, failed_account_id, new_acc.id, chat_id)
            await _increment_sends(db, new_acc)
            await db.commit()

            log.info("✓ Failover: %s → %s (task=%d)", chat_id, new_acc.phone, task.id)

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

    result_old = await db.execute(select(Account).where(Account.id == old_account_id))
    acc_old = result_old.scalar_one_or_none()
    if acc_old:
        acc_old.chats_count = max(0, acc_old.chats_count - 1)

    result_new = await db.execute(select(Account).where(Account.id == new_account_id))
    acc_new = result_new.scalar_one_or_none()
    if acc_new:
        acc_new.chats_count += 1


async def _mark_account_frozen(account_id: int):
    async with SessionLocal() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        acc = result.scalar_one_or_none()
        if acc and acc.status == "ok":
            acc.status    = "frozen"
            acc.is_banned = True
            acc.is_active = False
            await db.commit()
            log.warning("Аккаунт %s помечен как frozen", acc.phone)


# ── Вспомогательные функции ────────────────────────────────────────────────────

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


# ── Фоновые проверки ──────────────────────────────────────────────────────────

async def check_accounts():
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
    try:
        from services.restriction_service import run_full_restriction_check
        await run_full_restriction_check()
    except Exception as e:
        log.error("check_restrictions: %s", e)


async def warmup_pool():
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
        await asyncio.sleep(0.5)
    log.info("Прогрев пула завершён. Подключено: %d", len(_client_pool))


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
    await create_all_tables()
    await warmup_pool()

    scheduler.add_job(sync_tasks,           "interval", seconds=30,  id="__sync__")
    scheduler.add_job(keepalive_clients,    "interval", minutes=5,   id="__keepalive__")
    scheduler.add_job(check_accounts,       "interval", hours=1,     id="__check_accs__")
    scheduler.add_job(reset_sends_counters, "interval", hours=1,     id="__reset_sends__")
    scheduler.add_job(check_restrictions,   "interval", minutes=30,  id="__restrictions__")

    scheduler.start()
    log.info("Воркер запущен.")

    await sync_tasks()

    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [WORKER] %(levelname)s: %(message)s",
    )
    asyncio.run(main())
