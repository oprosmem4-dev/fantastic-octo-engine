"""
config.py — все настройки проекта, читаются из .env файла.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram боты ─────────────────────────────────────────────────────────────
BOT_TOKEN          = os.environ["BOT_TOKEN"]           # токен главного бота (для воркера)
GENERATOR_BOT_TOKEN = os.environ["GENERATOR_BOT_TOKEN"] # токен бота-регистратора зеркал
OWNER_ID           = int(os.environ["OWNER_ID"])

# ── База данных ───────────────────────────────────────────────────────────────
DATABASE_URL = os.environ["DATABASE_URL"]

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── CryptoBot ─────────────────────────────────────────────────────────────────
CRYPTOBOT_TOKEN          = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_WEBHOOK_SECRET = os.getenv("CRYPTOBOT_WEBHOOK_SECRET", "")

# ── TON кошелёк ───────────────────────────────────────────────────────────────
TON_WALLET = os.getenv("TON_WALLET", "")

# ── Тарифы подписки ───────────────────────────────────────────────────────────
SUBSCRIPTION_PRICES = {
    "1week":  {"stars": 50,  "usdt": 1.0,  "days": 7},
    "1month": {"stars": 150, "usdt": 3.0,  "days": 30},
    "6month": {"stars": 450, "usdt": 20.0, "days": 180},
}

# ── Лимиты ────────────────────────────────────────────────────────────────────
MAX_CHATS_PER_USER = 100
TRIAL_DAYS         = 3

# ── Ссылка на бота-регистратора (для зеркал — куда слать оплату) ──────────────
# Теперь MAIN_BOT_LINK ведёт на generator бот, т.к. именно там регистрация.
# Если нужна отдельная ссылка для оплаты — задайте PAYMENT_BOT_LINK.
MAIN_BOT_LINK  = os.getenv("MAIN_BOT_LINK", "https://t.me/your_generator_bot")
PAYMENT_BOT_LINK = os.getenv("PAYMENT_BOT_LINK", MAIN_BOT_LINK)

# ── FastAPI ───────────────────────────────────────────────────────────────────
API_HOST   = os.getenv("API_HOST", "0.0.0.0")
API_PORT   = int(os.getenv("API_PORT", "8000"))
API_SECRET = os.environ["API_SECRET"]

# ── Проверка спамблока ────────────────────────────────────────────────────────
SPAMCHECK_USERNAME = os.getenv("SPAMCHECK_USERNAME", "")

# ── Mirror runner: шардирование (опционально) ─────────────────────────────────
# Позволяет запустить несколько mirror_runner на разных серверах,
# разделив ответственность по user_id.
# Пример: сервер 1: SHARD_MIN=0, SHARD_MAX=500000
#          сервер 2: SHARD_MIN=500000, SHARD_MAX=None
_shard_min = os.getenv("MIRROR_SHARD_MIN")
_shard_max = os.getenv("MIRROR_SHARD_MAX")
MIRROR_SHARD_MIN: int | None = int(_shard_min) if _shard_min else None
MIRROR_SHARD_MAX: int | None = int(_shard_max) if _shard_max else None

# ── Mirror runner: задержка перезапуска при падении зеркала ──────────────────
MIRROR_RESTART_DELAY = int(os.getenv("MIRROR_RESTART_DELAY", "10"))  # секунды
