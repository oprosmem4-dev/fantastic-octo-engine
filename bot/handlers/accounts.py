from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import accounts_kb, account_actions_kb, cancel_kb
from models import User
from services import account_service

router = Router()


class AddAccountFSM(StatesGroup):
    phone = State()
    api_id = State()
    api_hash = State()
    code = State()
    password = State()


_pending_clients: dict[int, tuple] = {}


@router.message(Command("accounts"))
@router.callback_query(F.data == "menu:accounts")
async def show_accounts(event: Message | CallbackQuery, user: User, db: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    accounts = await account_service.get_user_accounts(db, user.id)
    text = f"📱 Ваши аккаунты ({len(accounts)}):"
    kb = accounts_kb(accounts)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("acc:view:"))
async def view_account(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    account_id = int(callback.data.split(":")[2])
    accounts = await account_service.get_user_accounts(db, user.id)
    account = next((a for a in accounts if a.id == account_id), None)
    if account is None:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    status = "✅ Активен" if account.is_active else "❌ Неактивен"
    banned = " | 🚫 Заблокирован" if account.is_banned else ""
    text = (
        f"📱 Аккаунт: {account.phone}\n"
        f"Статус: {status}{banned}\n"
        f"Чатов: {account.chats_count}"
    )
    await callback.message.edit_text(text, reply_markup=account_actions_kb(account_id))
    await callback.answer()


@router.callback_query(F.data.startswith("acc:delete:"))
async def delete_account(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    account_id = int(callback.data.split(":")[2])
    deleted = await account_service.delete_account(db, account_id, user.id)
    if deleted:
        await callback.answer("✅ Аккаунт удалён", show_alert=True)
    else:
        await callback.answer("❌ Аккаунт не найден", show_alert=True)
    accounts = await account_service.get_user_accounts(db, user.id)
    await callback.message.edit_text(f"📱 Ваши аккаунты ({len(accounts)}):", reply_markup=accounts_kb(accounts))


@router.callback_query(F.data == "acc:add")
async def start_add_account(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddAccountFSM.phone)
    await callback.message.edit_text(
        "📱 Введите номер телефона в формате +79001234567:",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AddAccountFSM.phone)
async def fsm_phone(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=message.text.strip())
    await state.set_state(AddAccountFSM.api_id)
    await message.answer("🔑 Введите api_id (число с my.telegram.org):", reply_markup=cancel_kb())


@router.message(AddAccountFSM.api_id)
async def fsm_api_id(message: Message, state: FSMContext) -> None:
    try:
        api_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ api_id должен быть числом. Попробуйте снова:", reply_markup=cancel_kb())
        return
    await state.update_data(api_id=api_id)
    await state.set_state(AddAccountFSM.api_hash)
    await message.answer("🔑 Введите api_hash:", reply_markup=cancel_kb())


@router.message(AddAccountFSM.api_hash)
async def fsm_api_hash(message: Message, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    api_hash = message.text.strip()
    await state.update_data(api_hash=api_hash)

    phone = data["phone"]
    api_id = data["api_id"]

    try:
        client, phone_code_hash = await account_service.send_code(phone, api_id, api_hash)
        _pending_clients[user.id] = (client, phone, api_id, api_hash, phone_code_hash)
        await state.set_state(AddAccountFSM.code)
        await message.answer("📨 Код отправлен. Введите код подтверждения:", reply_markup=cancel_kb())
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}", reply_markup=cancel_kb())


@router.message(AddAccountFSM.code)
async def fsm_code(message: Message, state: FSMContext, user: User, db: AsyncSession) -> None:
    code = message.text.strip()
    pending = _pending_clients.get(user.id)
    if pending is None:
        await state.clear()
        await message.answer("❌ Сессия истекла. Начните заново.", reply_markup=cancel_kb())
        return

    client, phone, api_id, api_hash, phone_code_hash = pending
    try:
        session_string = await account_service.sign_in(client, phone, code, phone_code_hash)
        await account_service.add_account(db, user.id, phone, api_id, api_hash, session_string)
        _pending_clients.pop(user.id, None)
        await state.clear()
        await message.answer("✅ Аккаунт успешно добавлен!")
    except Exception as e:
        if "Two-steps" in str(e) or "password" in str(e).lower():
            await state.set_state(AddAccountFSM.password)
            await message.answer("🔒 Введите пароль двухфакторной аутентификации:", reply_markup=cancel_kb())
        else:
            _pending_clients.pop(user.id, None)
            await state.clear()
            await message.answer(f"❌ Ошибка входа: {e}", reply_markup=cancel_kb())


@router.message(AddAccountFSM.password)
async def fsm_password(message: Message, state: FSMContext, user: User, db: AsyncSession) -> None:
    password = message.text.strip()
    pending = _pending_clients.get(user.id)
    if pending is None:
        await state.clear()
        await message.answer("❌ Сессия истекла. Начните заново.", reply_markup=cancel_kb())
        return

    client, phone, api_id, api_hash, phone_code_hash = pending
    try:
        await client.sign_in(password=password)
        session_string = client.session.save()
        await account_service.add_account(db, user.id, phone, api_id, api_hash, session_string)
        _pending_clients.pop(user.id, None)
        await state.clear()
        await message.answer("✅ Аккаунт успешно добавлен!")
    except Exception as e:
        _pending_clients.pop(user.id, None)
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}", reply_markup=cancel_kb())
