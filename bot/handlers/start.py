from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import main_menu_kb
from models import User

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, user: User, db: AsyncSession) -> None:
    status = user.subscription_status
    status_emoji = {
        "active": "✅",
        "trial": "🆓",
        "expired": "❌",
        "blocked": "🚫",
    }.get(status, "❓")

    text = (
        f"👋 Привет, {user.full_name or 'друг'}!\n\n"
        f"🤖 TG SaaS — сервис рассылки в Telegram\n\n"
        f"Статус: {status_emoji} {status}\n\n"
        "Выберите действие:"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    status = user.subscription_status
    status_emoji = {
        "active": "✅",
        "trial": "🆓",
        "expired": "❌",
        "blocked": "🚫",
    }.get(status, "❓")

    text = (
        f"👋 Привет, {user.full_name or 'друг'}!\n\n"
        f"Статус: {status_emoji} {status}\n\n"
        "Выберите действие:"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    lines = [
        f"👤 ID: <code>{user.id}</code>",
        f"📛 Имя: {user.full_name or '—'}",
        f"🔖 Username: @{user.username}" if user.username else "🔖 Username: —",
        f"📊 Статус: {user.subscription_status}",
    ]
    if user.sub_ends_at and user.sub_ends_at > now:
        lines.append(f"📅 Подписка до: {user.sub_ends_at.strftime('%d.%m.%Y %H:%M')} UTC")
    elif user.trial_ends_at and user.trial_ends_at > now:
        lines.append(f"⏳ Триал до: {user.trial_ends_at.strftime('%d.%m.%Y %H:%M')} UTC")
    lines.append(f"📦 Лимит чатов: {user.max_chats}")

    from bot.keyboards import cancel_kb
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=cancel_kb())
    await callback.answer()
