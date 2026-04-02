from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import TRIAL_DAYS
from models import User


async def get_or_create_user(
    db: AsyncSession,
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        now = datetime.now(timezone.utc)
        user = User(
            id=user_id,
            username=username,
            full_name=full_name,
            trial_ends_at=now + timedelta(days=TRIAL_DAYS),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        if user.username != username or user.full_name != full_name:
            user.username = username
            user.full_name = full_name
            await db.commit()
    return user


async def add_subscription(db: AsyncSession, user_id: int, days: int) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise ValueError(f"User {user_id} not found")
    now = datetime.now(timezone.utc)
    base = user.sub_ends_at if (user.sub_ends_at and user.sub_ends_at > now) else now
    user.sub_ends_at = base + timedelta(days=days)
    await db.commit()
    await db.refresh(user)
    return user


async def block_user(db: AsyncSession, user_id: int) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise ValueError(f"User {user_id} not found")
    user.is_blocked = True
    await db.commit()
    return user


async def unblock_user(db: AsyncSession, user_id: int) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise ValueError(f"User {user_id} not found")
    user.is_blocked = False
    await db.commit()
    return user


async def set_chat_limit(db: AsyncSession, user_id: int, max_chats: int) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise ValueError(f"User {user_id} not found")
    user.max_chats = max_chats
    await db.commit()
    return user


async def get_user(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()
