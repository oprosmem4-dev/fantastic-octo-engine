"""
api/main.py — FastAPI backend.

Обрабатывает:
  - Webhook от CryptoBot (подтверждение оплаты)
  - Внутренние endpoint-ы для управления (опционально)
"""
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, HTTPException, Header
from sqlalchemy import select

from config import CRYPTOBOT_WEBHOOK_SECRET, API_HOST, API_PORT, SUBSCRIPTION_PRICES
from database import SessionLocal, create_all_tables
from models import Payment
from services.user_service import get_user
from services.payment_service import confirm_payment

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Запускается при старте FastAPI — создаём таблицы."""
    await create_all_tables()
    log.info("FastAPI запущен.")
    yield


app = FastAPI(title="TG SaaS Backend", lifespan=lifespan)


# ── CryptoBot Webhook ─────────────────────────────────────────────────────────

@app.post("/webhook/cryptobot")
async def cryptobot_webhook(request: Request):
    """
    Webhook от CryptoBot — вызывается когда пользователь оплатил.
    CryptoBot отправляет POST-запрос с подписью в заголовке.
    """
    body = await request.body()

    # Проверяем подпись запроса (защита от фейковых вебхуков)
    signature = request.headers.get("crypto-pay-api-signature", "")
    expected  = hmac.new(
        CRYPTOBOT_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    if signature != expected:
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = await request.json()
    log.info("CryptoBot webhook: %s", data)

    # Обрабатываем только оплаченные инвойсы
    if data.get("update_type") != "invoice_paid":
        return {"ok": True}

    payload    = data["payload"]["payload"]       # "sub_{plan}_{user_id}"
    invoice_id = str(data["payload"]["invoice_id"])

    # Разбираем payload
    try:
        _, plan, user_id_str = payload.split("_", 2)
        user_id = int(user_id_str)
    except ValueError:
        log.error("Неверный payload: %s", payload)
        return {"ok": True}

    async with SessionLocal() as db:
        # Ищем платёж по external_id
        result = await db.execute(
            select(Payment).where(
                Payment.external_id == invoice_id,
                Payment.status == "pending",
            )
        )
        payment = result.scalar_one_or_none()

        if not payment:
            log.warning("Платёж не найден: invoice_id=%s", invoice_id)
            return {"ok": True}

        user = await get_user(db, user_id)
        if not user:
            log.warning("Пользователь не найден: %d", user_id)
            return {"ok": True}

        await confirm_payment(db, payment, user)
        log.info("CryptoBot оплата подтверждена: user=%d plan=%s", user_id, plan)

    return {"ok": True}


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Точка входа ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [API] %(levelname)s: %(message)s"
    )
    uvicorn.run(app, host=API_HOST, port=API_PORT)
