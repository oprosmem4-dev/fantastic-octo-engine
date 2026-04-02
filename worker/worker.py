from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import SessionLocal
from models import Account, Log, Task, TaskAccount
from services.account_service import make_client

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_loaded_jobs: set[int] = set()


async def send_to_chat(
    session_string: str,
    api_id: int,
    api_hash: str,
    chat_id: str,
    message: str,
    task_id: int,
    account_id: int,
) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    success = False
    error_msg = None
    try:
        await client.connect()
        await client.send_message(int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id, message)
        success = True
    except Exception as e:
        error_msg = str(e)
        logger.error(f"send_to_chat error task={task_id} chat={chat_id}: {e}")
    finally:
        await client.disconnect()

    async with SessionLocal() as db:
        log = Log(
            task_id=task_id,
            account_id=account_id,
            chat_id=chat_id,
            success=success,
            error=error_msg,
        )
        db.add(log)
        await db.commit()


async def run_task(task_id: int) -> None:
    async with SessionLocal() as db:
        result = await db.execute(
            select(Task)
            .where(Task.id == task_id, Task.is_active)
            .options(selectinload(Task.accounts), selectinload(Task.chats))
        )
        task = result.scalar_one_or_none()
        if task is None:
            return

        task.last_run_at = datetime.now(timezone.utc)
        await db.commit()

        for ta in task.accounts:
            result_acc = await db.execute(select(Account).where(Account.id == ta.account_id))
            account = result_acc.scalar_one_or_none()
            if account is None or not account.is_active or account.is_banned:
                continue
            chat_ids = ta.get_chat_ids()
            for chat_id in chat_ids:
                asyncio.create_task(
                    send_to_chat(
                        account.session_string or "",
                        account.api_id,
                        account.api_hash,
                        chat_id,
                        task.message,
                        task.id,
                        account.id,
                    )
                )


async def sync_tasks() -> None:
    async with SessionLocal() as db:
        result = await db.execute(select(Task).where(Task.is_active))
        active_tasks = list(result.scalars().all())

    active_ids = {t.id for t in active_tasks}

    for task_id in list(_loaded_jobs):
        if task_id not in active_ids:
            job_id = f"task_{task_id}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            _loaded_jobs.discard(task_id)

    for task in active_tasks:
        job_id = f"task_{task.id}"
        if task.id not in _loaded_jobs:
            scheduler.add_job(
                run_task,
                trigger="interval",
                minutes=task.interval_minutes,
                id=job_id,
                args=[task.id],
                replace_existing=True,
            )
            _loaded_jobs.add(task.id)


async def main() -> None:
    scheduler.add_job(sync_tasks, trigger="interval", seconds=30, id="sync_tasks")
    scheduler.start()
    logger.info("Worker started.")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
