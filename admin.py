"""
bot/handlers/admin.py — панель администратора.

Функции:
  - Детальная статистика пользователей с подписками
  - Управление пользователями (блок/разблок, лимиты, подписка)
  - Управление системными аккаунтами
  - Рассылка сообщений всем пользователям (/broadcast)
"""
import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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


# ── FSM для рассылки ──────────────────────────────────────────────────────────

class BroadcastState(StatesGroup):
    message = State()
    confirm = State()


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

    now = datetime.now(timezone.utc)

    total_users = (await db.execute(select(func.count(User.id)))).scalar()
    active_users = await count_active_users(db)
    total_tasks  = (await db.execute(select(func.count(Task.id)).where(Task.is_active == True))).scalar()
    total_accs   = (await db.execute(select(func.count(Account.id)).where(Account.is_active == True))).scalar()

    # Пользователи с активной подпиской (не триал)
    result = await db.execute(
        select(User).where(User.sub_ends_at > now).order_by(User.sub_ends_at.asc())
    )
    paid_users = result.scalars().all()

    # Пользователи с триалом
    result = await db.execute(
        select(User).where(
            User.trial_ends_at > now,
            (User.sub_ends_at == None) | (User.sub_ends_at <= now)
        )
    )
    trial_users = result.scalars().all()

    # Последние платежи (топ-10)
    result = await db.execute(
        select(Payment).where(Payment.status == "paid")
        .order_by(Payment.paid_at.desc())
        .limit(10)
    )
    recent_payments = result.scalars().all()

    # Формируем текст по подпискам
    subs_lines = []
    for u in paid_users[:20]:
        days_left = (u.sub_ends_at - now).days
        uname = f"@{u.username}" if u.username else f"`{u.id}`"
        subs_lines.append(f"• {uname} — {days_left} дн.")

    trial_lines = []
    for u in trial_users[:10]:
        hours_left = int((u.trial_ends_at - now).total_seconds() / 3600)
        uname = f"@{u.username}" if u.username else f"`{u.id}`"
        trial_lines.append(f"• {uname} — {hours_left} ч.")

    # Последние платежи
    plan_names = {"1month": "1 мес", "3month": "3 мес", "6month": "6 мес"}
    pay_lines = []
    for p in recent_payments:
        date_str = p.paid_at.strftime("%d.%m %H:%M") if p.paid_at else "—"
        plan_label = plan_names.get(p.plan, p.plan)
        pay_lines.append(f"• `{p.user_id}` — {plan_label}, {int(p.amount)}⭐ ({date_str})")

    subs_block  = "\n".join(subs_lines)  if subs_lines  else "  (нет)"
    trial_block = "\n".join(trial_lines) if trial_lines else "  (нет)"
    pay_block   = "\n".join(pay_lines)   if pay_lines   else "  (нет)"

    text = (
        f"📊 *Статистика сервиса*\n\n"
        f"👥 Всего пользователей: *{total_users}*\n"
        f"👤 С активными задачами: *{active_users}*\n"
        f"📋 Активных задач: *{total_tasks}*\n"
        f"🤖 Активных аккаунтов: *{total_accs}*\n\n"
        f"✅ *Активные подписки ({len(paid_users)}):*\n{subs_block}\n\n"
        f"🎁 *На триале ({len(trial_users)}):*\n{trial_block}\n\n"
        f"💰 *Последние оплаты:*\n{pay_block}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Все пользователи", callback_data="admin:stats:users")],
        [InlineKeyboardButton(text="🔄 Обновить",         callback_data="admin:stats")],
        [InlineKeyboardButton(text="◀️ Назад",            callback_data="admin:menu")],
    ])
    await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "admin:stats:users")
async def admin_stats_users(query: CallbackQuery, user: User, db: AsyncSession):
    """Список всех пользователей с кратким статусом подписки."""
    if not is_admin(user):
        return

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(User).order_by(User.created_at.desc()).limit(50)
    )
    users = result.scalars().all()

    lines = []
    for u in users:
        if u.sub_ends_at and u.sub_ends_at > now:
            days = (u.sub_ends_at - now).days
            status = f"✅ {days}д"
        elif u.trial_ends_at and u.trial_ends_at > now:
            hours = int((u.trial_ends_at - now).total_seconds() / 3600)
            status = f"🎁 {hours}ч"
        else:
            status = "❌"

        blocked = " 🚫" if u.is_blocked else ""
        uname = f"@{u.username}" if u.username else u.full_name or str(u.id)
        lines.append(f"`{u.id}` {uname} — {status}{blocked}")

    text = f"👥 *Пользователи (последние 50):*\n\n" + "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin:stats")
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
        "`/userinfo <user_id>` — инфо о пользователе\n"
        "`/broadcast` — рассылка всем пользователям"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")
    ]])
    await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.message(Command("giveday"))
async def cmd_giveday(message: Message, user: User, db: AsyncSession):
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
    try:
        await message.bot.send_message(target_id, f"🎉 Вам выдана подписка на *{days} дней*!", parse_mode="Markdown")
    except Exception:
        pass


@router.message(Command("block"))
async def cmd_block(message: Message, user: User, db: AsyncSession):
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

    result = await db.execute(
        select(Account).where(Account.is_system == True).order_by(Account.id)
    )
    accounts = result.scalars().all()

    lines = []
    for acc in accounts:
        icon = acc.status_icon
        lines.append(f"{icon} `{acc.phone}` — {acc.chats_count} чатов")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить системный", callback_data="admin:addacc")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")],
    ])

    text = "🤖 *Системные аккаунты*\n\n" + ("\n".join(lines) or "(нет аккаунтов)")
    await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


# ── Добавление системного аккаунта ────────────────────────────────────────────

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
    await message.answer(
        "*Шаг 3/3* — Введите номер телефона:\nПример: `+998901234567`",
        reply_markup=kb_cancel(), parse_mode="Markdown"
    )
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

    acc = await account_service.create_account(
        db,
        api_id=data["api_id"],
        api_hash=data["api_hash"],
        phone=phone,
        session_string=session_str,
        owner_id=None,
        is_system=True,
    )
    await message.answer(
        f"✅ Системный аккаунт *{name}* (`{phone}`) добавлен!\n"
        f"Теперь он доступен всем пользователям.",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )


# ── Рассылка всем пользователям ───────────────────────────────────────────────

@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast(query: CallbackQuery, state: FSMContext, user: User):
    if not is_admin(user):
        await query.answer("Нет доступа.", show_alert=True)
        return

    await query.message.edit_text(
        "📢 *Рассылка всем пользователям*\n\n"
        "Напишите сообщение которое получат ВСЕ пользователи бота.\n\n"
        "⚠️ Поддерживаются: текст, фото с подписью, Markdown-форматирование.",
        reply_markup=kb_cancel(),
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastState.message)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext, user: User):
    if not is_admin(user):
        return
    await message.answer(
        "📢 *Рассылка всем пользователям*\n\n"
        "Напишите сообщение которое получат ВСЕ пользователи бота.\n\n"
        "⚠️ Поддерживаются: текст, фото с подписью.",
        reply_markup=kb_cancel(),
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastState.message)


@router.message(BroadcastState.message)
async def got_broadcast_message(message: Message, state: FSMContext):
    """Получили сообщение для рассылки — просим подтвердить."""
    # Сохраняем message_id чтобы потом его переслать
    await state.update_data(
        broadcast_from_chat=message.chat.id,
        broadcast_message_id=message.message_id,
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разослать всем", callback_data="admin:broadcast:confirm")],
        [InlineKeyboardButton(text="❌ Отмена",         callback_data="menu:new")],
    ])

    await message.answer(
        "👆 *Сообщение выше будет разослано всем пользователям.*\n\n"
        "Подтвердите отправку:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastState.confirm)


@router.callback_query(BroadcastState.confirm, F.data == "admin:broadcast:confirm")
async def confirm_broadcast(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    if not is_admin(user):
        return

    data = await state.get_data()
    from_chat = data.get("broadcast_from_chat")
    msg_id    = data.get("broadcast_message_id")
    await state.clear()

    # Получаем всех незаблокированных пользователей
    result = await db.execute(
        select(User).where(User.is_blocked == False)
    )
    all_users = result.scalars().all()

    await query.message.edit_text(
        f"📢 Начинаю рассылку *{len(all_users)}* пользователям...",
        parse_mode="Markdown"
    )

    sent = 0
    failed = 0
    for u in all_users:
        if u.id == user.id:
            continue  # не слать самому себе
        try:
            await query.bot.forward_message(
                chat_id=u.id,
                from_chat_id=from_chat,
                message_id=msg_id,
            )
            sent += 1
        except Exception:
            failed += 1

    await query.message.edit_text(
        f"✅ *Рассылка завершена*\n\n"
        f"✉️ Отправлено: *{sent}*\n"
        f"❌ Ошибок: *{failed}*\n\n"
        f"(Ошибки обычно = пользователь заблокировал бота)",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )
    log.info("Broadcast от %d: sent=%d failed=%d", user.id, sent, failed)
