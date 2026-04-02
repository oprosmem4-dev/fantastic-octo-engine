from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sub_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_chats: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tasks: Mapped[list[Task]] = relationship("Task", back_populates="user", lazy="selectin")
    accounts: Mapped[list[Account]] = relationship(
        "Account", back_populates="owner", foreign_keys="Account.owner_id", lazy="selectin"
    )

    @property
    def has_access(self) -> bool:
        if self.is_blocked:
            return False
        now = datetime.now(timezone.utc)
        if self.sub_ends_at and self.sub_ends_at > now:
            return True
        if self.trial_ends_at and self.trial_ends_at > now:
            return True
        return False

    @property
    def subscription_status(self) -> str:
        if self.is_blocked:
            return "blocked"
        now = datetime.now(timezone.utc)
        if self.sub_ends_at and self.sub_ends_at > now:
            return "active"
        if self.trial_ends_at and self.trial_ends_at > now:
            return "trial"
        return "expired"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    phone: Mapped[str] = mapped_column(String(32))
    api_id: Mapped[int] = mapped_column(Integer)
    api_hash: Mapped[str] = mapped_column(String(255))
    session_string: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    chats_count: Mapped[int] = mapped_column(Integer, default=0)

    owner: Mapped[User | None] = relationship("User", back_populates="accounts", foreign_keys=[owner_id])


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    interval_minutes: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="tasks")
    chats: Mapped[list[TaskChat]] = relationship("TaskChat", back_populates="task", lazy="selectin")
    accounts: Mapped[list[TaskAccount]] = relationship("TaskAccount", back_populates="task", lazy="selectin")


class TaskChat(Base):
    __tablename__ = "task_chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"))
    chat_id: Mapped[str] = mapped_column(String(64))
    chat_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_ok: Mapped[bool] = mapped_column(Boolean, default=True)

    task: Mapped[Task] = relationship("Task", back_populates="chats")


class TaskAccount(Base):
    __tablename__ = "task_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"))
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"))
    chat_ids: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task] = relationship("Task", back_populates="accounts")
    account: Mapped[Account] = relationship("Account")

    def get_chat_ids(self) -> list[str]:
        if not self.chat_ids:
            return []
        return json.loads(self.chat_ids)

    def set_chat_ids(self, ids: list[str]) -> None:
        self.chat_ids = json.dumps(ids)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    method: Mapped[str] = mapped_column(String(32))  # stars | cryptobot | ton
    plan: Mapped[str] = mapped_column(String(32))     # 1week | 1month | 3month | 6month
    amount: Mapped[float] = mapped_column(default=0.0)
    currency: Mapped[str] = mapped_column(String(16), default="USD")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | paid | failed
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User")


class MirrorBot(Base):
    __tablename__ = "mirror_bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    token: Mapped[str] = mapped_column(String(255))
    bot_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship("User")


class Log(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"))
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"))
    chat_id: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
