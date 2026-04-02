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
# ── Добавление системного аккаунта (только админ) ─────────────────────────────

class AddSystemAccount(StatesGroup):
    api_id   = State()
    api_hash = State()
    phone    = State()
    code     = State()
    password = State()

@router.callback_query(F.data == "admin:addacc")
async def admin_start_add_acc(query: CallbackQuery, state: FSMContext, user: User):
    if not is_admin(user):
        return
    await query.message.edit_text(
        "➕ *Добавление системного аккаунта*\n\n"
        "Этот аккаунт будет доступен всем пользователям.\n\n"
        "*Шаг 1/3* — Введите API\\_ID:",
        reply_markup=kb_cancel(),
        parse_mode="Markdown"
    )
    await state.set_state(AddSystemAccount.api_id)

@router.message(AddSystemAccount.api_id)
async def admin_got_apiid(message: Message, state: FSMContext, user: User):
    if not is_admin(user):
        return
    if not message.text.strip().isdigit():
        await message.answer("❌ Должно быть числом:")
        return
    await state.update_data(api_id=int(message.text.strip()))
    await message.answer("*Шаг 2/3* — Введите API\\_HASH:", reply_markup=kb_cancel(), parse_mode="Markdown")
    await state.set_state(AddSystemAccount.api_hash)

@router.message(AddSystemAccount.api_hash)
async def admin_got_apihash(message: Message, state: FSMContext, user: User):
    if not is_admin(user):
        return
    await state.update_data(api_hash=message.text.strip())
    await message.answer("*Шаг 3/3* — Введите номер телефона:\nПример: `+998901234567`", reply_markup=kb_cancel(), parse_mode="Markdown")
    await state.set_state(AddSystemAccount.phone)

@router.message(AddSystemAccount.phone)
async def admin_got_phone(message: Message, state: FSMContext, user: User):
    if not is_admin(user):
        return
    phone = message.text.strip()
    data = await state.get_data()
    await message.answer(f"📨 Отправляю код на {phone}...")
    try:
        client, phone_code_hash = await account_service.send_code(data["api_id"], data["api_hash"], phone)
        await state.update_data(phone=phone, phone_code_hash=phone_code_hash)
        message.bot._pending_clients = getattr(message.bot, "_pending_clients", {})
        message.bot._pending_clients[message.from_user.id] = client
        await message.answer("✅ Код отправлен!\n\nВведите код из Telegram:", reply_markup=kb_cancel())
        await state.set_state(AddSystemAccount.code)
    except Exception as e:
        await message.answer(f"❌ Ошибка: `{e}`", parse_mode="Markdown")
        await state.clear()

@router.message(AddSystemAccount.code)
async def admin_got_code(message: Message, state: FSMContext, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    from telethon.errors import SessionPasswordNeededError
    code = message.text.strip().replace(" ", "")
    data = await state.get_data()
    client = getattr(message.bot, "_pending_clients", {}).get(message.from_user.id)
    if not client:
        await message.answer("❌ Сессия истекла. Начните заново.")
        await state.clear()
        return
    try:
        session_str = await account_service.sign_in_code(client, data["phone"], code, data["phone_code_hash"])
    except SessionPasswordNeededError:
        await message.answer("🔐 Введите пароль 2FA:", reply_markup=kb_cancel())
        await state.set_state(AddSystemAccount.password)
        return
    except Exception as e:
        await message.answer(f"❌ Неверный код: `{e}`", parse_mode="Markdown")
        await client.disconnect()
        message.bot._pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return
    await _finish_system_account(message, state, user, db, client, data["phone"], session_str)

@router.message(AddSystemAccount.password)
async def admin_got_password(message: Message, state: FSMContext, user: User, db: AsyncSession):
    if not is_admin(user):
        return
    client = getattr(message.bot, "_pending_clients", {}).get(message.from_user.id)
    data = await state.get_data()
    try:
        session_str = await account_service.sign_in_2fa(client, message.text.strip())
    except Exception as e:
        await message.answer(f"❌ Неверный пароль: `{e}`", parse_mode="Markdown")
        await client.disconnect()
        message.bot._pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return
    await _finish_system_account(message, state, user, db, client, data["phone"], session_str)

async def _finish_system_account(message, state, user, db, client, phone, session_str):
    data = await state.get_data()
    name = await account_service.get_me_name(client)
    await client.disconnect()
    message.bot._pending_clients.pop(message.from_user.id, None)
    await state.clear()

    # is_system=True — главное отличие от личного аккаунта
    acc = await account_service.create_account(
        db,
        api_id=data["api_id"],
        api_hash=data["api_hash"],
        phone=phone,
        session_string=session_str,
        owner_id=None,      # нет владельца — системный
        is_system=True,     # доступен всем пользователям
    )
    await message.answer(
        f"✅ Системный аккаунт *{name}* (`{phone}`) добавлен!\n"
        f"Теперь он доступен всем пользователям.",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )
