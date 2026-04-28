"""
services/account_service.py — управление Telegram-аккаунтами (Telethon).
Добавление, авторизация, получение списка, загрузка/выгрузка.
"""
import logging
import asyncio
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
    
    ФИКСИРОВАНО: добавлена задержка после подключения и логирование.
    """
    client = TelegramClient(StringSession(), api_id, api_hash)
    log.info("Подключаюсь для отправки кода на %s...", phone)
    
    try:
        await client.connect()
        log.info("Подключение успешно")
        
        # Задержка после подключения — даёт время на синхронизацию с серверами
        await asyncio.sleep(1)
        
        log.info("Отправляю код на %s...", phone)
        sent = await client.send_code_request(phone)
        
        log.info("✅ Код успешно отправлен на %s (hash=%s)", phone, sent.phone_code_hash)
        return client, sent.phone_code_hash
        
    except Exception as e:
        log.error("❌ Ошибка отправки кода на %s: %s", phone, e)
        try:
            await client.disconnect()
        except Exception:
            pass
        raise


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
    log.info("Пытаюсь войти по коду для %s...", phone)
    await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    session_str = StringSession.save(client.session)
    log.info("✅ Успешный вход для %s", phone)
    return session_str


async def sign_in_2fa(client: TelegramClient, password: str) -> str:
    """Войти по паролю 2FA. Возвращает session_string."""
    log.info("Пытаюсь войти с паролем 2FA...")
    await client.sign_in(password=password)
    session_str = StringSession.save(client.session)
    log.info("✅ Успешный вход через 2FA")
    return session_str


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
    Проверяет может ли аккаунт писать в чат.
    Метод: реальная отправка сообщения "тест" с немедленным удалением.
    Это единственный 100% надёжный способ проверки.
    """
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest, DeleteMessagesRequest
    from telethon.tl.functions.channels import DeleteMessagesRequest as ChannelDeleteMessagesRequest

    # ── Шаг 1: получить entity ────────────────────────────────────────────────
    entity = None
    try:
        if not chat_id.lstrip("-").isdigit():
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
    except ChannelPrivateError:
        return False, "private"
    except (PeerIdInvalidError, ValueError):
        return False, "invalid_id"
    except Exception as e:
        return False, str(e)

    if entity is None:
        return False, "not_found"

    # ── Шаг 2: попробовать вступить если есть username ────────────────────────
    if chat_id.startswith("https://t.me/+") or chat_id.startswith("t.me/+"):
        invite_hash = chat_id.rstrip("/").split("+")[-1]
        try:
            await client(ImportChatInviteRequest(invite_hash))
            await asyncio.sleep(1)
            entity = await client.get_entity(entity.id)
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

    elif getattr(entity, "username", None):
        try:
            await client(JoinChannelRequest(entity))
            await asyncio.sleep(1)
            entity = await client.get_entity(entity.id)
        except UserAlreadyParticipantError:
            pass
        except ChannelsTooMuchError:
            return False, "too_many_channels"
        except InviteRequestSentError:
            return False, "join_pending"
        except Exception:
            pass

    # ── Шаг 3: отправить тестовое сообщение и сразу удалить ──────────────────
    try:
        msg = await client.send_message(entity, ".")
        # Сообщение отправилось — удаляем его немедленно
        try:
            await client.delete_messages(entity, [msg.id])
        except Exception:
            pass  # не смогли удалить — не критично, главное что отправилось
        return True, "ok"

    except FloodWaitError as e:
        # Временный флуд — не значит что нельзя писать
        log.warning("FloodWait %ds при проверке чата %s", e.seconds, chat_id)
        return True, "ok"

    except SlowModeWaitError:
        # SlowMode — писать можно, просто с задержкой
        return True, "ok"

    except UserBannedInChannelError:
        return False, "banned"

    except ChatWriteForbiddenError:
        return False, "write_forbidden"

    except Exception as e:
        error = str(e).lower()
        # Дополнительные случаи которые Telethon может вернуть как generic Exception
        if "banned" in error:
            return False, "banned"
        if "forbidden" in error or "not allowed" in error:
            return False, "write_forbidden"
        if "private" in error:
            return False, "private"
        log.warning("Неизвестная ошибка при проверке %s: %s", chat_id, e)
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
