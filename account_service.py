"""
services/account_service.py — управление Telegram-аккаунтами (Telethon).
Добавление, авторизация, получение списка, загрузка/выгрузка.
"""
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    ChannelPrivateError,
    InviteHashExpiredError,
    UserBannedInChannelError,
    ChatWriteForbiddenError,
    SlowModeWaitError,
    FloodWaitError,
    UserAlreadyParticipantError,
    InviteRequestSentError,
    ChannelsTooMuchError,
    PeerIdInvalidError,
)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Account

log = logging.getLogger(__name__)


async def get_accounts(db: AsyncSession, owner_id: int | None = None) -> list[Account]:
    """
    Получить аккаунты.
    owner_id=None → все системные аккаунты.
    owner_id=X    → аккаунты пользователя X.
    """
    q = select(Account).where(Account.is_active == True, Account.is_banned == False)
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
    """Сохранить новый аккаунт после успешной авторизации."""
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


# ── Telethon helpers ──────────────────────────────────────────────────────────

def make_client(acc: Account) -> TelegramClient:
    """Создать Telethon-клиент из сохранённого аккаунта."""
    return TelegramClient(
        StringSession(acc.session_string),
        int(acc.api_id),
        acc.api_hash,
    )


async def send_code(api_id: int, api_hash: str, phone: str) -> tuple[TelegramClient, str]:
    """
    Начать вход: отправить код на телефон.
    Возвращает (client, phone_code_hash) — клиент надо сохранить в user_data.
    """
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    sent = await client.send_code_request(phone)
    return client, sent.phone_code_hash


async def sign_in_code(
    client: TelegramClient,
    phone: str,
    code: str,
    phone_code_hash: str,
) -> str | None:
    """
    Войти по коду. Если нужен 2FA — бросает SessionPasswordNeededError.
    Возвращает session_string при успехе.
    """
    await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    return StringSession.save(client.session)


async def sign_in_2fa(client: TelegramClient, password: str) -> str:
    """Войти по паролю 2FA. Возвращает session_string."""
    await client.sign_in(password=password)
    return StringSession.save(client.session)


async def get_me_name(client: TelegramClient) -> str:
    """Получить имя авторизованного пользователя."""
    me = await client.get_me()
    return me.first_name or me.username or str(me.id)


async def get_chats_from_folder(client: TelegramClient, folder_link: str) -> list[dict]:
    """
    Получить список чатов из папки Telegram по ссылке.
    Папка: https://t.me/addlist/XXXX
    """
    # Извлекаем slug из ссылки
    slug = folder_link.rstrip("/").split("/")[-1]
    chats = []
    try:
        result = await client(
            __import__("telethon.tl.functions.chatlists", fromlist=["CheckChatlistInviteRequest"])
            .CheckChatlistInviteRequest(slug=slug)
        )
        for peer in result.chats[:100]:  # не больше 100 чатов
            chats.append({
                "id": str(peer.id),
                "title": getattr(peer, "title", getattr(peer, "first_name", str(peer.id))),
            })
    except Exception as e:
        log.warning("Ошибка получения папки %s: %s", folder_link, e)
    return chats


async def can_write_to_chat(client: TelegramClient, chat_id: str) -> tuple[bool, str]:
    """
    Проверяет, может ли аккаунт писать в чат (без реальной отправки).
    Если аккаунт не участник публичного чата — пробует вступить.
    Возвращает (can_write: bool, reason: str).
    """
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest

    # Шаг 1: разрезолвить entity
    entity = None
    try:
        if not chat_id.lstrip("-").isdigit():
            entity = await client.get_entity(f"@{chat_id}")
        else:
            try:
                entity = await client.get_entity(int(chat_id))
            except (ValueError, PeerIdInvalidError):
                n = int(chat_id)
                if n > 0:
                    entity = await client.get_entity(int(f"-100{n}"))
    except ChannelPrivateError:
        return False, "private"
    except (PeerIdInvalidError, ValueError):
        return False, "invalid_id"
    except Exception as e:
        return False, str(e)

    if entity is None:
        return False, "not_found"

    # Шаг 2: если это invite-ссылка (https://t.me/+hash) — пробуем вступить через неё
    if chat_id.startswith("https://t.me/+"):
        invite_hash = chat_id.rstrip("/").split("+")[-1]
        try:
            await client(ImportChatInviteRequest(invite_hash))
        except UserAlreadyParticipantError:
            pass
        except InviteHashExpiredError:
            return False, "invite_expired"
        except ChannelsTooMuchError:
            return False, "too_many_channels"
        except InviteRequestSentError:
            return False, "join_pending"
        except Exception as e:
            return False, str(e)

    # Шаг 3: для каналов/супергрупп с username — пробуем вступить если нет членства
    else:
        has_username = bool(getattr(entity, "username", None))
        if has_username:
            try:
                await client(JoinChannelRequest(entity))
            except UserAlreadyParticipantError:
                pass
            except ChannelsTooMuchError:
                return False, "too_many_channels"
            except InviteRequestSentError:
                return False, "join_pending"
            except Exception as join_err:
                log.warning("Не удалось вступить в чат %s: %s", chat_id, join_err)

    # Шаг 4: broadcast-канал — обычные участники не могут писать
    if getattr(entity, "broadcast", False):
        return False, "broadcast_channel"

    # Шаг 5: проверить права через get_permissions
    try:
        perms = await client.get_permissions(entity)
        if perms.send_messages:
            return True, "ok"
        return False, "no_send_permission"
    except UserBannedInChannelError:
        return False, "banned"
    except ChatWriteForbiddenError:
        return False, "write_forbidden"
    except SlowModeWaitError:
        return True, "ok"  # slow mode — писать можно, просто с ограничением
    except FloodWaitError:
        return True, "ok"  # flood wait — временное ограничение, не бан
    except Exception as e:
        return False, str(e)


async def check_and_join_chats(
    client: TelegramClient,
    chats: list[dict],
) -> list[dict]:
    """
    Проверить доступ аккаунта ко всем чатам и попытаться вступить при необходимости.

    Принимает список {"id": str, "title": str}.
    Возвращает список {"id", "title", "can_write": bool, "reason": str, "link": str|None}.
    """
    results = []
    for chat in chats:
        chat_id = str(chat["id"])
        title = chat.get("title", chat_id)

        # Формируем ссылку для отображения пользователю
        if chat_id.lstrip("-").isdigit():
            link = None
        elif chat_id.startswith("https://t.me/"):
            link = chat_id
        else:
            link = f"https://t.me/{chat_id}"

        can_write, reason = await can_write_to_chat(client, chat_id)
        results.append({
            "id": chat_id,
            "title": title,
            "can_write": can_write,
            "reason": reason,
            "link": link,
        })
    return results
