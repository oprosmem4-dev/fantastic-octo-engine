from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import payment_methods_kb, payment_plans_kb, cancel_kb
from config import SUBSCRIPTION_PRICES, TON_WALLET, MAIN_BOT_LINK
from models import User
from services import payment_service

router = Router()


@router.message(Command("pay"))
@router.callback_query(F.data == "menu:pay")
async def show_payment_methods(event: Message | CallbackQuery, user: User, db: AsyncSession) -> None:
    text = (
        "💳 Выберите способ оплаты:\n\n"
        "⭐ Telegram Stars — мгновенно\n"
        "💎 CryptoBot — USDT\n"
        "💎 TON — ручная проверка"
    )
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=payment_methods_kb())
        await event.answer()
    else:
        await event.answer(text, reply_markup=payment_methods_kb())


@router.callback_query(F.data.startswith("pay_method:"))
async def select_plan(callback: CallbackQuery) -> None:
    method = callback.data.split(":")[1]
    lines = ["💳 Выберите тариф:\n"]
    for plan, info in SUBSCRIPTION_PRICES.items():
        if method == "stars":
            price = f"{info['stars']} ⭐"
        else:
            price = f"${info['usdt']}"
        lines.append(f"• {plan}: {price} ({info['days']} дней)")

    await callback.message.edit_text("\n".join(lines), reply_markup=payment_plans_kb(method))
    await callback.answer()


@router.callback_query(F.data.startswith("pay:stars:"))
async def pay_stars(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    plan = callback.data.split(":")[2]
    info = SUBSCRIPTION_PRICES[plan]
    stars = info["stars"]

    payment = await payment_service.create_payment(db, user.id, "stars", plan)

    prices = [LabeledPrice(label=f"TG SaaS {plan}", amount=stars)]
    await callback.message.answer_invoice(
        title=f"Подписка TG SaaS — {plan}",
        description=f"{info['days']} дней доступа",
        payload=str(payment.id),
        currency="XTR",
        prices=prices,
    )
    await callback.answer()


@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, db: AsyncSession) -> None:
    payload = message.successful_payment.invoice_payload
    try:
        payment_id = int(payload)
        await payment_service.confirm_payment(db, payment_id)
        await message.answer("✅ Оплата прошла! Подписка активирована.")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка обработки платежа: {e}")


@router.callback_query(F.data.startswith("pay:cryptobot:"))
async def pay_cryptobot(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    plan = callback.data.split(":")[2]
    try:
        invoice = await payment_service.cryptobot_invoice(plan, user.id)
        pay_url = invoice.get("pay_url", "")
        if pay_url:
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            builder = InlineKeyboardBuilder()
            builder.button(text="💎 Оплатить через CryptoBot", url=pay_url)
            builder.button(text="🔙 Меню", callback_data="menu:main")
            builder.adjust(1)
            await callback.message.edit_text(
                f"💎 Оплата через CryptoBot\n\nСумма: ${SUBSCRIPTION_PRICES[plan]['usdt']}",
                reply_markup=builder.as_markup(),
            )
        else:
            await callback.message.edit_text("❌ Ошибка создания инвойса. Попробуйте позже.", reply_markup=cancel_kb())
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}", reply_markup=cancel_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("pay:ton:"))
async def pay_ton(callback: CallbackQuery, user: User) -> None:
    plan = callback.data.split(":")[2]
    info = SUBSCRIPTION_PRICES[plan]
    amount_usdt = info["usdt"]
    text = (
        f"💎 Оплата через TON\n\n"
        f"Отправьте {amount_usdt} USDT на кошелёк:\n"
        f"<code>{TON_WALLET}</code>\n\n"
        f"В комментарии укажите ваш ID: <code>{user.id}</code>\n\n"
        f"После оплаты обратитесь к администратору."
    )
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Меню", callback_data="menu:main")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()
