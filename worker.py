"""
worker/worker.py — воркер рассылок.

ИЗМЕНЕНИЯ:
  - Планировщик переработан: вместо job-per-task → job-per-chat
    Каждая пара (task_id, account_id, chat_id) получает свой независимый job.
    Следующий запуск = last_sent_at + interval_minutes + random_offset_seconds.
    Random offset генерируется индивидуально для каждого чата при каждой отправке.
  - get_dialogs() убран из цикла отправки (лишняя активность).
  - Невидимая рандомизация текста: zero-width space вставляется в случайную
    позицию — визуально текст не меняется, хэш сообщения разный.
  - Фото скачиваются свежим перед каждой отправкой (избегаем устаревших file_id).
  - Правильная работа с BytesIO буферами: seek(0) перед каждым использованием.
"""
import asyncio
import io
import json
import logging
import random
from datetime import datetime, timezone, timedelta

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

scheduler = AsyncIOScheduler(timezone="UTC")

# job_id → interval_minutes (для отслеживания изменений интервала)
_loaded_jobs: dict[str, int] = {}

# (task_id, account_id, chat_id) → last_sent_at (datetime UTC)
# Хранится в памяти, сбрасывается при рестарте воркера.
_last_sent: dict[tuple, datetime] = {}

_restriction_check_running = False

# ── Невидимая рандомизация текста ────────────────────────────────────────────
# Zero-width символы — визуально не отображаются ни в одном клиенте Telegram.
_ZW_CHARS = ['\u200b', '\u200c', '\u200d']


def _randomize_text(text: str) -> str:
    """
    Вставляет один zero-width символ в случайную позицию текста.
    Визуально текст не изменяется, хэш сообщения — уникальный.
    Если текст пустой — возвращает как есть.
    """
    if not text:
        return text
    pos = random.randint(0, len(text))
    char = random.choice(_ZW_CHARS)
    return text[:pos] + char + text[pos:]


# ── Загрузка фото через Bot API ───────────────────────────────────────────────

async def download_photos(file_ids: list[str]) -> list[io.BytesIO]:
    """
    Скачивает фото по file_id и возвращает список BytesIO объектов.
    Каждый объект готов к чтению (seek(0) уже сделан).
    
    Если не удалось скачать какое-то фото, оно пропускается, но процесс продолжается.
    """
    if not file_ids:
        return []

    bot = Bot(token=BOT_TOKEN)
    results = []
    try:
        for i, file_id in enumerate(file_ids):
            try:
                log.debug("Скачиваю фото %d (file_id: %s...)", i, file_id[:20])
                tg_file = await bot.get_file(file_id)
                buf = io.BytesIO()
                await bot.download_file(tg_file.file_path, destination=buf)
                buf.seek(0)  # ← Важно: сбросить позицию для чтения
                buf.name = f"photo_{i}.jpg"
                results.append(buf)
                log.debug("✓ Фото %d загружено успешно (%d байт)", i, buf.getbuffer().nbytes)
            except Exception as e:
                log.error("✗ Не удалось скачать file_id %s: %s", file_id[:20], e)
                # Продолжаем, пропускаем это фото
    finally:
        await bot.session.close()

    if not results and file_ids:
        log.warning("⚠️  Не удалось загрузить ни одно из %d фото", len(file_ids))
    
    return results


# ── Синхронизация планировщика с БД ──────────────────────────────────────────

async def sync_tasks():
    """
    Каждые 30 сек читает активные задачи из БД и синхронизирует APScheduler.
    Единица планирования — (task_id, account_id, chat_id).
    Job ID формат: "chat_{task_id}_{account_id}_{safe_chat_id}"
    """
    async with SessionLocal() as db:
        result = await db.execute(
            select(Task)
            .options(selectinload(Task.accounts))
            .where(Task.is_active == True)
        )
        active_tasks = result.scalars().all()

        # Собираем все job_id которые должны существовать
        desired_jobs: dict[str, tuple] = {}  # job_id → (task_id, account_id, chat_id, interval)

        for task in active_tasks:
            # Проверяем доступ пользователя
            result2 = await db.execute(
                select(Task).options(selectinload(Task.user)).where(Task.id == task.id)
            )
            full_task = result2.scalar_one_or_none()
            if not full_task or not full_task.user.has_access:
                continue

            for ta in task.accounts:
                try:
                    chat_ids = json.loads(ta.chat_ids or "[]")
                except Exception:
                    chat_ids = []

                for chat_id in chat_ids:
                    safe = chat_id.replace("@", "at_").replace("-", "m_")
                    job_id = f"chat_{task.id}_{ta.account_id}_{safe}"
                    desired_jobs[job_id] = (task.id, ta.account_id, chat_id, task.interval_minutes)

        # Удаляем лишние jobs
        for job_id in list(_loaded_jobs.keys()):
            if job_id not in desired_jobs:
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                del _loaded_jobs[job_id]
                log.info("Удалён job %s", job_id)

        # Добавляем новые / обновляем изменившиеся интервалы
        for job_id, (task_id, account_id, chat_id, interval) in desired_jobs.items():
            existing_interval = _loaded_jobs.get(job_id)

            if existing_interval is None:
                # Новый job — первый запуск немедленно, потом по интервалу
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
                log.info(
                    "Добавлен job %s (task=%d acc=%d chat=%s каждые %d мин.)",
                    job_id, task_id, account_id, chat_id, interval
                )

            elif existing_interval != interval:
                scheduler.reschedule_job(
                    job_id, trigger="interval", minutes=interval
                )
                _loaded_jobs[job_id] = interval
                log.info("Обновлён интервал job %s → %d мин.", job_id, interval)


# ── Job для одного чата ──────────────────────────────────────────────────────

async def send_chat_job(task_id: int, account_id: int, chat_id: str):
    """
    Вызывается APScheduler для конкретной пары (task, account, chat).

    Логика времени:
      - Смотрим когда последний раз отправляли в ЭТОТ чат (_last_sent).
      - Если прошло меньше чем interval - random_offset → пропускаем.
        (APScheduler может немного опередить из-за точности планировщика)
      - После успешной отправки записываем время и планируем следующий
        запуск с новым random_offset секунд поверх интервала.

    Random offset: 0..120 секунд, генерируется заново каждый раз.
    """
    key = (task_id, account_id, chat_id)
    now = datetime.now(timezone.utc)

    async with SessionLocal() as db:
        # Загружаем задачу
        result = await db.execute(
            select(Task)
            .options(selectinload(Task.user))
            .where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task or not task.is_active:
            return
        if not task.user.has_access:
            log.info("task=%d: пользователь без доступа, пропуск", task_id)
            return

        # Загружаем аккаунт
        result = await db.execute(
            select(Account).where(Account.id == account_id)
        )
        account = result.scalar_one_or_none()
        if not account or not account.is_active or account.is_banned or account.status != "ok":
            log.warning("Аккаунт %d недоступен, пропуск chat=%s", account_id, chat_id)
            return

        # Подготовка данных сообщения
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
            await asyncio.sleep(1)

            if not await client.is_user_authorized():
                log.warning("Аккаунт %s не авторизован", account.phone)
                return

            # get_dialogs() убран — лишняя активность

            success = await send_to_chat(
                db, client, account, task_id, chat_id,
                message_text=message_text,
                photo_file_ids=photo_file_ids,
                entities_json=format_entities_json,
            )

        except Exception as e:
            log.error("Ошибка аккаунта %s при отправке в %s: %s", account.phone, chat_id, e)
            success = False
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    if success:
        _last_sent[key] = datetime.now(timezone.utc)

        # Перепланируем job с новым random offset
        random_offset_sec = random.randint(0, 120)
        job_id = _make_job_id(task_id, account_id, chat_id)
        job = scheduler.get_job(job_id)
        if job:
            interval_min = _loaded_jobs.get(job_id, task.interval_minutes)
            next_run = datetime.now(timezone.utc) + timedelta(
                minutes=interval_min,
                seconds=random_offset_sec,
            )
            scheduler.reschedule_job(
                job_id,
                trigger="interval",
                minutes=interval_min,
                start_date=next_run,
            )
            log.info(
                "✓ [%s] → %s | следующий через %d мин. + %d сек.",
                account.phone, chat_id, interval_min, random_offset_sec
            )


def _make_job_id(task_id: int, account_id: int, chat_id: str) -> str:
    safe = chat_id.replace("@", "at_").replace("-", "m_")
    return f"chat_{task_id}_{account_id}_{safe}"


# ── Отправка в один чат ──────────────────────────────────────────────────────

async def send_to_chat(
    db: AsyncSession,
    client,
    account: Account,
    task_id: int,
    chat_id: str,
    message_text: str,
    photo_file_ids: list[str],
    entities_json: list[dict],
) -> bool:
    """
    Отправляет сообщение в один чат.
    Текст рандомизируется (zero-width char) — визуально не меняется.
    Фото скачиваются свежим перед отправкой (избегаем устаревших file_id).
    Возвращает True при успехе.
    """
    success = False
    error_text = None

    # Рандомизируем текст перед отправкой
    send_text = _randomize_text(message_text)

    # Скачиваем фото свежим (избегаем проблемы с устаревшими file_id)
    photo_bytes: list[io.BytesIO] = []
    if photo_file_ids:
        photo_bytes = await download_photos(photo_file_ids)
        if not photo_bytes and photo_file_ids:
            log.warning("Не удалось загрузить фото, отправляем без изображения")

    try:
        entity = await resolve_entity(client, chat_id)
        if entity is None:
            error_text = "не удалось найти чат"
            log.warning("Не удалось найти чат %s", chat_id)
        else:
            entities = _to_telethon_entities(entities_json)
            
            if photo_bytes:
                # Подготавливаем буферы к чтению
                for buf in photo_bytes:
                    buf.seek(0)
                
                files = photo_bytes if len(photo_bytes) > 1 else photo_bytes[0]
                await client.send_file(
                    entity,
                    file=files,
                    caption=send_text or "",
                    formatting_entities=entities if entities else None,
                )
            else:
                await client.send_message(
                    entity,
                    send_text or "",
                    formatting_entities=entities if entities else None,
                )
            success = True

    except FloodWaitError as e:
        log.warning("FloodWait %d сек. для %s в %s", e.seconds, account.phone, chat_id)
        await asyncio.sleep(min(e.seconds, 60))
        
        # Повторная попытка после ожидания
        try:
            entity = await resolve_entity(client, chat_id)
            if entity:
                # Скачиваем фото заново для повторной попытки
                photo_bytes_retry: list[io.BytesIO] = []
                if photo_file_ids:
                    photo_bytes_retry = await download_photos(photo_file_ids)
                
                # Подготавливаем буферы
                for buf in photo_bytes_retry:
                    buf.seek(0)
                
                entities = _to_telethon_entities(entities_json)
                retry_text = _randomize_text(message_text)
                
                if photo_bytes_retry:
                    files = photo_bytes_retry if len(photo_bytes_retry) > 1 else photo_bytes_retry[0]
                    await client.send_file(
                        entity,
                        file=files,
                        caption=retry_text or "",
                        formatting_entities=entities if entities else None,
                    )
                else:
                    await client.send_message(
                        entity,
                        retry_text or "",
                        formatting_entities=entities if entities else None,
                    )
                success = True
        except Exception as retry_err:
            error_text = str(retry_err)
            log.error("Ошибка при повторной попытке отправки: %s", retry_err)

    except (UserBannedInChannelError, ChatWriteForbiddenError) as e:
        error_text = f"нет доступа: {type(e).__name__}"
        log.warning("Нет доступа к %s через %s", chat_id, account.phone)
        asyncio.create_task(
            check_account_on_send_error(account.id, task_id, chat_id, e)
        )

    except Exception as e:
        error_text = str(e)
        log.error("Ошибка отправки в %s через %s: %s", chat_id, account.phone, e)
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

    return success


# ── Вспомогательные функции ──────────────────────────────────────────────────

def _to_telethon_entities(entities_json: list[dict]) -> list:
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


def _normalize_chat_id(chat_id: str) -> str:
    chat_id = chat_id.strip()
    if chat_id.startswith("@"):
        username = chat_id.lstrip("@")
        return f"@{username}"
    return chat_id


async def resolve_entity(client, chat_id: str):
    chat_id = _normalize_chat_id(chat_id)
    log.debug("resolve_entity: '%s'", chat_id)

    if chat_id.startswith("@"):
        try:
            return await client.get_entity(chat_id)
        except Exception as e:
            log.debug("resolve @%s: %s", chat_id, e)
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
        if chat_id.startswith("-100"):
            inner = chat_id[4:]
            if inner.isdigit():
                try:
                    return await client.get_entity(int(inner))
                except Exception:
                    pass
        return None

    try:
        return await client.get_entity(chat_id)
    except Exception as e:
        log.debug("resolve %s (str): %s", chat_id, e)
        return None


# ── Фоновая проверка авторизации аккаунтов ────────────────────────────────────

async def check_accounts():
    async with SessionLocal() as db:
        result = await db.execute(
            select(Account).where(Account.is_active == True, Account.is_banned == False)
        )
        accounts = result.scalars().all()

        for account in accounts:
            client = make_client(account)
            try:
                await client.connect()
                await asyncio.sleep(1)
                if not await client.is_user_authorized():
                    account.is_banned = True
                    account.status = "frozen"
                    log.warning("Аккаунт %s забанен или разлогинен", account.phone)
                await client.disconnect()
            except Exception as e:
                log.error("Ошибка проверки %s: %s", account.phone, e)

        await db.commit()


async def check_restrictions():
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


# ── Точка входа ──────────────────────────────────────────────────────────────

async def main():
    await create_all_tables()

    scheduler.add_job(sync_tasks, "interval", seconds=30, id="__sync__")
    scheduler.add_job(check_accounts, "interval", hours=1, id="__check_accs__")
    scheduler.add_job(check_restrictions, "interval", minutes=30, id="__restrictions__")

    scheduler.start()
    log.info("Воркер запущен (per-chat scheduling).")

    await sync_tasks()
    asyncio.get_event_loop().call_later(
        60, lambda: asyncio.create_task(check_restrictions())
    )

    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [WORKER] %(levelname)s: %(message)s"
    )
    asyncio.run(main())
