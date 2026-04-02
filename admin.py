"""
bot/handlers/admin.py — панель администратора.

Функции:
  - Статистика сервиса
  - Управление пользователями (блок/разблок, лимиты, подписка)
  - Управление системными аккаунтами
"""
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from config import OWNER_ID
from models import User, Account, Task, Payment
from services import account_service
from services.user_service import (
    get_user, get_all_users, block_user, unblock_user,
    set_max_chats, add_subscription, count_active_users
)
from bot.keyboards import kb_admin_menu, kb_cancel, kb_back_to_menu

log = logging.getLogger(__name__)
router = Router()


def is_admin(user: User) -> bool:
    return user.is_admin or user.id == OWNER_ID


# ── Вход в админ-панель ───────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message, user: User):
    if not is_admin(user):
        await message.answer("❌ Нет доступа.")
        return
    await message.answer("👑 *Панель администратора*", reply_markup=kb_admin_menu(), parse_mode="Markdown")


@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(query: CallbackQuery, user: User):
    if not is_admin(user):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await query.message.edit_text("👑 *Панель администратора*", reply_markup=kb_admin_menu(), parse_mode="Markdown")


# ── Статистика ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:stats")
async def admin_stats(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return

    # Считаем всё
    total_users = (await db.execute(select(func.count(User.id)))).scalar()
    active_users = await count_active_users(db)
    total_tasks  = (await db.execute(select(func.count(Task.id)).where(Task.is_active == True))).scalar()
    total_accs   = (await db.execute(select(func.count(Account.id)).where(Account.is_active == True))).scalar()

    # Загрузка аккаунтов
    result = await db.execute(
        select(Account).where(Account.is_active == True, Account.is_banned == False)
    )
    accounts = result.scalars().all()
    acc_lines = "\n".join(
        f"  • `{a.phone}`: {a.chats_count}/35 чатов (свободно: {max(0, 35 - a.chats_count)})"
        for a in accounts
    ) or "  (нет аккаунтов)"

    from config import MAX_CHATS_PER_ACCOUNT
    text = (
        f"📊 *Статистика*\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"👤 Активных (с задачами): {active_users}\n"
        f"📋 Активных задач: {total_tasks}\n"
        f"🤖 Активных аккаунтов: {total_accs}\n\n"
        f"🤖 *Загрузка аккаунтов:*\n{acc_lines}"
    )
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")
    ]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


# ── Пользователи ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:users")
async def admin_users(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return

    text = (
        "👥 *Управление пользователями*\n\n"
        "Команды:\n"
        "`/giveday <user_id> <days>` — выдать подписку\n"
        "`/block <user_id>` — заблокировать\n"
        "`/unblock <user_id>` — разблокировать\n"
        "`/setlimit <user_id> <chats>` — лимит чатов\n"
        "`/userinfo <user_id>` — инфо о пользователе"
    )
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")
    ]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.message(Command("giveday"))
async def cmd_giveday(message: Message, user: User, db: AsyncSession):
    """Выдать подписку: /giveday 123456789 30"""
    if not is_admin(user):
        return
    args = message.text.split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("Использование: `/giveday <user_id> <days>`", parse_mode="Markdown")
        return
    target_id, days = int(args[1]), int(args[2])
    target = await get_user(db, target_id)
    if not target:
        await message.answer(f"❌ Пользователь `{target_id}` не найден.", parse_mode="Markdown")
        return
    await add_subscription(db, target, days)
    await message.answer(f"✅ Пользователю `{target_id}` выдано *{days} дней* подписки.", parse_mode="Markdown")
    # Уведомляем пользователя
    try:
        await message.bot.send_message(target_id, f"🎉 Вам выдана подписка на *{days} дней*!", parse_mode="Markdown")
    except Exception:
        pass


@router.message(Command("block"))
async def cmd_block(message: Message, user: User, db: AsyncSession):
    """Заблокировать пользователя."""
    if not is_admin(user):
        return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: `/block <user_id>`", parse_mode="Markdown")
        return
    ok = await block_user(db, int(args[1]))
    await message.answer("✅ Заблокирован." if ok else "❌ Не найден.", parse_mode="Markdown")


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, user: User, db: AsyncSession):
    """Разблокировать пользователя."""
    if not is_admin(user):
        return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: `/unblock <user_id>`", parse_mode="Markdown")
        return
    ok = await unblock_user(db, int(args[1]))
    await message.answer("✅ Разблокирован." if ok else "❌ Не найден.", parse_mode="Markdown")


@router.message(Command("setlimit"))
async def cmd_setlimit(message: Message, user: User, db: AsyncSession):
    """Установить лимит чатов."""
    if not is_admin(user):
        return
    args = message.text.split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("Использование: `/setlimit <user_id> <chats>`", parse_mode="Markdown")
        return
    ok = await set_max_chats(db, int(args[1]), int(args[2]))
    await message.answer("✅ Лимит установлен." if ok else "❌ Не найден.", parse_mode="Markdown")


@router.message(Command("userinfo"))
async def cmd_userinfo(message: Message, user: User, db: AsyncSession):
    """Информация о пользователе."""
    if not is_admin(user):
        return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Использование: `/userinfo <user_id>`", parse_mode="Markdown")
        return
    target = await get_user(db, int(args[1]))
    if not target:
        await message.answer("❌ Не найден.", parse_mode="Markdown")
        return
    text = (
        f"👤 *Пользователь {target.id}*\n"
        f"Имя: {target.full_name}\n"
        f"Username: @{target.username or '—'}\n"
        f"Статус: {target.subscription_status}\n"
        f"Заблокирован: {'Да' if target.is_blocked else 'Нет'}\n"
        f"Лимит чатов: {target.max_chats}\n"
        f"Задач: {len(target.tasks)}\n"
        f"Аккаунтов: {len(target.accounts)}\n"
        f"Регистрация: {target.created_at.strftime('%Y-%m-%d')}"
    )
    await message.answer(text, parse_mode="Markdown")


# ── Системные аккаунты ────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:accounts")
async def admin_accounts(query: CallbackQuery, user: User, db: AsyncSession):
    if not is_admin(user):
        return

    accounts = await account_service.get_accounts(db)  # системные
    lines = []
    for acc in accounts:
        status = "✅" if acc.is_active and not acc.is_banned else "❌"
        lines.append(f"{status} `{acc.phone}` — {acc.chats_count}/35 чатов")

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить системный", callback_data="admin:addacc")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")],
    ])

    text = "🤖 *Системные аккаунты*\n\n" + ("\n".join(lines) or "(нет аккаунтов)")
    await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
