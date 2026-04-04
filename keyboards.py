"""
bot/keyboards.py — все клавиатуры бота в одном месте.
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import SUBSCRIPTION_PRICES, MAIN_BOT_LINK
from models import Task, Account


def kb_main_menu(has_access: bool) -> InlineKeyboardMarkup:
    """Главное меню."""
    builder = InlineKeyboardBuilder()
    if has_access:
        builder.button(text="📋 Мои задачи",    callback_data="tasks:list")
        builder.button(text="➕ Новая задача",  callback_data="tasks:new")
        builder.button(text="👤 Мои аккаунты", callback_data="accounts:list")
    builder.button(text="💳 Подписка",         callback_data="pay:menu")
    builder.button(text="📊 Статус",           callback_data="status")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def kb_subscription_plans(is_mirror: bool = False) -> InlineKeyboardMarkup:
    """Кнопки выбора тарифного плана."""
    if is_mirror:
        # В зеркале оплата недоступна
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="💳 Оплатить в главном боте",
                url=MAIN_BOT_LINK
            )
        ]])
    builder = InlineKeyboardBuilder()
    labels = {"1month": "1 месяц", "3month": "3 месяца", "6month": "6 месяцев"}
    for plan, info in SUBSCRIPTION_PRICES.items():
        label = f"{labels[plan]} — {info['stars']}⭐ / {info['usdt']}$"
        builder.button(text=label, callback_data=f"pay:select:{plan}")
    builder.button(text="◀️ Назад", callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()


def kb_payment_methods(plan: str) -> InlineKeyboardMarkup:
    """Способы оплаты для выбранного плана."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ Telegram Stars",  callback_data=f"pay:stars:{plan}")
    builder.button(text="💰 CryptoBot (USDT)", callback_data=f"pay:crypto:{plan}")
    builder.button(text="💎 TON",             callback_data=f"pay:ton:{plan}")
    builder.button(text="◀️ Назад",           callback_data="pay:menu")
    builder.adjust(1)
    return builder.as_markup()


def kb_tasks(tasks: list[Task]) -> InlineKeyboardMarkup:
    """Список задач пользователя."""
    builder = InlineKeyboardBuilder()
    for t in tasks:
        icon = "▶️" if t.is_active else "⏸"
        builder.button(
            text=f"{icon} {t.name} ({len(t.chats)} чатов, каждые {t.interval_minutes}м)",
            callback_data=f"tasks:view:{t.id}"
        )
    builder.button(text="➕ Новая задача", callback_data="tasks:new")
    builder.button(text="◀️ Меню",        callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()


def kb_task_detail(task: Task) -> InlineKeyboardMarkup:
    """Управление конкретной задачей."""
    builder = InlineKeyboardBuilder()
    toggle_text = "⏸ Остановить" if task.is_active else "▶️ Запустить"
    builder.button(text=toggle_text,      callback_data=f"tasks:toggle:{task.id}")
    builder.button(text="🗑 Удалить",     callback_data=f"tasks:delete:{task.id}")
    builder.button(text="◀️ К задачам",  callback_data="tasks:list")
    builder.adjust(2, 1)
    return builder.as_markup()


def kb_task_delete_confirm(task_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления задачи."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить",  callback_data=f"tasks:confirm_delete:{task_id}")
    builder.button(text="❌ Отмена",       callback_data=f"tasks:view:{task_id}")
    builder.adjust(2)
    return builder.as_markup()


def kb_accounts(accounts: list[Account]) -> InlineKeyboardMarkup:
    """Список аккаунтов пользователя."""
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        icon = "✅" if acc.is_active else "⏸"
        builder.button(
            text=f"{icon} {acc.phone} ({acc.chats_count} чатов)",
            callback_data=f"accounts:view:{acc.id}"
        )
    builder.button(text="➕ Добавить аккаунт", callback_data="accounts:add")
    builder.button(text="◀️ Меню",             callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()


def kb_account_detail(acc: Account) -> InlineKeyboardMarkup:
    """Управление аккаунтом."""
    builder = InlineKeyboardBuilder()
    toggle_text = "⏸ Отключить" if acc.is_active else "▶️ Включить"
    builder.button(text=toggle_text,          callback_data=f"accounts:toggle:{acc.id}")
    builder.button(text="🗑 Удалить",         callback_data=f"accounts:delete:{acc.id}")
    builder.button(text="◀️ К аккаунтам",    callback_data="accounts:list")
    builder.adjust(2, 1)
    return builder.as_markup()


def kb_cancel() -> InlineKeyboardMarkup:
    """Простая кнопка отмены."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="menu")
    ]])


def kb_back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Меню", callback_data="menu")
    ]])


def kb_access_error() -> InlineKeyboardMarkup:
    """Кнопки после неудачной проверки доступа к чатам."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 К задачам", callback_data="tasks:list")
    builder.button(text="◀️ Меню",     callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()
def kb_confirm_chats() -> InlineKeyboardMarkup:
    """Кнопка подтверждения после ввода чатов."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Продолжить", callback_data="tasks:confirm_chats")
    builder.button(text="❌ Отмена",     callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()


def kb_choose_sender(accounts: list[Account]) -> InlineKeyboardMarkup:
    """Клавиатура выбора аккаунта-отправителя при создании задачи."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🤖 Системные аккаунты", callback_data="tasks:sender:system")
    for acc in accounts:
        icon = "✅" if acc.is_active else "⏸"
        builder.button(
            text=f"{icon} {acc.phone}",
            callback_data=f"tasks:sender:acc:{acc.id}"
        )
    builder.button(text="❌ Отмена", callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()

# ── Админ ─────────────────────────────────────────────────────────────────────

def kb_admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Пользователи",   callback_data="admin:users")
    builder.button(text="🤖 Аккаунты",       callback_data="admin:accounts")
    builder.button(text="📊 Статистика",     callback_data="admin:stats")
    builder.button(text="◀️ Меню",           callback_data="menu")
    builder.adjust(2, 1, 1)
    return builder.as_markup()
