"""
services/restriction_service.py — обнаружение и обработка ограничений на аккаунтах.

ИСПРАВЛЕНИЯ:
  - _normalize_chat_id: нормализует @@username → @username перед любым использованием
  - check_chat_access_light: нормализует chat_id на входе
  - _check_account_chat_access: нормализует chat_id из БД перед проверкой
  - check_account_on_send_error: нормализует chat_id на входе
  - handle_chat_restriction: нормализует chat_id на входе
"""
import asyncio
import json
import logging
import time
from typing import Optional

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
)
from telethon.tl import types as tl_types
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.messages import GetHistoryRequest

from config import OWNER_ID, BOT_TOKEN, SPAMCHECK_USERNAME
from models import Account, Task, TaskAccount, TaskChat
from services.account_service import make_client

log = logging.getLogger(__name__)

_SPAMCHECK_COOLDOWN = 1800  # 30 минут
_last_spamcheck: dict[int, float] = {}


def _get_bot() -> Bot:
    return Bot(token=BOT_TOKEN)


def _normalize_chat_id(chat_id: str) -> str:
    """
    Нормализовать chat_id:
      @@username  → @username
      @@@username → @username
      -100123456  → -100123456  (без изменений)
    """
    s = str(chat_id).strip()
    if s.startswith("@"):
        username = s.lstrip("@")
        return f"@{username}"
    return s


# ─────────────────────────────────────────────────────────────────────────────
# НИЗКОУРОВНЕВЫЕ ПРОВЕРКИ
# ─────────────────────────────────────────────────────────────────────────────

async def is_account_frozen(client: TelegramClient) -> bool:
    try:
        me = await client.get_me()
        return me is None
    except Exception as e:
        name = type(e).__name__
        msg = str(e).lower()
        frozen_markers = [
            "UserDeactivated", "AuthKeyUnregistered",
            "user_deactivated", "auth_key_unregistered",
            "session_revoked", "SessionRevoked",
        ]
        return any(m in name + msg for m in frozen_markers)


async def is_account_spamblocked(client: TelegramClient) -> bool:
    if not SPAMCHECK_USERNAME:
        log.warning("SPAMCHECK_USERNAME не задан в .env — проверка спамблока пропущена")
        return False

    spambot_restricted = False
    try:
        await client.send_message("SpamBot", "/start")
        await asyncio.sleep(3)
        history = await client(GetHistoryRequest(
            peer="SpamBot",
            limit=3,
            offset_date=None,
            offset_id=0,
            max_id=0,
            min_id=0,
            add_offset=0,
            hash=0,
        ))

        OK_PHRASES = [
            "свободен от", "нет ограничений", "все в порядке", "всё в порядке",
            "good standing", "no limits", "free of", "no restrictions", "not limited",
        ]
        BAN_PHRASES = [
            "ваш аккаунт ограничен", "аккаунт ограничен", "ограничения на отправку",
            "заблокирован от отправки", "не можете отправлять сообщения",
            "your account is limited", "your account has been limited",
            "you can't send messages", "you cannot send messages",
            "sending messages has been limited",
        ]

        for msg in history.messages:
            txt = (msg.message or "").lower()
            if not txt:
                continue
            if any(phrase in txt for phrase in OK_PHRASES):
                spambot_restricted = False
                break
            if any(phrase in txt for phrase in BAN_PHRASES):
                spambot_restricted = True
                break

    except FloodWaitError as e:
        log.warning("FloodWait %ds при проверке @SpamBot", e.seconds)
        await asyncio.sleep(min(e.seconds, 10))
    except Exception as e:
        log.warning("Ошибка проверки @SpamBot: %s", e)

    if spambot_restricted:
        return True

    target = SPAMCHECK_USERNAME.lstrip("@")
    try:
        await client.send_message(target, "check")
        return False
    except FloodWaitError:
        return False
    except Exception as e:
        log.info("Ошибка отправки в spamcheck (%s): %s → считаем спамблоком", target, e)
        return True


async def check_chat_access_light(
    client: TelegramClient,
    chat_id: str,
) -> tuple[bool, str]:
    """
    Лёгкая проверка доступа к чату без тестовой отправки.
    chat_id нормализуется на входе (@@username → @username).
    """
    # ── Нормализация ──────────────────────────────────────────────────────────
    chat_id = _normalize_chat_id(chat_id)

    entity = None
    try:
        if chat_id.startswith("@"):
            entity = await client.get_entity(chat_id)
        elif not chat_id.lstrip("-").isdigit():
            entity = await client.get_entity(f"@{chat_id}")
        else:
            try:
                entity = await client.get_entity(int(chat_id))
            except Exception:
                try:
                    n = int(chat_id)
                    if n > 0:
                        entity = await client.get_entity(int(f"-100{n}"))
                except Exception:
                    pass
    except Exception as e:
        err = str(e).lower()
        if "private" in err or "channel_private" in err:
            return False, "private"
        # Если не нашли — не останавливаем задачу, просто логируем
        log.warning("check_chat_access_light: не удалось найти чат '%s': %s", chat_id, e)
        return True, "resolve_failed"   # ← возвращаем True чтобы не останавливать задачу

    if entity is None:
        log.warning("check_chat_access_light: entity=None для '%s'", chat_id)
        return True, "resolve_failed"   # ← аналогично — не останавливаем

    if isinstance(entity, tl_types.Channel):
        try:
            me = await client.get_me()
            result = await client(GetParticipantRequest(
                channel=entity,
                participant=me.id,
            ))
            p = result.participant

            if isinstance(p, tl_types.ChannelParticipantBanned):
                return False, "banned"

            banned_rights = getattr(p, "banned_rights", None)
            if banned_rights and getattr(banned_rights, "send_messages", False):
                return False, "write_forbidden"

        except Exception as e:
            err = str(e).lower()
            if "not_participant" in err or "not participant" in err:
                return False, "kicked"
            if "channel_private" in err or "private" in err:
                return False, "private"
            if "banned" in err:
                return False, "banned"
            log.debug("GetParticipant %s: %s (игнорируем)", chat_id, e)

    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ОПЕРАЦИИ С БД
# ─────────────────────────────────────────────────────────────────────────────

async def stop_account_tasks(db: AsyncSession, account: Account) -> int:
    result = await db.execute(
        select(TaskAccount).where(TaskAccount.account_id == account.id)
    )
    task_accounts = result.scalars().all()
    task_ids = {ta.task_id for ta in task_accounts}
    stopped = 0

    for task_id in task_ids:
        result = await db.execute(
            select(Task).where(Task.id == task_id, Task.is_active == True)
        )
        task = result.scalar_one_or_none()
        if task:
            task.is_active = False
            stopped += 1

    await db.commit()
    return stopped


async def redistribute_system_chats(db: AsyncSession, blocked_account: Account) -> int:
    result = await db.execute(
        select(TaskAccount).where(TaskAccount.account_id == blocked_account.id)
    )
    old_task_accounts = result.scalars().all()

    if not old_task_accounts:
        return 0

    result = await db.execute(
        select(Account).where(
            Account.is_system == True,
            Account.is_active == True,
            Account.is_banned == False,
            Account.id != blocked_account.id,
            Account.status == "ok",
        ).order_by(Account.chats_count.asc())
    )
    available = list(result.scalars().all())

    if not available:
        log.warning("Нет доступных системных аккаунтов для перераспределения от %s", blocked_account.phone)
        return 0

    used_ids: set[int] = set()

    for ta in old_task_accounts:
        try:
            chat_ids = json.loads(ta.chat_ids or "[]")
        except Exception:
            chat_ids = []

        if not chat_ids:
            await db.delete(ta)
            continue

        for i, chat_id in enumerate(chat_ids):
            available.sort(key=lambda a: a.chats_count)
            acc = available[i % len(available)]

            result = await db.execute(
                select(TaskAccount).where(
                    TaskAccount.task_id == ta.task_id,
                    TaskAccount.account_id == acc.id,
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing_ids = json.loads(existing.chat_ids or "[]")
                if str(chat_id) not in [str(x) for x in existing_ids]:
                    existing_ids.append(chat_id)
                    existing.chat_ids = json.dumps(existing_ids)
            else:
                db.add(TaskAccount(
                    task_id=ta.task_id,
                    account_id=acc.id,
                    chat_ids=json.dumps([str(chat_id)]),
                ))

            acc.chats_count += 1
            used_ids.add(acc.id)

        blocked_account.chats_count = max(0, blocked_account.chats_count - len(chat_ids))
        await db.delete(ta)

    await db.commit()
    return len(used_ids)


async def find_replacement_system_account(
    db: AsyncSession,
    excluded_account: Account,
    chat_id: str,
) -> Optional[Account]:
    from services.account_service import can_write_to_chat

    chat_id = _normalize_chat_id(chat_id)

    result = await db.execute(
        select(Account).where(
            Account.is_system == True,
            Account.is_active == True,
            Account.is_banned == False,
            Account.id != excluded_account.id,
            Account.status == "ok",
        ).order_by(Account.chats_count.asc())
    )
    candidates = result.scalars().all()

    for acc in candidates:
        client = make_client(acc)
        try:
            await client.connect()
            await asyncio.sleep(1)
            can_write, _ = await can_write_to_chat(client, chat_id)
            if can_write:
                return acc
        except Exception as e:
            log.warning("Ошибка проверки замены %s→%s: %s", acc.phone, chat_id, e)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    return None


async def transfer_chat_to_account(
    db: AsyncSession,
    task: Task,
    old_account: Account,
    new_account: Account,
    chat_id: str,
):
    chat_id = _normalize_chat_id(chat_id)

    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task.id,
            TaskAccount.account_id == old_account.id,
        )
    )
    old_ta = result.scalar_one_or_none()
    if old_ta:
        old_ids = json.loads(old_ta.chat_ids or "[]")
        old_ids = [x for x in old_ids if str(x) != str(chat_id)]
        old_ta.chat_ids = json.dumps(old_ids)
        old_account.chats_count = max(0, old_account.chats_count - 1)
        if not old_ids:
            await db.delete(old_ta)

    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task.id,
            TaskAccount.account_id == new_account.id,
        )
    )
    new_ta = result.scalar_one_or_none()
    if new_ta:
        new_ids = json.loads(new_ta.chat_ids or "[]")
        if str(chat_id) not in [str(x) for x in new_ids]:
            new_ids.append(str(chat_id))
        new_ta.chat_ids = json.dumps(new_ids)
    else:
        db.add(TaskAccount(
            task_id=task.id,
            account_id=new_account.id,
            chat_ids=json.dumps([str(chat_id)]),
        ))

    new_account.chats_count += 1


async def remove_chat_from_task(
    db: AsyncSession,
    task: Task,
    account: Account,
    chat_id: str,
):
    chat_id = _normalize_chat_id(chat_id)

    result = await db.execute(
        select(TaskAccount).where(
            TaskAccount.task_id == task.id,
            TaskAccount.account_id == account.id,
        )
    )
    ta = result.scalar_one_or_none()
    if ta:
        ids = json.loads(ta.chat_ids or "[]")
        ids = [x for x in ids if str(x) != str(chat_id)]
        ta.chat_ids = json.dumps(ids)
        account.chats_count = max(0, account.chats_count - 1)
        if not ids:
            await db.delete(ta)

    result = await db.execute(
        select(TaskChat).where(
            TaskChat.task_id == task.id,
            TaskChat.chat_id == str(chat_id),
        )
    )
    tc = result.scalar_one_or_none()
    if tc:
        await db.delete(tc)


# ─────────────────────────────────────────────────────────────────────────────
# ОБРАБОТЧИКИ ОГРАНИЧЕНИЙ
# ─────────────────────────────────────────────────────────────────────────────

async def handle_frozen_account(db: AsyncSession, account: Account, bot: Bot):
    if account.status == "frozen":
        return

    log.warning("❄️  Аккаунт %s (id=%d) заморожен", account.phone, account.id)
    account.status = "frozen"
    account.is_banned = True
    account.is_active = False
    await db.commit()

    stopped = await stop_account_tasks(db, account)

    if account.is_system:
        try:
            await bot.send_message(
                OWNER_ID,
                f"❄️ *Системный аккаунт заморожен*\n\n"
                f"📱 `{account.phone}`\n"
                f"ID: `{account.id}`\n\n"
                f"Telegram заблокировал аккаунт (deactivated).\n"
                f"Остановлено задач: *{stopped}*.\n\n"
                f"Используйте /admin для управления аккаунтами.",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error("Ошибка уведомления о заморозке системного: %s", e)
    else:
        if account.owner_id:
            try:
                await bot.send_message(
                    account.owner_id,
                    f"❄️ *Ваш аккаунт заморожен Telegram*\n\n"
                    f"📱 `{account.phone}`\n\n"
                    f"Telegram деактивировал этот аккаунт.\n"
                    f"Все рассылки остановлены ({stopped} задач).\n\n"
                    f"Для восстановления обратитесь в поддержку Telegram.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass


async def handle_spamblocked_account(db: AsyncSession, account: Account, bot: Bot):
    if account.status == "spamblocked":
        return

    log.warning("🚫  Аккаунт %s (id=%d) в спамблоке", account.phone, account.id)
    account.status = "spamblocked"
    await db.commit()

    if account.is_system:
        redirected = await redistribute_system_chats(db, account)
        try:
            await bot.send_message(
                OWNER_ID,
                f"🚫 *Системный аккаунт в спамблоке*\n\n"
                f"📱 `{account.phone}`\n"
                f"ID: `{account.id}`\n\n"
                f"Чаты перераспределены равномерно на *{redirected}* аккаунт(ов).\n"
                f"Новые задачи на этот аккаунт не назначаются.\n\n"
                f"Управление аккаунтами: /admin",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error("Ошибка уведомления о спамблоке системного: %s", e)
    else:
        stopped = await stop_account_tasks(db, account)
        if account.owner_id:
            try:
                await bot.send_message(
                    account.owner_id,
                    f"🚫 *Ваш аккаунт получил спамблок*\n\n"
                    f"📱 `{account.phone}`\n\n"
                    f"Telegram ограничил возможность отправки сообщений с этого аккаунта.\n"
                    f"Остановлено задач: *{stopped}*\n\n"
                    f"💡 Попробуйте снять ограничение через @SpamBot\n\n"
                    f"Управление аккаунтами: /accounts",
                    parse_mode="Markdown",
                )
            except Exception:
                pass


async def handle_chat_restriction(
    db: AsyncSession,
    account: Account,
    task_id: int,
    chat_id: str,
    reason: str,
    bot: Bot,
):
    # Нормализуем сразу на входе
    chat_id = _normalize_chat_id(chat_id)

    # resolve_failed означает что чат просто не нашли — не останавливаем задачу
    if reason == "resolve_failed":
        log.warning(
            "handle_chat_restriction: reason=resolve_failed для '%s' acc=%s — пропускаем",
            chat_id, account.phone,
        )
        return

    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return

    result = await db.execute(
        select(TaskChat).where(
            TaskChat.task_id == task_id,
            TaskChat.chat_id == str(chat_id),
        )
    )
    tc = result.scalar_one_or_none()
    chat_title = (tc.chat_title or str(chat_id)) if tc else str(chat_id)

    reason_labels = {
        "banned":            "аккаунт заблокирован администратором чата",
        "write_forbidden":   "нет прав на отправку сообщений",
        "kicked":            "аккаунт исключён из чата",
        "private":           "чат стал приватным",
        "not_found":         "чат не найден",
        "too_many_channels": "аккаунт состоит в слишком многих чатах",
    }
    reason_text = reason_labels.get(reason, reason)

    if account.is_system:
        new_acc = await find_replacement_system_account(db, account, chat_id)

        if new_acc:
            await transfer_chat_to_account(db, task, account, new_acc, chat_id)
            await db.commit()
            log.info("Чат %s перенесён с %s на %s (задача %d)", chat_id, account.phone, new_acc.phone, task_id)
            try:
                await bot.send_message(
                    task.user_id,
                    f"🔄 *Автоматическая замена аккаунта в рассылке*\n\n"
                    f"Задача: *{task.name}*\n"
                    f"Чат: {chat_title}\n\n"
                    f"Причина: {reason_text}\n\n"
                    f"❌ Старый: `{account.phone}`\n"
                    f"✅ Новый: `{new_acc.phone}`\n\n"
                    f"Рассылка продолжается автоматически.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        else:
            await remove_chat_from_task(db, task, account, chat_id)
            await db.commit()
            try:
                await bot.send_message(
                    task.user_id,
                    f"⚠️ *Чат недоступен для рассылки*\n\n"
                    f"Задача: *{task.name}*\n"
                    f"Чат: {chat_title}\n"
                    f"Причина: {reason_text}\n\n"
                    f"Ни один системный аккаунт не может писать в этот чат.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    else:
        task.is_active = False
        await db.commit()

        if account.owner_id:
            import html
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            msg_preview = (task.message or "").strip()
            if len(msg_preview) > 800:
                msg_preview = msg_preview[:800] + "…"

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Перенести рассылку на другой аккаунт",
                    callback_data=f"tasks:transfer_start:{task.id}:{chat_id}",
                )],
                [InlineKeyboardButton(text="◀️ Меню", callback_data="menu:new")],
            ])

            try:
                await bot.send_message(
                    account.owner_id,
                    f"⚠️ <b>Ограничение в чате — рассылка остановлена</b>\n\n"
                    f"Аккаунт: <code>{account.phone}</code>\n"
                    f"Чат: {html.escape(chat_title)}\n"
                    f"Задача: <b>{html.escape(task.name)}</b>\n\n"
                    f"Причина: {html.escape(reason_text)}\n\n"
                    f"<b>Текст рассылки:</b>\n"
                    f"<blockquote expandable>{html.escape(msg_preview)}</blockquote>\n\n"
                    f"Нажмите кнопку ниже чтобы продолжить рассылку через другой аккаунт.",
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning("Ошибка отправки уведомления об ограничении: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# ПРОВЕРКА ПРИ ОШИБКЕ ОТПРАВКИ
# ─────────────────────────────────────────────────────────────────────────────

async def check_account_on_send_error(
    account_id: int,
    task_id: int,
    chat_id: str,
    send_error: Exception,
):
    # Нормализуем сразу
    chat_id = _normalize_chat_id(chat_id)

    now = time.monotonic()
    last = _last_spamcheck.get(account_id, 0)
    if now - last < _SPAMCHECK_COOLDOWN:
        log.debug("Кулдаун проверки аккаунта %d — пропускаем", account_id)
        return
    _last_spamcheck[account_id] = now

    from database import SessionLocal

    bot = _get_bot()
    try:
        async with SessionLocal() as db:
            result = await db.execute(select(Account).where(Account.id == account_id))
            account = result.scalar_one_or_none()
            if not account or account.status != "ok":
                return

            client = make_client(account)
            try:
                await client.connect()
                await asyncio.sleep(1)

                if not await client.is_user_authorized():
                    await handle_frozen_account(db, account, bot)
                    return

                if await is_account_frozen(client):
                    await handle_frozen_account(db, account, bot)
                    return

                if await is_account_spamblocked(client):
                    await handle_spamblocked_account(db, account, bot)
                    return

                err_name = type(send_error).__name__.lower()
                err_str = str(send_error).lower()

                if isinstance(send_error, UserBannedInChannelError) or "banned" in err_name + err_str:
                    reason = "banned"
                elif isinstance(send_error, ChatWriteForbiddenError) or "forbidden" in err_name + err_str:
                    reason = "write_forbidden"
                elif "participant" in err_str or "kicked" in err_str:
                    reason = "kicked"
                else:
                    reason = str(send_error)[:60]

                await handle_chat_restriction(db, account, task_id, chat_id, reason, bot)

            except Exception as e:
                log.error("Ошибка в check_account_on_send_error (acc=%d): %s", account_id, e)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
    finally:
        await bot.session.close()


# ─────────────────────────────────────────────────────────────────────────────
# ПЕРИОДИЧЕСКАЯ ПРОВЕРКА ВСЕХ АККАУНТОВ (каждые 30 минут)
# ─────────────────────────────────────────────────────────────────────────────

async def run_full_restriction_check():
    from database import SessionLocal

    log.info("▶️  Запуск периодической проверки ограничений аккаунтов")
    bot = _get_bot()

    try:
        async with SessionLocal() as db:
            result = await db.execute(
                select(Account).where(
                    Account.is_active == True,
                    Account.is_banned == False,
                    Account.status == "ok",
                )
            )
            accounts = result.scalars().all()
            log.info("Проверка ограничений: %d аккаунтов", len(accounts))

            for account in accounts:
                await _check_single_account(db, account, bot)
                await asyncio.sleep(3)

    except Exception as e:
        log.error("Критическая ошибка проверки ограничений: %s", e)
    finally:
        await bot.session.close()
        log.info("✅  Проверка ограничений завершена")


async def _check_single_account(db: AsyncSession, account: Account, bot: Bot):
    client = make_client(account)
    try:
        await client.connect()
        await asyncio.sleep(1)

        if not await client.is_user_authorized():
            await handle_frozen_account(db, account, bot)
            return

        if await is_account_frozen(client):
            await handle_frozen_account(db, account, bot)
            return

        _last_spamcheck[account.id] = time.monotonic()

        if await is_account_spamblocked(client):
            await handle_spamblocked_account(db, account, bot)
            return

        await _check_account_chat_access(db, account, client, bot)

    except Exception as e:
        log.error("Ошибка проверки аккаунта %s (id=%d): %s", account.phone, account.id, e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _check_account_chat_access(
    db: AsyncSession,
    account: Account,
    client: TelegramClient,
    bot: Bot,
):
    result = await db.execute(
        select(TaskAccount).where(TaskAccount.account_id == account.id)
    )
    task_accounts = result.scalars().all()

    for ta in task_accounts:
        result = await db.execute(
            select(Task).where(Task.id == ta.task_id, Task.is_active == True)
        )
        task = result.scalar_one_or_none()
        if not task:
            continue

        try:
            raw_chat_ids = json.loads(ta.chat_ids or "[]")
        except Exception:
            continue

        # Нормализуем все chat_id из БД перед проверкой
        chat_ids = [_normalize_chat_id(str(cid)) for cid in raw_chat_ids]

        for chat_id in list(chat_ids):
            can_write, reason = await check_chat_access_light(client, chat_id)

            if not can_write:
                log.warning(
                    "Аккаунт %s не может писать в %s: %s",
                    account.phone, chat_id, reason,
                )
                await handle_chat_restriction(
                    db, account, ta.task_id, chat_id, reason, bot
                )

            await asyncio.sleep(1)
