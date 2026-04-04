"""
services/task_service.py — управление задачами рассылок.
"""
import json
import logging
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from config import MAX_CHATS_PER_USER, MAX_CHATS_PER_ACCOUNT
from models import Task, TaskChat, TaskAccount, Account, User, Log

log = logging.getLogger(__name__)


async def get_tasks(db: AsyncSession, user_id: int) -> list[Task]:
    result = await db.execute(
        select(Task)
        .options(selectinload(Task.chats), selectinload(Task.accounts))
        .where(Task.user_id == user_id)
        .order_by(Task.created_at.desc())
    )
    return list(result.scalars().all())


async def get_task(db: AsyncSession, task_id: int, user_id: int) -> Task | None:
    result = await db.execute(
        select(Task)
        .options(selectinload(Task.chats), selectinload(Task.accounts))
        .where(Task.id == task_id, Task.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def create_task(
    db: AsyncSession,
    user: User,
    name: str,
    message: str,
    interval_minutes: int,
    chats: list[dict],
) -> dict | None:
    """
    Создать задачу. Возвращает dict а не ORM объект —
    это избегает MissingGreenlet при обращении к task.chats после commit.
    """
    existing_tasks = await get_tasks(db, user.id)
    current_chats = sum(len(t.chats) for t in existing_tasks)

    if current_chats + len(chats) > user.max_chats:
        log.warning("Пользователь %d превысил лимит чатов", user.id)
        return None

    task = Task(
        user_id=user.id,
        name=name,
        message=message,
        interval_minutes=interval_minutes,
    )
    db.add(task)
    await db.flush()

    for chat in chats:
        db.add(TaskChat(
            task_id=task.id,
            chat_id=str(chat["id"]),
            chat_title=chat.get("title", ""),
        ))

    await _distribute_chats(db, task, user, chats)
    await db.commit()

    # Возвращаем dict — не трогаем ORM объект после commit
    return {
        "id": task.id,
        "name": name,
        "chats_count": len(chats),
        "interval_minutes": interval_minutes,
    }


async def delete_task(db: AsyncSession, task_id: int, user_id: int) -> bool:
    task = await get_task(db, task_id, user_id)
    if not task:
        return False
    # Сначала удаляем логи этой задачи, иначе FK не даст удалить Task
    await db.execute(delete(Log).where(Log.task_id == task_id))

# Потом удаляем саму задачу
    await db.delete(task)
    await db.commit()
    return True


async def toggle_task(db: AsyncSession, task_id: int, user_id: int) -> bool | None:
    task = await get_task(db, task_id, user_id)
    if not task:
        return None
    task.is_active = not task.is_active
    await db.commit()
    return task.is_active


async def _distribute_chats(db, task, user, chats):
    result = await db.execute(
        select(Account).where(
            Account.is_active == True,
            Account.is_banned == False,
        ).where(
            (Account.owner_id == user.id) | (Account.is_system == True)
        ).order_by(Account.chats_count.asc())
    )
    accounts = list(result.scalars().all())

    if not accounts:
        log.warning("Нет доступных аккаунтов для задачи %d", task.id)
        return

    chat_ids = [str(c["id"]) for c in chats]
    chunks = [chat_ids[i:i + MAX_CHATS_PER_ACCOUNT] for i in range(0, len(chat_ids), MAX_CHATS_PER_ACCOUNT)]

    for i, chunk in enumerate(chunks):
        if i >= len(accounts):
            break
        acc = accounts[i]
        db.add(TaskAccount(task_id=task.id, account_id=acc.id, chat_ids=json.dumps(chunk)))
        acc.chats_count += len(chunk)
