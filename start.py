"""
bot/handlers/start.py — /start, /help, главное меню, статус.
"""
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from models import User
from bot.keyboards import kb_main_menu, kb_back_to_menu

router = Router()


def status_text(user: User) -> str:
    """Текст статуса пользователя."""
    tasks_count    = len(user.tasks)
    accounts_count = len(user.accounts)
    active_tasks   = sum(1 for t in user.tasks if t.is_active)

    return (
        f"👤 *{user.full_name}*\n"
        f"🆔 `{user.id}`\n\n"
        f"📊 {user.subscription_status}\n\n"
        f"📋 Задач: {tasks_count} (активных: {active_tasks})\n"
        f"🤖 Аккаунтов: {accounts_count}\n"
        f"📬 Лимит чатов: {user.max_chats}"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, user: User):
    """Приветствие при /start."""
    greeting = "👋 *Добро пожаловать!*" if len(user.tasks) == 0 else "👋 *С возвращением!*"
    text = (
        f"{greeting}\n\n"
        f"Я помогу делать рассылки в Telegram-чаты.\n\n"
        f"{user.subscription_status}\n\n"
        "Выбери действие:"
    )
    await message.answer(text, reply_markup=kb_main_menu(user.has_access), parse_mode="Markdown")


@router.message(Command("help"))
async def cmd_help(message: Message, user: User):
    """Справка."""
    text = (
        "📋 *Команды:*\n\n"
        "/start — главное меню\n"
        "/status — ваш статус\n"
        "/tasks — управление задачами\n"
        "/accounts — управление аккаунтами\n"
        "/pay — оплата подписки\n"
    )
    if user.is_admin:
        text += "\n*Администратор:*\n/admin — панель управления\n"
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("status"))
async def cmd_status(message: Message, user: User):
    await message.answer(status_text(user), parse_mode="Markdown", reply_markup=kb_back_to_menu())


# ── Callback-и ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu")
async def cb_menu(query: CallbackQuery, user: User):
    """Вернуться в главное меню."""
    await query.message.edit_text(
        f"👋 Главное меню\n{user.subscription_status}",
        reply_markup=kb_main_menu(user.has_access),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "status")
async def cb_status(query: CallbackQuery, user: User):
    await query.message.edit_text(
        status_text(user),
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )
