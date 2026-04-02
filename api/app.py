from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, Request, HTTPException
from sqlalchemy import select

from config import API_SECRET, CRYPTOBOT_WEBHOOK_SECRET
from database import SessionLocal
from models import Payment
from services.payment_service import confirm_payment
from services.user_service import add_subscription

logger = logging.getLogger(__name__)

app = FastAPI(title="TG SaaS API")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/cryptobot")
async def cryptobot_webhook(request: Request) -> dict:
    body = await request.body()
    signature = request.headers.get("crypto-pay-api-signature", "")

    if CRYPTOBOT_WEBHOOK_SECRET:
        secret_hash = hmac.new(
            CRYPTOBOT_WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(secret_hash, signature):
            raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        data = json.loads(body)
        update_type = data.get("update_type")
        if update_type == "invoice_paid":
            payload = data["payload"]["payload"]
            user_id_str, plan = payload.split(":")
            user_id = int(user_id_str)

            async with SessionLocal() as db:
                result = await db.execute(
                    select(Payment).where(
                        Payment.user_id == user_id,
                        Payment.method == "cryptobot",
                        Payment.plan == plan,
                        Payment.status == "pending",
                    )
                )
                payment = result.scalar_one_or_none()
                if payment:
                    await confirm_payment(db, payment.id)
                    logger.info(f"CryptoBot payment confirmed for user {user_id}, plan {plan}")
    except Exception as e:
        logger.error(f"Webhook error: {e}")

    return {"ok": True}
