import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/tgsaas")
API_SECRET: str = os.getenv("API_SECRET", "")

CRYPTOBOT_TOKEN: str = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_WEBHOOK_SECRET: str = os.getenv("CRYPTOBOT_WEBHOOK_SECRET", "")
TON_WALLET: str = os.getenv("TON_WALLET", "")
MAIN_BOT_LINK: str = os.getenv("MAIN_BOT_LINK", "https://t.me/your_bot")
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = int(os.getenv("API_PORT", "8000"))

SUBSCRIPTION_PRICES = {
    "1week":  {"stars": 30,  "usdt": 1.5,  "days": 7},
    "1month": {"stars": 100, "usdt": 5.0,  "days": 30},
    "3month": {"stars": 250, "usdt": 12.0, "days": 90},
    "6month": {"stars": 450, "usdt": 20.0, "days": 180},
}

MAX_CHATS_PER_USER = 100
MAX_CHATS_PER_ACCOUNT = 35
TRIAL_DAYS = 1
