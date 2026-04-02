from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from database import SessionLocal
from services.user_service import get_or_create_user


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = None
        if isinstance(event, Message):
            tg_user = event.from_user
        elif isinstance(event, CallbackQuery):
            tg_user = event.from_user

        if tg_user is None:
            return await handler(event, data)

        async with SessionLocal() as db:
            user = await get_or_create_user(
                db,
                user_id=tg_user.id,
                username=tg_user.username,
                full_name=tg_user.full_name,
            )
            data["user"] = user
            data["db"] = db
            return await handler(event, data)
