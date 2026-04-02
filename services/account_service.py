from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.sessions import StringSession

from models import Account


async def make_client(account: Account) -> TelegramClient:
    session = StringSession(account.session_string or "")
    client = TelegramClient(session, account.api_id, account.api_hash)
    return client


async def send_code(phone: str, api_id: int, api_hash: str) -> tuple[TelegramClient, str]:
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    result = await client.send_code_request(phone)
    return client, result.phone_code_hash


async def sign_in(
    client: TelegramClient,
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str | None = None,
) -> str:
    await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    return client.session.save()


async def get_accounts(db: AsyncSession, user_id: int) -> list[Account]:
    result = await db.execute(
        select(Account).where(
            (Account.owner_id == user_id) | Account.is_system
        )
    )
    return list(result.scalars().all())


async def get_user_accounts(db: AsyncSession, user_id: int) -> list[Account]:
    result = await db.execute(
        select(Account).where(Account.owner_id == user_id)
    )
    return list(result.scalars().all())


async def get_system_accounts(db: AsyncSession) -> list[Account]:
    result = await db.execute(
        select(Account).where(Account.is_system, Account.is_active, ~Account.is_banned)
    )
    return list(result.scalars().all())


async def add_account(
    db: AsyncSession,
    owner_id: int | None,
    phone: str,
    api_id: int,
    api_hash: str,
    session_string: str,
    is_system: bool = False,
) -> Account:
    account = Account(
        owner_id=owner_id,
        phone=phone,
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        is_system=is_system,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


async def delete_account(db: AsyncSession, account_id: int, user_id: int) -> bool:
    result = await db.execute(
        select(Account).where(Account.id == account_id, Account.owner_id == user_id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        return False
    await db.delete(account)
    await db.commit()
    return True
