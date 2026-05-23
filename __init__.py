"""
models/__init__.py — все модели базы данных в одном месте.

ИЗМЕНЕНИЯ:
  - Account: добавлены поля прокси (proxy_host/port/type/user/pass)
  - Account: добавлены поля sends_last_hour, sends_reset_at для rate-limit балансировки
"""

from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey,
    Integer, String, Text,
)
from sqlalchemy.orm import relationship, Mapped, mapped_column

from database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# ПОЛЬЗОВАТЕЛИ
# ─────────────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int]          = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str]   = mapped_column(String(128), default="")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool]   = mapped_column(Boolean, default=False)

    trial_ends_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True))
    sub_ends_at: Mapped[datetime | None]    = mapped_column(DateTime(timezone=True))

    max_chats: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    accounts: Mapped[list["Account"]] = relationship(back_populates="owner", lazy="selectin")
    tasks: Mapped[list["Task"]]       = relationship(back_populates="user",  lazy="selectin")
    mirror_bot: Mapped["MirrorBot | None"] = relationship(back_populates="user", uselist=False)
    payments: Mapped[list["Payment"]] = relationship(back_populates="user",  lazy="selectin")

    @property
    def has_access(self) -> bool:
        now = now_utc()
        trial_ok = self.trial_ends_at and self.trial_ends_at > now
        sub_ok   = self.sub_ends_at and self.sub_ends_at > now
        return bool(trial_ok or sub_ok)

    @property
    def subscription_status(self) -> str:
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
    """
    Telegram-аккаунт для рассылок.

    Поле status:
      "ok"          — работает нормально
      "frozen"      — Telegram деактивировал аккаунт
      "spamblocked" — спамблок

    Прокси:
      proxy_host / proxy_port / proxy_type ("socks5"|"http") / proxy_user / proxy_pass
      Один прокси = не более 5 аккаунтов (контролируется логикой добавления).

    Нагрузка:
      sends_last_hour — количество отправок за текущий час (сбрасывается каждый час)
      sends_reset_at  — когда был последний сброс счётчика
    """
    __tablename__ = "accounts"

    id: Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)

    phone: Mapped[str]        = mapped_column(String(32), unique=True)
    api_id: Mapped[int]       = mapped_column(Integer)
    api_hash: Mapped[str]     = mapped_column(String(64))
    session_string: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool]   = mapped_column(Boolean, default=True)
    is_banned: Mapped[bool]   = mapped_column(Boolean, default=False)
    is_system: Mapped[bool]   = mapped_column(Boolean, default=False)

    # Статус ограничений: "ok" | "frozen" | "spamblocked"
    status: Mapped[str]       = mapped_column(String(16), default="ok")

    chats_count: Mapped[int]  = mapped_column(Integer, default=0)

    # ── Прокси ───────────────────────────────────────────────────────────────
    proxy_host: Mapped[str | None] = mapped_column(String(128))
    proxy_port: Mapped[int | None] = mapped_column(Integer)
    proxy_type: Mapped[str | None] = mapped_column(String(16))   # "socks5" | "http"
    proxy_user: Mapped[str | None] = mapped_column(String(64))
    proxy_pass: Mapped[str | None] = mapped_column(String(128))

    # ── Нагрузка (для балансировки) ───────────────────────────────────────────
    sends_last_hour: Mapped[int] = mapped_column(Integer, default=0)
    sends_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    owner: Mapped["User | None"] = relationship(back_populates="accounts")
    task_links: Mapped[list["TaskAccount"]] = relationship(back_populates="account")

    @property
    def proxy_label(self) -> str:
        """Строка для отображения прокси."""
        if not self.proxy_host:
            return "нет"
        t = self.proxy_type or "socks5"
        user_part = f"{self.proxy_user}@" if self.proxy_user else ""
        return f"{t}://{user_part}{self.proxy_host}:{self.proxy_port}"

    @property
    def status_icon(self) -> str:
        if self.status == "frozen":
            return "❄️"
        if self.status == "spamblocked":
            return "🚫"
        return "✅" if self.is_active else "⏸"

    @property
    def status_label(self) -> str:
        if self.status == "frozen":
            return "❄️ Заморожен Telegram"
        if self.status == "spamblocked":
            return "🚫 Спамблок"
        if not self.is_active:
            return "⏸ Приостановлен"
        if self.is_banned:
            return "❌ Заблокирован"
        return "✅ Активен"


# ─────────────────────────────────────────────────────────────────────────────
# ЗАДАЧИ РАССЫЛОК
# ─────────────────────────────────────────────────────────────────────────────

class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int]      = mapped_column(BigInteger, ForeignKey("users.id"))
    name: Mapped[str]         = mapped_column(String(128), default="Задача")
    message: Mapped[str]      = mapped_column(Text)

    photo_file_ids: Mapped[str]   = mapped_column(Text, default="[]")
    format_entities: Mapped[str]  = mapped_column(Text, default="[]")

    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)

    is_active: Mapped[bool]   = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"]              = relationship(back_populates="tasks")
    chats: Mapped[list["TaskChat"]]   = relationship(back_populates="task",   cascade="all, delete")
    accounts: Mapped[list["TaskAccount"]] = relationship(back_populates="task", cascade="all, delete")


class TaskChat(Base):
    __tablename__ = "task_chats"

    id: Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"))
    chat_id: Mapped[str] = mapped_column(String(64))
    chat_title: Mapped[str] = mapped_column(String(128), default="")
    is_ok: Mapped[bool]  = mapped_column(Boolean, default=True)

    task: Mapped["Task"] = relationship(back_populates="chats")


class TaskAccount(Base):
    __tablename__ = "task_accounts"

    id: Mapped[int]         = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int]    = mapped_column(Integer, ForeignKey("tasks.id"))
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"))
    chat_ids: Mapped[str]   = mapped_column(Text, default="")

    task: Mapped["Task"]       = relationship(back_populates="accounts")
    account: Mapped["Account"] = relationship(back_populates="task_links")


# ─────────────────────────────────────────────────────────────────────────────
# ПЛАТЕЖИ
# ─────────────────────────────────────────────────────────────────────────────

class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int]     = mapped_column(BigInteger, ForeignKey("users.id"))
    method: Mapped[str]      = mapped_column(String(32))
    plan: Mapped[str]        = mapped_column(String(32))
    amount: Mapped[float]    = mapped_column(Float)
    currency: Mapped[str]    = mapped_column(String(16))
    status: Mapped[str]      = mapped_column(String(16), default="pending")
    external_id: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="payments")


# ─────────────────────────────────────────────────────────────────────────────
# ЛОГИ
# ─────────────────────────────────────────────────────────────────────────────

class Log(Base):
    __tablename__ = "logs"

    id: Mapped[int]              = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None]  = mapped_column(Integer, ForeignKey("tasks.id"), nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=True)
    chat_id: Mapped[str]         = mapped_column(String(64))
    success: Mapped[bool]        = mapped_column(Boolean)
    error: Mapped[str | None]    = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
