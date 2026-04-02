"""
models/__init__.py — все модели базы данных в одном месте.
Импортируй так: from models import User, Account, Task, ...
"""

from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey,
    Integer, String, Text, Enum as SAEnum
)
from sqlalchemy.orm import relationship, Mapped, mapped_column

from database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# ПОЛЬЗОВАТЕЛИ
# ─────────────────────────────────────────────────────────────────────────────

class User(Base):
    """Пользователь бота."""
    __tablename__ = "users"

    id: Mapped[int]          = mapped_column(BigInteger, primary_key=True)  # Telegram ID
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str]   = mapped_column(String(128), default="")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool]   = mapped_column(Boolean, default=False)

    # Подписка
    trial_ends_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True))
    sub_ends_at: Mapped[datetime | None]    = mapped_column(DateTime(timezone=True))

    # Лимиты (можно увеличить вручную через админку)
    max_chats: Mapped[int] = mapped_column(Integer, default=100)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    # Связи
    accounts: Mapped[list["Account"]] = relationship(back_populates="owner", lazy="selectin")
    tasks: Mapped[list["Task"]]       = relationship(back_populates="user",  lazy="selectin")
    mirror_bot: Mapped["MirrorBot | None"] = relationship(back_populates="user", uselist=False)
    payments: Mapped[list["Payment"]] = relationship(back_populates="user",  lazy="selectin")

    @property
    def has_access(self) -> bool:
        """True если trial или подписка активны."""
        now = now_utc()
        trial_ok = self.trial_ends_at and self.trial_ends_at > now
        sub_ok   = self.sub_ends_at and self.sub_ends_at > now
        return bool(trial_ok or sub_ok)

    @property
    def subscription_status(self) -> str:
        """Удобная строка статуса."""
        now = now_utc()
        if self.sub_ends_at and self.sub_ends_at > now:
            days = (self.sub_ends_at - now).days
            return f"✅ Подписка: {days} дн."
        if self.trial_ends_at and self.trial_ends_at > now:
            hours = int((self.trial_ends_at - now).total_seconds() / 3600)
            return f"🎁 Триал: {hours} ч."
        return "❌ Нет доступа"


# ─────────────────────────────────────────────────────────────────────────────
# ЗЕРКАЛЬНЫЕ БОТЫ
# ─────────────────────────────────────────────────────────────────────────────

class MirrorBot(Base):
    """Бот-зеркало — пользователь может добавить свой bot_token."""
    __tablename__ = "mirror_bots"

    id: Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int]      = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    token: Mapped[str]        = mapped_column(String(120), unique=True)
    bot_username: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool]   = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    user: Mapped["User"] = relationship(back_populates="mirror_bot")


# ─────────────────────────────────────────────────────────────────────────────
# АККАУНТЫ (Telethon userbots)
# ─────────────────────────────────────────────────────────────────────────────

class Account(Base):
    """Telegram-аккаунт для рассылок (добавляется через Telethon)."""
    __tablename__ = "accounts"

    id: Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    # owner_id = None означает системный аккаунт (добавил администратор)

    phone: Mapped[str]        = mapped_column(String(32), unique=True)
    api_id: Mapped[int]       = mapped_column(Integer)
    api_hash: Mapped[str]     = mapped_column(String(64))
    session_string: Mapped[str | None] = mapped_column(Text)  # StringSession от Telethon

    is_active: Mapped[bool]   = mapped_column(Boolean, default=True)
    is_banned: Mapped[bool]   = mapped_column(Boolean, default=False)
    is_system: Mapped[bool]   = mapped_column(Boolean, default=False)  # системный аккаунт

    # Статистика нагрузки
    chats_count: Mapped[int]  = mapped_column(Integer, default=0)  # текущее число чатов

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    owner: Mapped["User | None"] = relationship(back_populates="accounts")
    task_links: Mapped[list["TaskAccount"]] = relationship(back_populates="account")


# ─────────────────────────────────────────────────────────────────────────────
# ЗАДАЧИ РАССЫЛОК
# ─────────────────────────────────────────────────────────────────────────────

class Task(Base):
    """Задача на рассылку — пользователь создаёт, воркер выполняет."""
    __tablename__ = "tasks"

    id: Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int]      = mapped_column(BigInteger, ForeignKey("users.id"))
    name: Mapped[str]         = mapped_column(String(128), default="Задача")
    message: Mapped[str]      = mapped_column(Text)          # текст для отправки
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    is_active: Mapped[bool]   = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"]              = relationship(back_populates="tasks")
    chats: Mapped[list["TaskChat"]]   = relationship(back_populates="task",   cascade="all, delete")
    accounts: Mapped[list["TaskAccount"]] = relationship(back_populates="task", cascade="all, delete")


class TaskChat(Base):
    """Один чат в задаче рассылки."""
    __tablename__ = "task_chats"

    id: Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int]      = mapped_column(Integer, ForeignKey("tasks.id"))
    chat_id: Mapped[str]      = mapped_column(String(64))     # например "-1001234567890"
    chat_title: Mapped[str]   = mapped_column(String(128), default="")
    is_ok: Mapped[bool]       = mapped_column(Boolean, default=True)  # False если бан/нет доступа

    task: Mapped["Task"] = relationship(back_populates="chats")


class TaskAccount(Base):
    """Связь задача ↔ аккаунт (какой аккаунт рассылает в какие чаты этой задачи)."""
    __tablename__ = "task_accounts"

    id: Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"))
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"))
    chat_ids: Mapped[str] = mapped_column(Text, default="")  # JSON список чатов

    task: Mapped["Task"]       = relationship(back_populates="accounts")
    account: Mapped["Account"] = relationship(back_populates="task_links")


# ─────────────────────────────────────────────────────────────────────────────
# ПЛАТЕЖИ
# ─────────────────────────────────────────────────────────────────────────────

class Payment(Base):
    """Запись о платеже."""
    __tablename__ = "payments"

    id: Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int]     = mapped_column(BigInteger, ForeignKey("users.id"))
    method: Mapped[str]      = mapped_column(String(32))  # "stars" | "cryptobot" | "ton"
    plan: Mapped[str]        = mapped_column(String(32))  # "1month" | "3month" | "6month"
    amount: Mapped[float]    = mapped_column(Float)
    currency: Mapped[str]    = mapped_column(String(16))  # "XTR" | "USDT" | "TON"
    status: Mapped[str]      = mapped_column(String(16), default="pending")  # pending|paid|failed
    external_id: Mapped[str | None] = mapped_column(String(256))  # ID в платёжной системе
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="payments")


# ─────────────────────────────────────────────────────────────────────────────
# ЛОГИ
# ─────────────────────────────────────────────────────────────────────────────

class Log(Base):
    """Лог отправок — что, куда, когда, результат."""
    __tablename__ = "logs"

    id: Mapped[int]         = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=True)
    chat_id: Mapped[str]    = mapped_column(String(64))
    success: Mapped[bool]   = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
