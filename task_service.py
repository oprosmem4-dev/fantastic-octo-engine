"""
services/task_service.py — управление задачами рассылок.

ИЗМЕНЕНИЯ:
  - _distribute_chats: при системных аккаунтах чаты делятся равномерно
    по всем доступным системным аккаунтам (round-robin).
  - chat_title сохраняется как "title (username)" для отображения.
"""
import json
import logging

from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from config import MAX_CHATS_PER_USER
from models import Task, TaskChat, TaskAccount, Account, User, Log

log = logging.getLogger(__name__)


async def get_tasks(db: AsyncSession, user_id: int) -> list[Task]:
    result = await db.execute(
        select(Task)
        .options(
            selectinload(Task.chats),
            selectinload(Task.accounts).selectinload(TaskAccount.account),
        )
        .where(Task.user_id == user_id)
        .order_by(Task.created_at.desc())
    )
    return list(result.scalars().all())


async def get_task(db: AsyncSession, task_id: int, user_id: int) -> Task | None:
    result = await db.execute(
        select(Task)
        .options(
            selectinload(Task.chats),
            selectinload(Task.accounts).selectinload(TaskAccount.account),
        )
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
    preferred_account_id: int | None = None,
    photo_file_ids: list[str] | None = None,
    format_entities: list[dict] | None = None,
) -> dict | None:
    """
    Создать задачу.
    Возвращает dict (не ORM) чтобы избежать MissingGreenlet после commit.

    chats — список dict с ключами: id, title, username (опционально).
    """
    existing_tasks = await get_tasks(db, user.id)
    current_chats  = sum(len(t.chats) for t in existing_tasks)

    if current_chats + len(chats) > user.max_chats:
        log.warning("Пользователь %d превысил лимит чатов", user.id)
        return None

    task = Task(
        user_id=user.id,
        name=name,
        message=message,
        photo_file_ids=json.dumps(photo_file_ids or [], ensure_ascii=False),
        format_entities=json.dumps(format_entities or [], ensure_ascii=False),
        interval_minutes=interval_minutes,
    )
    db.add(task)
    await db.flush()

    for chat in chats:
        # Сохраняем username в chat_title для отображения в боте
        username = chat.get("username")
        title    = chat.get("title") or chat.get("id")
        stored_title = f"@{username}" if username else title

        db.add(TaskChat(
            task_id=task.id,
            chat_id=str(chat["id"]),
            chat_title=stored_title,
        ))

    await _distribute_chats(db, task, user, chats, preferred_account_id=preferred_account_id)
    await db.commit()

    return {
        "id":               task.id,
        "name":             name,
        "chats_count":      len(chats),
        "interval_minutes": interval_minutes,
    }


async def delete_task(db: AsyncSession, task_id: int, user_id: int) -> bool:
    task = await get_task(db, task_id, user_id)
    if not task:
        return False
    await db.execute(delete(Log).where(Log.task_id == task_id))
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


async def _distribute_chats(
    db,
    task,
    user,
    chats: list[dict],
    preferred_account_id: int | None = None,
):
    """
    Распределить чаты по аккаунтам.

    preferred_account_id задан → все чаты на этот аккаунт (личный выбор).

    preferred_account_id is None (системные аккаунты):
      Чаты делятся РАВНОМЕРНО по всем доступным системным аккаунтам (round-robin).
      При одном системном аккаунте — все чаты на него.
      Лимита чатов на аккаунт нет.
    """
    chat_ids = [str(c["id"]) for c in chats]

    if preferred_account_id is not None:
        # Личный аккаунт — все чаты на него
        result = await db.execute(
            select(Account).where(
                Account.id == preferred_account_id,
                Account.is_active == True,
                Account.is_banned == False,
                Account.status == "ok",
            )
        )
        accounts = list(result.scalars().all())

        if not accounts:
            log.warning("Выбранный аккаунт %d недоступен для задачи %d",
                        preferred_account_id, task.id)
            return

        acc = accounts[0]
        db.add(TaskAccount(
            task_id=task.id,
            account_id=acc.id,
            chat_ids=json.dumps(chat_ids),
        ))
        acc.chats_count += len(chat_ids)
        log.info("Задача %d: %d чатов → %s (личный)", task.id, len(chat_ids), acc.phone)
        return

    # Системные аккаунты — round-robin
    result = await db.execute(
        select(Account).where(
            Account.is_active == True,
            Account.is_banned == False,
            Account.status == "ok",
            Account.is_system == True,
        ).order_by(Account.chats_count.asc())
    )
    system_accounts = list(result.scalars().all())

    # Fallback на личные аккаунты пользователя если системных нет
    if not system_accounts:
        result = await db.execute(
            select(Account).where(
                Account.owner_id == user.id,
                Account.is_active == True,
                Account.is_banned == False,
                Account.status == "ok",
            ).order_by(Account.chats_count.asc())
        )
        system_accounts = list(result.scalars().all())

    if not system_accounts:
        log.warning("Нет доступных аккаунтов для задачи %d", task.id)
        return

    n = len(system_accounts)

    if n == 1:
        acc = system_accounts[0]
        db.add(TaskAccount(
            task_id=task.id,
            account_id=acc.id,
            chat_ids=json.dumps(chat_ids),
        ))
        acc.chats_count += len(chat_ids)
        log.info("Задача %d: %d чатов → %s (единственный системный)",
                 task.id, len(chat_ids), acc.phone)
        return

    # Несколько системных — round-robin по наименее загруженному
    distribution: dict[int, list[str]] = {acc.id: [] for acc in system_accounts}

    for cid in chat_ids:
        # Сортируем по суммарной нагрузке: текущая + уже назначенные в этой задаче
        system_accounts.sort(
            key=lambda a: a.chats_count + len(distribution[a.id])
        )
        distribution[system_accounts[0].id].append(cid)

    for acc in system_accounts:
        ids = distribution[acc.id]
        if not ids:
            continue
        db.add(TaskAccount(
            task_id=task.id,
            account_id=acc.id,
            chat_ids=json.dumps(ids),
        ))
        acc.chats_count += len(ids)
        log.info("Задача %d: %d чатов → %s (round-robin)",
                 task.id, len(ids), acc.phone)
