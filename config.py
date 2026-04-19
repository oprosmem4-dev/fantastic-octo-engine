"""
config.py — все настройки проекта, читаются из .env файла.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram бот ──────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]       # токен главного бота
OWNER_ID    = int(os.environ["OWNER_ID"])   # Telegram ID владельца

# ── База данных ───────────────────────────────────────────────────────────────
DATABASE_URL = os.environ["DATABASE_URL"]
# пример: postgresql+asyncpg://user:pass@localhost:5432/tgsaas

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── CryptoBot (для оплаты крипто) ─────────────────────────────────────────────
CRYPTOBOT_TOKEN  = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_WEBHOOK_SECRET = os.getenv("CRYPTOBOT_WEBHOOK_SECRET", "")

# ── TON кошелёк (для оплаты TON) ──────────────────────────────────────────────
TON_WALLET = os.getenv("TON_WALLET", "")    # адрес вашего TON кошелька

# ── Тарифы подписки ───────────────────────────────────────────────────────────
# Стоимость в Stars и крипто (USDT)
SUBSCRIPTION_PRICES = {
    "1month": {"stars": 100, "usdt": 5.0,  "days": 30},
    "3month": {"stars": 250, "usdt": 12.0, "days": 90},
    "6month": {"stars": 450, "usdt": 20.0, "days": 180},
}

# ── Лимиты по умолчанию ───────────────────────────────────────────────────────
MAX_CHATS_PER_USER    = 100   # максимум чатов на пользователя
# MAX_CHATS_PER_ACCOUNT удалён — нет ограничения на количество чатов на аккаунт
TRIAL_DAYS            = 1     # сколько дней пробного периода

# ── Ссылка на главного бота (для зеркал) ─────────────────────────────────────
MAIN_BOT_LINK = os.getenv("MAIN_BOT_LINK", "https://t.me/your_main_bot")

# ── FastAPI ───────────────────────────────────────────────────────────────────
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
API_SECRET = os.environ["API_SECRET"]  # секрет для внутренних запросов
