from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import admin_kb, admin_accounts_kb, cancel_kb
from config import OWNER_ID
from models import User, Account
from services import account_service, user_service

router = Router()


class AddSystemAccountFSM(StatesGroup):
    phone = State()
    api_id = State()
    api_hash = State()
    code = State()
    password = State()


_pending_admin_clients: dict[int, tuple] = {}


def is_admin(user: User) -> bool:
    return user.is_admin or user.id == OWNER_ID


@router.message(Command("admin"))
async def cmd_admin(message: Message, user: User) -> None:
    if not is_admin(user):
        await message.answer("❌ Нет доступа.")
        return
    await message.answer("🔧 Панель администратора:", reply_markup=admin_kb())


@router.callback_query(F.data == "admin:main")
async def cb_admin_main(callback: CallbackQuery, user: User) -> None:
    if not is_admin(user):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text("🔧 Панель администратора:", reply_markup=admin_kb())
    await callback.answer()


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    if not is_admin(user):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return
    from models import Task
    user_count = (await db.execute(select(func.count()).select_from(User))).scalar()
    task_count = (await db.execute(select(func.count()).select_from(Task))).scalar()
    acc_count = (await db.execute(select(func.count()).select_from(Account))).scalar()
    text = (
        f"📊 Статистика:\n\n"
        f"👥 Пользователи: {user_count}\n"
        f"📋 Задачи: {task_count}\n"
        f"📱 Аккаунты: {acc_count}"
    )
    from bot.keyboards import admin_kb
    await callback.message.edit_text(text, reply_markup=admin_kb())
    await callback.answer()


@router.callback_query(F.data == "admin:accounts")
async def cb_admin_accounts(callback: CallbackQuery, user: User) -> None:
    if not is_admin(user):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text("🔑 Управление аккаунтами:", reply_markup=admin_accounts_kb())
    await callback.answer()


@router.callback_query(F.data == "admin:add_system")
async def cb_add_system(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    if not is_admin(user):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return
    await state.set_state(AddSystemAccountFSM.phone)
    await callback.message.edit_text(
        "📱 Введите номер телефона системного аккаунта (+79001234567):",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AddSystemAccountFSM.phone)
async def admin_fsm_phone(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=message.text.strip())
    await state.set_state(AddSystemAccountFSM.api_id)
    await message.answer("🔑 Введите api_id:", reply_markup=cancel_kb())


@router.message(AddSystemAccountFSM.api_id)
async def admin_fsm_api_id(message: Message, state: FSMContext) -> None:
    try:
        api_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ api_id должен быть числом:", reply_markup=cancel_kb())
        return
    await state.update_data(api_id=api_id)
    await state.set_state(AddSystemAccountFSM.api_hash)
    await message.answer("🔑 Введите api_hash:", reply_markup=cancel_kb())


@router.message(AddSystemAccountFSM.api_hash)
async def admin_fsm_api_hash(message: Message, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    api_hash = message.text.strip()
    await state.update_data(api_hash=api_hash)
    try:
        client, phone_code_hash = await account_service.send_code(data["phone"], data["api_id"], api_hash)
        _pending_admin_clients[user.id] = (client, data["phone"], data["api_id"], api_hash, phone_code_hash)
        await state.set_state(AddSystemAccountFSM.code)
        await message.answer("📨 Код отправлен. Введите код:", reply_markup=cancel_kb())
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}", reply_markup=cancel_kb())


@router.message(AddSystemAccountFSM.code)
async def admin_fsm_code(message: Message, state: FSMContext, user: User, db: AsyncSession) -> None:
    code = message.text.strip()
    pending = _pending_admin_clients.get(user.id)
    if not pending:
        await state.clear()
        await message.answer("❌ Сессия истекла.", reply_markup=cancel_kb())
        return
    client, phone, api_id, api_hash, phone_code_hash = pending
    try:
        session_string = await account_service.sign_in(client, phone, code, phone_code_hash)
        await account_service.add_account(db, None, phone, api_id, api_hash, session_string, is_system=True)
        _pending_admin_clients.pop(user.id, None)
        await state.clear()
        await message.answer("✅ Системный аккаунт добавлен!")
    except Exception as e:
        if "password" in str(e).lower():
            await state.set_state(AddSystemAccountFSM.password)
            await message.answer("🔒 Введите пароль 2FA:", reply_markup=cancel_kb())
        else:
            _pending_admin_clients.pop(user.id, None)
            await state.clear()
            await message.answer(f"❌ Ошибка: {e}", reply_markup=cancel_kb())


@router.message(AddSystemAccountFSM.password)
async def admin_fsm_password(message: Message, state: FSMContext, user: User, db: AsyncSession) -> None:
    password = message.text.strip()
    pending = _pending_admin_clients.get(user.id)
    if not pending:
        await state.clear()
        await message.answer("❌ Сессия истекла.", reply_markup=cancel_kb())
        return
    client, phone, api_id, api_hash, phone_code_hash = pending
    try:
        await client.sign_in(password=password)
        session_string = client.session.save()
        await account_service.add_account(db, None, phone, api_id, api_hash, session_string, is_system=True)
        _pending_admin_clients.pop(user.id, None)
        await state.clear()
        await message.answer("✅ Системный аккаунт добавлен!")
    except Exception as e:
        _pending_admin_clients.pop(user.id, None)
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}", reply_markup=cancel_kb())


@router.message(Command("giveday"))
async def cmd_giveday(message: Message, user: User, db: AsyncSession) -> None:
    if not is_admin(user):
        await message.answer("❌ Нет доступа.")
        return
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.answer("Использование: /giveday <user_id> <days>")
        return
    try:
        target_id, days = int(args[0]), int(args[1])
        await user_service.add_subscription(db, target_id, days)
        await message.answer(f"✅ Пользователю {target_id} добавлено {days} дней.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("block"))
async def cmd_block(message: Message, user: User, db: AsyncSession) -> None:
    if not is_admin(user):
        await message.answer("❌ Нет доступа.")
        return
    args = message.text.split()[1:]
    if not args:
        await message.answer("Использование: /block <user_id>")
        return
    try:
        await user_service.block_user(db, int(args[0]))
        await message.answer(f"✅ Пользователь {args[0]} заблокирован.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, user: User, db: AsyncSession) -> None:
    if not is_admin(user):
        await message.answer("❌ Нет доступа.")
        return
    args = message.text.split()[1:]
    if not args:
        await message.answer("Использование: /unblock <user_id>")
        return
    try:
        await user_service.unblock_user(db, int(args[0]))
        await message.answer(f"✅ Пользователь {args[0]} разблокирован.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("setlimit"))
async def cmd_setlimit(message: Message, user: User, db: AsyncSession) -> None:
    if not is_admin(user):
        await message.answer("❌ Нет доступа.")
        return
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.answer("Использование: /setlimit <user_id> <chats>")
        return
    try:
        await user_service.set_chat_limit(db, int(args[0]), int(args[1]))
        await message.answer(f"✅ Лимит пользователя {args[0]} установлен: {args[1]}.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("userinfo"))
async def cmd_userinfo(message: Message, user: User, db: AsyncSession) -> None:
    if not is_admin(user):
        await message.answer("❌ Нет доступа.")
        return
    args = message.text.split()[1:]
    if not args:
        await message.answer("Использование: /userinfo <user_id>")
        return
    try:
        target = await user_service.get_user(db, int(args[0]))
        if target is None:
            await message.answer("Пользователь не найден.")
            return
        text = (
            f"👤 ID: <code>{target.id}</code>\n"
            f"Имя: {target.full_name or '—'}\n"
            f"Username: @{target.username or '—'}\n"
            f"Статус: {target.subscription_status}\n"
            f"Заблокирован: {'Да' if target.is_blocked else 'Нет'}\n"
            f"Лимит чатов: {target.max_chats}"
        )
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
