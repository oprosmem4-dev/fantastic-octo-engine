from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import MAX_CHATS_PER_ACCOUNT
from models import Account, Task, TaskAccount, TaskChat


async def create_task(
    db: AsyncSession,
    user_id: int,
    name: str,
    message: str,
    interval_minutes: int,
    chat_ids: list[str],
) -> dict[str, Any]:
    task = Task(
        user_id=user_id,
        name=name,
        message=message,
        interval_minutes=interval_minutes,
    )
    db.add(task)
    await db.flush()

    for chat_id in chat_ids:
        task_chat = TaskChat(task_id=task.id, chat_id=chat_id)
        db.add(task_chat)

    await db.commit()

    return {
        "id": task.id,
        "name": task.name,
        "chats_count": len(chat_ids),
        "interval_minutes": task.interval_minutes,
    }


async def get_tasks(db: AsyncSession, user_id: int) -> list[Task]:
    result = await db.execute(
        select(Task)
        .where(Task.user_id == user_id)
        .options(selectinload(Task.chats), selectinload(Task.accounts))
    )
    return list(result.scalars().all())


async def get_task(db: AsyncSession, task_id: int, user_id: int) -> Task | None:
    result = await db.execute(
        select(Task)
        .where(Task.id == task_id, Task.user_id == user_id)
        .options(selectinload(Task.chats), selectinload(Task.accounts))
    )
    return result.scalar_one_or_none()


async def toggle_task(db: AsyncSession, task_id: int, user_id: int) -> Task | None:
    task = await get_task(db, task_id, user_id)
    if task is None:
        return None
    task.is_active = not task.is_active
    await db.commit()
    return task


async def delete_task(db: AsyncSession, task_id: int, user_id: int) -> bool:
    task = await get_task(db, task_id, user_id)
    if task is None:
        return False
    await db.delete(task)
    await db.commit()
    return True


def _distribute_chats(
    chat_ids: list[str],
    accounts: list[Account],
) -> dict[int, list[str]]:
    """Distribute chat_ids across accounts respecting MAX_CHATS_PER_ACCOUNT."""
    distribution: dict[int, list[str]] = {acc.id: [] for acc in accounts}
    account_cycle = [acc for acc in accounts]
    if not account_cycle:
        return distribution

    idx = 0
    for chat_id in chat_ids:
        assigned = False
        for _ in range(len(account_cycle)):
            acc = account_cycle[idx % len(account_cycle)]
            if len(distribution[acc.id]) < MAX_CHATS_PER_ACCOUNT:
                distribution[acc.id].append(chat_id)
                assigned = True
                break
            idx += 1
        if assigned:
            idx += 1
    return distribution


async def assign_accounts_to_task(
    db: AsyncSession,
    task: Task,
    accounts: list[Account],
) -> None:
    chat_ids = [tc.chat_id for tc in task.chats]
    distribution = _distribute_chats(chat_ids, accounts)
    for account_id, chats in distribution.items():
        if not chats:
            continue
        ta = TaskAccount(task_id=task.id, account_id=account_id)
        ta.set_chat_ids(chats)
        db.add(ta)
    await db.commit()
