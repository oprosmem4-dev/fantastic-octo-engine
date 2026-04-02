"""
bot/handlers/payment.py — обработка оплаты подписки.

Способы оплаты:
  1. Telegram Stars (только главный бот)
  2. CryptoBot (инвойс USDT)
  3. TON (перевод на кошелёк с комментарием)
"""
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    LabeledPrice, PreCheckoutQuery
)
from sqlalchemy.ext.asyncio import AsyncSession

from config import SUBSCRIPTION_PRICES, TON_WALLET, MAIN_BOT_LINK
from models import User
from services import payment_service
from services.user_service import get_user
from bot.keyboards import kb_subscription_plans, kb_payment_methods, kb_back_to_menu

log = logging.getLogger(__name__)
router = Router()

# Флаг: это зеркало или главный бот (устанавливается при запуске)
IS_MIRROR = False


# ── Меню подписки ─────────────────────────────────────────────────────────────

@router.message(Command("pay"))
@router.callback_query(F.data == "pay:menu")
async def show_pay_menu(event, user: User):
    text = (
        f"💳 *Подписка*\n\n"
        f"{user.subscription_status}\n\n"
        "Выберите тариф:"
    )
    kb = kb_subscription_plans(is_mirror=IS_MIRROR)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("pay:select:"))
async def select_plan(query: CallbackQuery):
    """Выбрали тариф → показываем способы оплаты."""
    plan = query.data.split(":")[2]

    if IS_MIRROR:
        await query.answer("Оплата доступна только в главном боте.", show_alert=True)
        return

    info = SUBSCRIPTION_PRICES[plan]
    plan_names = {"1month": "1 месяц", "3month": "3 месяца", "6month": "6 месяцев"}
    text = (
        f"🛒 *{plan_names[plan]}*\n\n"
        f"⭐ Stars: {info['stars']}\n"
        f"💰 USDT: {info['usdt']}$\n"
        f"💎 TON: по курсу\n\n"
        "Выберите способ оплаты:"
    )
    await query.message.edit_text(text, reply_markup=kb_payment_methods(plan), parse_mode="Markdown")


# ── Telegram Stars ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay:stars:"))
async def pay_stars(query: CallbackQuery, user: User, db: AsyncSession):
    """Создать инвойс в Telegram Stars."""
    if IS_MIRROR:
        await query.answer("Оплата только в главном боте.", show_alert=True)
        return

    plan = query.data.split(":")[2]
    price = payment_service.get_stars_price(plan)
    plan_names = {"1month": "1 месяц", "3month": "3 месяца", "6month": "6 месяцев"}

    # Сохраняем pending-платёж
    payment = await payment_service.create_payment(db, user.id, "stars", plan)

    await query.message.answer_invoice(
        title=f"Подписка {plan_names[plan]}",
        description="Доступ к сервису рассылок",
        payload=f"stars:{payment.id}",
        currency="XTR",          # XTR = Telegram Stars
        prices=[LabeledPrice(label="Подписка", amount=price)],
    )
    await query.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    """Обязательное подтверждение перед оплатой Stars."""
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, user: User, db: AsyncSession):
    """Оплата Stars прошла успешно."""
    payload = message.successful_payment.invoice_payload
    # payload = "stars:{payment_id}"
    payment_id = int(payload.split(":")[1])

    from sqlalchemy import select
    from models import Payment
    result = await db.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()

    if payment and payment.status == "pending":
        await payment_service.confirm_payment(db, payment, user)
        await message.answer(
            f"✅ *Оплата прошла!*\n\n{user.subscription_status}",
            reply_markup=kb_back_to_menu(),
            parse_mode="Markdown"
        )
    else:
        await message.answer("⚠️ Платёж не найден, обратитесь к поддержке.")


# ── CryptoBot ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay:crypto:"))
async def pay_crypto(query: CallbackQuery, user: User, db: AsyncSession):
    """Создать инвойс через CryptoBot."""
    if IS_MIRROR:
        await query.answer("Оплата только в главном боте.", show_alert=True)
        return

    plan = query.data.split(":")[2]
    result = await payment_service.create_cryptobot_invoice(plan, user.id)

    if not result:
        await query.answer("❌ CryptoBot недоступен. Попробуйте другой способ.", show_alert=True)
        return

    # Сохраняем платёж с external_id
    await payment_service.create_payment(db, user.id, "cryptobot", plan, result["invoice_id"])

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💰 Оплатить через CryptoBot", url=result["pay_url"])
    ], [
        InlineKeyboardButton(text="◀️ Назад", callback_data="pay:menu")
    ]])

    price_info = SUBSCRIPTION_PRICES[plan]
    await query.message.edit_text(
        f"💰 *Оплата через CryptoBot*\n\n"
        f"Сумма: *{price_info['usdt']} USDT*\n\n"
        "Нажмите кнопку ниже для оплаты:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


# ── TON ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay:ton:"))
async def pay_ton(query: CallbackQuery, user: User, db: AsyncSession):
    """Показать инструкцию оплаты TON."""
    if IS_MIRROR:
        await query.answer("Оплата только в главном боте.", show_alert=True)
        return

    plan = query.data.split(":")[2]
    # Уникальный комментарий = user_id + plan
    comment = f"sub_{user.id}_{plan}"

    # Создаём pending-платёж
    await payment_service.create_payment(db, user.id, "ton", plan, comment)

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"pay:ton_check:{comment}")
    ], [
        InlineKeyboardButton(text="◀️ Назад", callback_data="pay:menu")
    ]])

    await query.message.edit_text(
        f"💎 *Оплата TON*\n\n"
        f"Отправьте TON на адрес:\n"
        f"`{TON_WALLET}`\n\n"
        f"⚠️ *Обязательный комментарий:*\n"
        f"`{comment}`\n\n"
        f"Без комментария оплата не будет засчитана!\n\n"
        f"После отправки нажмите «Я оплатил».",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("pay:ton_check:"))
async def ton_check(query: CallbackQuery, user: User, db: AsyncSession):
    """Проверка TON транзакции (упрощённая версия — ручная проверка)."""
    # В полной реализации здесь был бы запрос к TON API
    # Пока — уведомляем владельца для ручной проверки
    from config import OWNER_ID
    comment = query.data.split(":", 2)[2]
    try:
        await query.bot.send_message(
            OWNER_ID,
            f"💎 *TON-оплата на проверке*\n\n"
            f"Пользователь: `{user.id}` @{user.username or '—'}\n"
            f"Комментарий: `{comment}`\n\n"
            f"Проверьте транзакцию и выдайте подписку через /admin",
            parse_mode="Markdown"
        )
    except Exception:
        pass
    await query.answer("✅ Запрос отправлен. Мы проверим и активируем подписку.", show_alert=True)
