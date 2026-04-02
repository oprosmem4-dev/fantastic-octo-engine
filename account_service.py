"""
services/account_service.py — управление Telegram-аккаунтами (Telethon).
Добавление, авторизация, получение списка, загрузка/выгрузка.
"""
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

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


async def check_chat_access(client: TelegramClient, chat_id: str) -> bool:
    """Проверить, может ли аккаунт писать в чат (пробная отправка)."""
    try:
        entity = await client.get_entity(int(chat_id))
        await client.send_message(entity, "👋")
        return True
    except Exception:
        return False
