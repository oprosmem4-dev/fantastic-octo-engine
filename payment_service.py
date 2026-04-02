"""
services/payment_service.py — обработка платежей.
Поддерживаемые методы: Telegram Stars, CryptoBot, TON (по комментарию).
"""
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from config import SUBSCRIPTION_PRICES, CRYPTOBOT_TOKEN
from models import Payment, User
from services.user_service import add_subscription

log = logging.getLogger(__name__)


async def create_payment(
    db: AsyncSession,
    user_id: int,
    method: str,
    plan: str,
    external_id: str | None = None,
) -> Payment:
    """Создать запись платежа со статусом 'pending'."""
    price_info = SUBSCRIPTION_PRICES[plan]
    amount   = price_info["stars"] if method == "stars" else price_info["usdt"]
    currency = "XTR" if method == "stars" else ("TON" if method == "ton" else "USDT")

    payment = Payment(
        user_id=user_id,
        method=method,
        plan=plan,
        amount=amount,
        currency=currency,
        status="pending",
        external_id=external_id,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return payment


async def confirm_payment(db: AsyncSession, payment: Payment, user: User):
    """
    Подтвердить платёж — добавить подписку пользователю.
    Вызывается из webhook-обработчиков.
    """
    payment.status = "paid"
    payment.paid_at = datetime.now(timezone.utc)
    await db.commit()

    days = SUBSCRIPTION_PRICES[payment.plan]["days"]
    await add_subscription(db, user, days)
    log.info(
        "Платёж подтверждён: user=%d plan=%s days=%d",
        user.id, payment.plan, days
    )


# ── CryptoBot ─────────────────────────────────────────────────────────────────

async def create_cryptobot_invoice(plan: str, user_id: int) -> dict | None:
    """
    Создать инвойс через CryptoBot API.
    Возвращает словарь с полями: pay_url, invoice_id.
    """
    if not CRYPTOBOT_TOKEN:
        return None
    try:
        import aiohttp
        price_info = SUBSCRIPTION_PRICES[plan]
        payload = f"sub_{plan}_{user_id}"
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                "https://pay.crypt.bot/api/createInvoice",
                headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                json={
                    "asset": "USDT",
                    "amount": str(price_info["usdt"]),
                    "description": f"Подписка {plan}",
                    "payload": payload,
                    "expires_in": 3600,  # 1 час
                }
            )
            data = await resp.json()
        if data.get("ok"):
            return {
                "pay_url":    data["result"]["pay_url"],
                "invoice_id": str(data["result"]["invoice_id"]),
            }
    except Exception as e:
        log.error("CryptoBot ошибка: %s", e)
    return None


# ── Telegram Stars ────────────────────────────────────────────────────────────

def get_stars_price(plan: str) -> int:
    """Получить цену в Stars для плана."""
    return SUBSCRIPTION_PRICES[plan]["stars"]
