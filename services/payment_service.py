from __future__ import annotations

from datetime import datetime, timezone

import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import CRYPTOBOT_TOKEN, SUBSCRIPTION_PRICES
from models import Payment
from services.user_service import add_subscription


async def create_payment(
    db: AsyncSession,
    user_id: int,
    method: str,
    plan: str,
) -> Payment:
    price_info = SUBSCRIPTION_PRICES[plan]
    if method == "stars":
        amount = float(price_info["stars"])
        currency = "XTR"
    else:
        amount = float(price_info["usdt"])
        currency = "USDT"

    payment = Payment(
        user_id=user_id,
        method=method,
        plan=plan,
        amount=amount,
        currency=currency,
        status="pending",
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return payment


async def confirm_payment(db: AsyncSession, payment_id: int) -> Payment:
    result = await db.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise ValueError(f"Payment {payment_id} not found")
    payment.status = "paid"
    payment.paid_at = datetime.now(timezone.utc)
    await db.commit()

    days = SUBSCRIPTION_PRICES[payment.plan]["days"]
    await add_subscription(db, payment.user_id, days)
    return payment


async def cryptobot_invoice(plan: str, user_id: int) -> dict:
    """Create a CryptoBot invoice and return the invoice data."""
    price_info = SUBSCRIPTION_PRICES[plan]
    amount = price_info["usdt"]

    async with aiohttp.ClientSession() as session:
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        payload = {
            "asset": "USDT",
            "amount": str(amount),
            "description": f"TG SaaS subscription — {plan}",
            "payload": f"{user_id}:{plan}",
            "allow_anonymous": False,
        }
        async with session.post(
            "https://pay.crypt.bot/api/createInvoice",
            json=payload,
            headers=headers,
        ) as resp:
            data = await resp.json()
            return data.get("result", {})
