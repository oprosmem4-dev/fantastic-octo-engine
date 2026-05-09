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

ИСПРАВЛЕНО: photo_file_ids — aiogram Bot API file_id, Telethon не понимает
их напрямую. Теперь файлы скачиваются через Bot.get_file() в BytesIO
и передаются в client.send_file() как байты.
"""
import asyncio
import io
import json
import logging
from datetime import datetime, timezone
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
_loaded_tasks: dict[int, int] = {}
_restriction_check_running = False


# ── Загрузка фото через Bot API ───────────────────────────────────────────────

async def download_photos(file_ids: list[str]) -> list[io.BytesIO]:
    """
    Скачать фото по aiogram file_id через Bot API.
    Возвращает список BytesIO объектов готовых для передачи в Telethon.
    Файлы без расширения получат имя photo_N.jpg.
    """
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
                # Telethon определяет тип файла по имени атрибута name
                buf.name = f"photo_{i}.jpg"
                results.append(buf)
            except Exception as e:
                log.error("Не удалось скачать file_id %s: %s", file_id, e)
    finally:
        await bot.session.close()

    return results


# ── Главный цикл ──────────────────────────────────────────────────────────────

async def sync_tasks():
    async with SessionLocal() as db:
        result = await db.execute(
            select(Task).where(Task.is_active == True)
        )
        active_tasks = result.scalars().all()
        active_ids = {t.id for t in active_tasks}

        for task_id in list(_loaded_tasks.keys()):
            if task_id not in active_ids:
                job_id = f"task_{task_id}"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                del _loaded_tasks[task_id]
                log.info("Удалена задача %d из планировщика", task_id)

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


# ── Выполнение одной задачи ───────────────────────────────────────────────────

async def run_task(task_id: int):
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

        # ── Скачиваем фото ОДИН РАЗ для всех аккаунтов ───────────────────────
        # file_id из Bot API нельзя передать напрямую в Telethon,
        # поэтому скачиваем байты заранее и переиспользуем для каждого чата.
        message_text = task.message or ""
        try:
            photo_file_ids = json.loads(task.photo_file_ids or "[]")
        except Exception:
            photo_file_ids = []
        try:
            format_entities_json = json.loads(task.format_entities or "[]")
        except Exception:
            format_entities_json = []

        # Скачиваем фото в память (BytesIO) если они есть
        photo_bytes: list[io.BytesIO] = []
        if photo_file_ids:
            photo_bytes = await download_photos(photo_file_ids)
            if not photo_bytes:
                log.warning("Задача %d: не удалось скачать фото, отправим без них", task_id)

        for ta in task_accounts:
            await send_via_account(
                db, ta, task,
                message_text=message_text,
                photo_bytes=photo_bytes,
                entities_json=format_entities_json,
            )

        task.last_run_at = datetime.now(timezone.utc)
        await db.commit()


async def send_via_account(
    db: AsyncSession,
    ta: TaskAccount,
    task: Task,
    message_text: str,
    photo_bytes: list[io.BytesIO],
    entities_json: list[dict],
):
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

    client = make_client(account)
    try:
        await client.connect()
        await asyncio.sleep(1)

        if not await client.is_user_authorized():
            log.warning("Аккаунт %s не авторизован", account.phone)
            return

        await client.get_dialogs()

        for chat_id in chat_ids:
            # Перематываем BytesIO в начало перед каждой отправкой
            for buf in photo_bytes:
                buf.seek(0)

            await send_to_chat(
                db, client, account, ta.task_id, chat_id,
                message_text=message_text,
                photo_bytes=photo_bytes,
                entities_json=entities_json,
            )
            await asyncio.sleep(2)

    except Exception as e:
        log.error("Ошибка аккаунта %s: %s", account.phone, e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def send_to_chat(
    db: AsyncSession,
    client,
    account: Account,
    task_id: int,
    chat_id: str,
    message_text: str,
    photo_bytes: list[io.BytesIO],
    entities_json: list[dict],
):
    """
    Отправить одно сообщение в один чат.

    photo_bytes — список BytesIO уже скачанных фото.
    При каждом вызове seek(0) должен быть сделан снаружи.
    """
    success = False
    error_text = None

    try:
        entity = await resolve_entity(client, chat_id)
        if entity is None:
            error_text = "не удалось найти чат"
            log.warning("Не удалось найти чат %s", chat_id)
        else:
            entities = _to_telethon_entities(entities_json)
            if photo_bytes:
                # Одно фото — send_file с одним файлом
                # Несколько — send_file принимает список (альбом)
                files = photo_bytes if len(photo_bytes) > 1 else photo_bytes[0]
                await client.send_file(
                    entity,
                    file=files,
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
        # Перематываем перед повтором
        for buf in photo_bytes:
            buf.seek(0)
        try:
            entity = await resolve_entity(client, chat_id)
            if entity:
                entities = _to_telethon_entities(entities_json)
                if photo_bytes:
                    files = photo_bytes if len(photo_bytes) > 1 else photo_bytes[0]
                    await client.send_file(
                        entity,
                        file=files,
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

    except (UserBannedInChannelError, ChatWriteForbiddenError) as e:
        error_text = f"нет доступа: {type(e).__name__}"
        log.warning("Нет доступа к %s через %s", chat_id, account.phone)
        asyncio.create_task(
            check_account_on_send_error(account.id, task_id, chat_id, e)
        )

    except Exception as e:
        error_text = str(e)
        log.error("Ошибка отправки в %s: %s", chat_id, e)
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
    """
    Найти чат по ID или username.
    """
    chat_id = str(chat_id).strip()

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


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
    await create_all_tables()

    scheduler.add_job(sync_tasks, "interval", seconds=30, id="__sync__")
    scheduler.add_job(check_accounts, "interval", hours=1, id="__check_accs__")
    scheduler.add_job(check_restrictions, "interval", minutes=30, id="__restrictions__")

    scheduler.start()
    log.info("Воркер запущен.")

    await sync_tasks()
    asyncio.get_event_loop().call_later(60, lambda: asyncio.create_task(check_restrictions()))

    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [WORKER] %(levelname)s: %(message)s"
    )
    asyncio.run(main())
