"""
services/account_service.py — управление Telegram-аккаунтами (Telethon).

ИЗМЕНЕНИЯ:
  - can_write_to_chat: обработка discussion group (вступаем в родительский канал)
  - check_and_join_chats: для папок — массовое вступление + лёгкая проверка прав
    (без тестовой отправки). 100 чатов ~30 сек вместо 30+ минут.
  - Тестовая отправка только для чатов введённых вручную (folder_slug is None).
  - get_accounts: добавлен параметр only_working для admin-панели.
"""
import asyncio
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    ChannelsTooMuchError,
    FloodWaitError,
    InviteRequestSentError,
    PeerIdInvalidError,
    SlowModeWaitError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Account

log = logging.getLogger(__name__)


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def get_accounts(
    db: AsyncSession,
    owner_id: int | None = None,
    only_working: bool = True,
) -> list[Account]:
    """
    owner_id=None  → системные аккаунты (is_system=True).
    owner_id=X     → аккаунты пользователя X.
    only_working   → фильтр is_active/is_banned (по умолчанию True).
    """
    q = select(Account)
    if only_working:
        q = q.where(Account.is_active == True, Account.is_banned == False)
    if owner_id is not None:
        q = q.where(Account.owner_id == owner_id)
    else:
        q = q.where(Account.is_system == True)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_account_by_id(db: AsyncSession, account_id: int) -> Account | None:
    result = await db.execute(select(Account).where(Account.id == account_id))
    return result.scalar_one_or_none()


async def create_account(
    db: AsyncSession,
    api_id: int,
    api_hash: str,
    phone: str,
    session_string: str,
    owner_id: int | None = None,
    is_system: bool = False,
) -> Account:
    acc = Account(
        owner_id=owner_id,
        phone=phone,
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        is_system=is_system,
    )
    db.add(acc)
    await db.commit()
    await db.refresh(acc)
    log.info("Аккаунт %s добавлен (id=%d)", phone, acc.id)
    return acc


async def delete_account(db: AsyncSession, account_id: int) -> bool:
    acc = await get_account_by_id(db, account_id)
    if not acc:
        return False
    await db.delete(acc)
    await db.commit()
    return True


async def set_banned(db: AsyncSession, account_id: int, banned: bool):
    acc = await get_account_by_id(db, account_id)
    if acc:
        acc.is_banned = banned
        await db.commit()


async def update_chats_count(db: AsyncSession, account_id: int, count: int):
    acc = await get_account_by_id(db, account_id)
    if acc:
        acc.chats_count = count
        await db.commit()


# ── Telethon: auth ────────────────────────────────────────────────────────────

def make_client(acc: Account) -> TelegramClient:
    return TelegramClient(
        StringSession(acc.session_string),
        int(acc.api_id),
        acc.api_hash,
    )


async def send_code(api_id: int, api_hash: str, phone: str) -> tuple[TelegramClient, str]:
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    await asyncio.sleep(1)
    log.info("Отправка кода на %s", phone)
    sent = await client.send_code_request(phone)
    return client, sent.phone_code_hash


async def sign_in_code(client, phone, code, phone_code_hash) -> str | None:
    await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    return StringSession.save(client.session)


async def sign_in_2fa(client, password) -> str:
    await client.sign_in(password=password)
    return StringSession.save(client.session)


async def get_me_name(client: TelegramClient) -> str:
    me = await client.get_me()
    return me.first_name or me.username or str(me.id)


# ── Папки ─────────────────────────────────────────────────────────────────────

async def get_chats_from_folder(client: TelegramClient, folder_link: str) -> list[dict]:
    """
    Получить чаты из папки https://t.me/addlist/XXXX.

    Возвращает список dict:
      id          — строка "-100XXXXXXXXXX" или "-XXXXXXXXXX"
      title       — название чата
      username    — username без @ (или None)
      access_hash — (или None)
      folder_slug — slug папки для массового вступления
    """
    slug = folder_link.rstrip("/").split("/")[-1]
    chats: list[dict] = []
    try:
        from telethon.tl.functions.chatlists import CheckChatlistInviteRequest
        result = await client(CheckChatlistInviteRequest(slug=slug))

        for peer in result.chats[:500]:
            peer_id = getattr(peer, "id", None)
            if peer_id is None:
                continue

            title = (
                getattr(peer, "title", None)
                or getattr(peer, "first_name", None)
                or str(peer_id)
            )
            username    = getattr(peer, "username", None)
            access_hash = getattr(peer, "access_hash", None)

            # Каналы/супергруппы имеют access_hash → prefix -100
            str_id = f"-100{peer_id}" if access_hash is not None else str(-abs(peer_id))

            chats.append({
                "id":          str_id,
                "title":       title,
                "username":    username,
                "access_hash": access_hash,
                "folder_slug": slug,
            })

        log.info("Получено %d чатов из папки %s", len(chats), folder_link)
    except Exception as e:
        log.warning("Ошибка получения папки %s: %s", folder_link, e)

    return chats


# ── Вспомогательные функции ───────────────────────────────────────────────────

async def _resolve_entity(client: TelegramClient, chat_id: str):
    """
    Резолв entity по строковому ID.
    Пробует: числовой int → -100 prefix → строку как есть.
    """
    s = str(chat_id).strip()

    if s.startswith("@"):
        return await client.get_entity(s)

    if s.lstrip("-").isdigit():
        numeric = int(s)
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

    return await client.get_entity(s)


async def _join_single(client: TelegramClient, entity) -> tuple[bool, str]:
    """Вступить в один канал/супергруппу."""
    from telethon.tl.functions.channels import JoinChannelRequest
    try:
        await client(JoinChannelRequest(entity))
        await asyncio.sleep(1)
        return True, "ok"
    except UserAlreadyParticipantError:
        return True, "ok"
    except ChannelsTooMuchError:
        return False, "too_many_channels"
    except InviteRequestSentError:
        return True, "join_pending"
    except Exception as e:
        return False, str(e)[:80]


async def _join_folder_bulk(client: TelegramClient, slug: str) -> bool:
    """Массово вступить во все чаты папки одним запросом."""
    try:
        from telethon.tl.functions.chatlists import JoinChatlistInviteRequest
        await client(JoinChatlistInviteRequest(slug=slug, peers=[]))
        log.info("JoinChatlistInviteRequest для папки %s — успешно", slug)
        return True
    except Exception as e:
        log.warning("JoinChatlistInviteRequest %s: %s", slug, e)
        return False


async def _check_write_light(client: TelegramClient, chat_id: str) -> tuple[bool | None, str]:
    """
    Лёгкая проверка прав без отправки сообщений.
    Использует GetParticipantRequest.

    Возвращает:
      (True, "ok")            — можно писать
      (False, reason)         — нельзя писать
      (None, "not_participant") — ещё не вступили (bulk join не добавил)
      (None, "ok")            — GetParticipant не применим (обычная группа) → считаем OK
    """
    from telethon.tl import types as tl_types
    from telethon.tl.functions.channels import GetParticipantRequest

    entity = None
    try:
        entity = await _resolve_entity(client, chat_id)
    except Exception as e:
        err = str(e).lower()
        if "private" in err or "channel_private" in err:
            return False, "private"
        return False, str(e)[:80]

    if entity is None:
        return False, "not_found"

    if not isinstance(entity, tl_types.Channel):
        # Обычная группа — GetParticipant не нужен, считаем OK
        return None, "ok"

    try:
        me = await client.get_me()
        result = await client(GetParticipantRequest(channel=entity, participant=me.id))
        p = result.participant

        if isinstance(p, tl_types.ChannelParticipantBanned):
            return False, "banned"

        banned_rights = getattr(p, "banned_rights", None)
        if banned_rights and getattr(banned_rights, "send_messages", False):
            return False, "write_forbidden"

        return True, "ok"

    except Exception as e:
        err = str(e).lower()
        if "not_participant" in err or "not participant" in err:
            return None, "not_participant"
        if "banned" in err:
            return False, "banned"
        if "private" in err or "channel_private" in err:
            return False, "private"
        # Другие ошибки GetParticipant — не критично, считаем OK
        log.debug("GetParticipant %s: %s", chat_id, e)
        return None, "ok"


async def _test_send(
    client: TelegramClient,
    entity,
    chat_id: str,
    _retry: bool = False,
) -> tuple[bool, str]:
    """
    Тестовая отправка "." + немедленное удаление.
    При ошибке discussion group — вступает в родительский канал и повторяет (один раз).
    """
    from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
    from telethon.tl import types as tl_types

    try:
        msg = await client.send_message(entity, ".")
        try:
            await client.delete_messages(entity, [msg.id])
        except Exception:
            pass
        return True, "ok"

    except FloodWaitError as e:
        log.warning("FloodWait %ds при проверке %s", e.seconds, chat_id)
        return True, "ok"   # FloodWait ≠ нельзя писать

    except SlowModeWaitError:
        return True, "ok"

    except UserBannedInChannelError:
        return False, "banned"

    except ChatWriteForbiddenError:
        return False, "write_forbidden"

    except Exception as e:
        err_str  = str(e)
        err_low  = err_str.lower()

        # Discussion group — нужно вступить в родительский канал
        if "discussion" in err_low and not _retry:
            log.info("Discussion group %s — ищу родительский канал", chat_id)
            try:
                if isinstance(entity, tl_types.Channel):
                    full      = await client(GetFullChannelRequest(entity))
                    linked_id = getattr(full.full_chat, "linked_chat_id", None)
                    if linked_id:
                        parent = await client.get_entity(linked_id)
                        await client(JoinChannelRequest(parent))
                        await asyncio.sleep(2)
                        log.info("Вступил в родительский канал %s для %s", linked_id, chat_id)
                        return await _test_send(client, entity, chat_id, _retry=True)
            except Exception as je:
                log.warning("Не удалось вступить в родитель для %s: %s", chat_id, je)
            return False, "discussion_no_parent"

        if "banned" in err_low:
            return False, "banned"
        if "forbidden" in err_low or "not allowed" in err_low:
            return False, "write_forbidden"
        if "private" in err_low or "channel_private" in err_low:
            return False, "private"

        log.warning("Неизвестная ошибка при проверке %s: %s", chat_id, err_str)
        return False, err_str[:80]


# ── Публичные функции проверки ────────────────────────────────────────────────

async def can_write_to_chat(
    client: TelegramClient,
    chat_id: str,
    username: str | None = None,
    access_hash: int | None = None,
) -> tuple[bool, str]:
    """
    Полная проверка с тестовой отправкой. Только для ручного ввода чатов.
    Обрабатывает discussion group автоматически.
    """
    from telethon.tl.functions.messages import ImportChatInviteRequest

    # Резолв
    entity = None
    if username:
        try:
            entity = await client.get_entity(f"@{username}")
        except Exception:
            pass
    if entity is None:
        try:
            entity = await _resolve_entity(client, chat_id)
        except ChannelPrivateError:
            return False, "private"
        except (PeerIdInvalidError, ValueError):
            return False, "invalid_id"
        except Exception as e:
            if "private" in str(e).lower():
                return False, "private"

    if entity is None:
        return False, "not_found"

    # Вступление
    if chat_id.startswith("https://t.me/+") or chat_id.startswith("t.me/+"):
        invite_hash = chat_id.rstrip("/").split("+")[-1]
        try:
            await client(ImportChatInviteRequest(invite_hash))
            await asyncio.sleep(1)
            entity = await client.get_entity(entity.id)
        except UserAlreadyParticipantError:
            pass
        except Exception as e:
            err = str(e).lower()
            if "expired" in err:
                return False, "invite_expired"
            if "too_many" in err:
                return False, "too_many_channels"
    elif getattr(entity, "username", None):
        ok, reason = await _join_single(client, entity)
        if not ok:
            return False, reason
        try:
            entity = await client.get_entity(entity.id)
        except Exception:
            pass

    return await _test_send(client, entity, chat_id)


async def check_and_join_chats(
    client: TelegramClient,
    chats: list[dict],
) -> list[dict]:
    """
    Проверить доступ к списку чатов.

    Папки (folder_slug задан):
      • Одним запросом JoinChatlistInviteRequest вступаем во все чаты папки.
      • Лёгкая проверка прав через GetParticipantRequest (без отправки сообщений).
      • Скорость: ~30 сек на 100 чатов.

    Ручной ввод (folder_slug is None):
      • Полная проверка с тестовой отправкой "." + удаление.

    Возвращает список:
      {"id", "title", "username", "can_write", "reason", "link"}
    """
    # Массовое вступление — один запрос на уникальную папку
    done_slugs: set[str] = set()
    for chat in chats:
        slug = chat.get("folder_slug")
        if slug and slug not in done_slugs:
            await _join_folder_bulk(client, slug)
            done_slugs.add(slug)

    if done_slugs:
        # Даём TG время обновить членство
        await asyncio.sleep(3)

    results: list[dict] = []

    for chat in chats:
        chat_id  = str(chat["id"])
        title    = chat.get("title", chat_id)
        username = chat.get("username")
        slug     = chat.get("folder_slug")

        link = f"https://t.me/{username}" if username else None

        if slug:
            # ── Быстрый путь (папка) ──────────────────────────────────────────
            can_write, reason = await _check_write_light(client, chat_id)

            if can_write is None and reason == "not_participant":
                # bulk join не добавил — пробуем поштучно если есть username
                if username:
                    try:
                        entity = await client.get_entity(f"@{username}")
                        ok, r  = await _join_single(client, entity)
                        if ok:
                            await asyncio.sleep(1)
                            can_write, reason = await _check_write_light(client, chat_id)
                            if can_write is None:
                                can_write, reason = True, "ok"
                        else:
                            can_write, reason = False, r
                    except Exception:
                        can_write, reason = False, "not_found"
                else:
                    # Приватный чат без username — недоступен
                    can_write, reason = False, "private"

            elif can_write is None:
                # GetParticipant не применим (обычная группа) — считаем OK
                can_write, reason = True, "ok"

        else:
            # ── Полная проверка (ручной ввод) ─────────────────────────────────
            can_write, reason = await can_write_to_chat(
                client,
                chat_id,
                username=username,
                access_hash=chat.get("access_hash"),
            )
            await asyncio.sleep(1)

        results.append({
            "id":        chat_id,
            "title":     title,
            "username":  username,
            "can_write": bool(can_write),
            "reason":    reason,
            "link":      link,
        })

    return results
