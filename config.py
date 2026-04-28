"""
config.py — все настройки проекта, читаются из .env файла.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram бот ──────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
OWNER_ID    = int(os.environ["OWNER_ID"])

# ── База данных ───────────────────────────────────────────────────────────────
DATABASE_URL = os.environ["DATABASE_URL"]

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── CryptoBot ─────────────────────────────────────────────────────────────────
CRYPTOBOT_TOKEN  = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_WEBHOOK_SECRET = os.getenv("CRYPTOBOT_WEBHOOK_SECRET", "")

# ── TON кошелёк ───────────────────────────────────────────────────────────────
TON_WALLET = os.getenv("TON_WALLET", "")

# ── Тарифы подписки ───────────────────────────────────────────────────────────
SUBSCRIPTION_PRICES = {
    "1month": {"stars": 100, "usdt": 5.0,  "days": 30},
    "3month": {"stars": 250, "usdt": 12.0, "days": 90},
    "6month": {"stars": 450, "usdt": 20.0, "days": 180},
}

# ── Лимиты ────────────────────────────────────────────────────────────────────
MAX_CHATS_PER_USER = 100
TRIAL_DAYS         = 1

# ── Ссылка на главного бота (для зеркал) ──────────────────────────────────────
MAIN_BOT_LINK = os.getenv("MAIN_BOT_LINK", "https://t.me/your_main_bot")

# ── FastAPI ───────────────────────────────────────────────────────────────────
API_HOST   = os.getenv("API_HOST", "0.0.0.0")
API_PORT   = int(os.getenv("API_PORT", "8000"))
API_SECRET = os.environ["API_SECRET"]

# ── Проверка спамблока ────────────────────────────────────────────────────────
# Ваш личный @username (без @) или любой надёжный аккаунт, которому можно
# отправить тестовое сообщение. Если аккаунт в спамблоке — отправка провалится.
# Например: SPAMCHECK_USERNAME=my_personal_account
SPAMCHECK_USERNAME = os.getenv("SPAMCHECK_USERNAME", "")
