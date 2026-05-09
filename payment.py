"""
bot/handlers/payment.py — обработка оплаты подписки.

Способы оплаты:
  1. Telegram Stars
  2. Купить у администратора (@jstaskmebro)
"""
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy.ext.asyncio import AsyncSession

from config import SUBSCRIPTION_PRICES, OWNER_ID
from models import User
from services import payment_service
from bot.keyboards import kb_subscription_plans, kb_back_to_menu

log = logging.getLogger(__name__)
router = Router()

# Флаг: это зеркало или главный бот (устанавливается при запуске)
IS_MIRROR = False

ADMIN_USERNAME = "@jstaskmebro"


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
    """Выбрали тариф → показываем способы оплаты (только Stars + у админа)."""
    plan = query.data.split(":")[2]

    if IS_MIRROR:
        await query.answer("Оплата доступна только в главном боте.", show_alert=True)
        return

    info = SUBSCRIPTION_PRICES[plan]
    plan_names = {"1month": "1 месяц", "1week": "1 неделя", "6month": "6 месяцев"}
    text = (
        f"🛒 *{plan_names[plan]}*\n\n"
        f"⭐ Telegram Stars: {info['stars']}\n\n"
        "Выберите способ оплаты:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Оплатить Stars ({info['stars']}⭐)", callback_data=f"pay:stars:{plan}")],
        [InlineKeyboardButton(text="🛒 Купить у администратора", callback_data=f"pay:admin:{plan}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="pay:menu")],
    ])

    await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


# ── Telegram Stars ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay:stars:"))
async def pay_stars(query: CallbackQuery, user: User, db: AsyncSession):
    """Создать инвойс в Telegram Stars."""
    if IS_MIRROR:
        await query.answer("Оплата только в главном боте.", show_alert=True)
        return

    plan = query.data.split(":")[2]
    price = payment_service.get_stars_price(plan)
    plan_names = {"1month": "1 месяц", "1week": "1 неделя", "6month": "6 месяцев"}

    payment = await payment_service.create_payment(db, user.id, "stars", plan)

    await query.message.answer_invoice(
        title=f"Подписка {plan_names[plan]}",
        description="Доступ к сервису рассылок",
        payload=f"stars:{payment.id}",
        currency="XTR",
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
    payment_id = int(payload.split(":")[1])

    from sqlalchemy import select
    from models import Payment
    result = await db.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()

    if payment and payment.status == "pending":
        await payment_service.confirm_payment(db, payment, user)

        plan_names = {"1month": "1 месяц", "1week": "1 неделя", "6month": "6 месяцев"}
        plan_label = plan_names.get(payment.plan, payment.plan)
        stars_amount = payment.amount

        await message.answer(
            f"✅ *Оплата прошла!*\n\n{user.subscription_status}",
            reply_markup=kb_back_to_menu(),
            parse_mode="Markdown"
        )

        # Уведомляем администратора
        try:
            await message.bot.send_message(
                OWNER_ID,
                f"💰 *Новая оплата через Stars!*\n\n"
                f"👤 Пользователь: {user.full_name}\n"
                f"🆔 ID: `{user.id}`\n"
                f"👤 Username: @{user.username or '—'}\n\n"
                f"📦 Тариф: *{plan_label}*\n"
                f"⭐ Сумма: *{int(stars_amount)} Stars*\n\n"
                f"{user.subscription_status}",
                parse_mode="Markdown"
            )
        except Exception as e:
            log.warning("Не удалось уведомить админа о покупке: %s", e)
    else:
        await message.answer("⚠️ Платёж не найден, обратитесь к поддержке.")


# ── Купить у администратора ───────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay:admin:"))
async def pay_admin(query: CallbackQuery, user: User):
    """Показать контакт администратора для ручной покупки."""
    if IS_MIRROR:
        await query.answer("Оплата только в главном боте.", show_alert=True)
        return

    plan = query.data.split(":")[2]
    plan_names = {"1month": "1 месяц", "1week": "1 неделя", "6month": "6 месяцев"}
    plan_label = plan_names.get(plan, plan)
    info = SUBSCRIPTION_PRICES[plan]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✉️ Написать администратору", url=f"https://t.me/jstaskmebro")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"pay:select:{plan}")],
    ])

    await query.message.edit_text(
        f"🛒 *Покупка у администратора*\n\n"
        f"Тариф: *{plan_label}*\n"
        f"Стоимость: *{info['stars']}⭐ Stars*\n\n"
        f"Напишите администратору {ADMIN_USERNAME} и укажите:\n"
        f"• Ваш Telegram ID: `{query.from_user.id}`\n"
        f"• Тариф: {plan_label}\n\n"
        f"Администратор активирует подписку вручную.",
        reply_markup=kb,
        parse_mode="Markdown"
    )
