from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Мои задачи", callback_data="menu:tasks")
    builder.button(text="📱 Аккаунты", callback_data="menu:accounts")
    builder.button(text="💳 Оплата", callback_data="menu:pay")
    builder.button(text="🤖 Зеркало", callback_data="menu:mirror")
    builder.button(text="ℹ️ Статус", callback_data="menu:status")
    builder.adjust(2)
    return builder.as_markup()


def tasks_list_kb(tasks: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for task in tasks:
        status = "▶️" if task.is_active else "⏸"
        builder.button(text=f"{status} {task.name}", callback_data=f"task:view:{task.id}")
    builder.button(text="➕ Новая задача", callback_data="task:new")
    builder.button(text="🔙 Меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def task_actions_kb(task_id: int, is_active: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    toggle_text = "⏸ Остановить" if is_active else "▶️ Запустить"
    builder.button(text=toggle_text, callback_data=f"task:toggle:{task_id}")
    builder.button(text="🗑 Удалить", callback_data=f"task:delete:{task_id}")
    builder.button(text="🔙 К задачам", callback_data="menu:tasks")
    builder.adjust(2)
    return builder.as_markup()


def accounts_kb(accounts: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        status = "✅" if acc.is_active else "❌"
        builder.button(text=f"{status} {acc.phone}", callback_data=f"acc:view:{acc.id}")
    builder.button(text="➕ Добавить аккаунт", callback_data="acc:add")
    builder.button(text="🔙 Меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def account_actions_kb(account_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Удалить", callback_data=f"acc:delete:{account_id}")
    builder.button(text="🔙 К аккаунтам", callback_data="menu:accounts")
    builder.adjust(2)
    return builder.as_markup()


def payment_plans_kb(method: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    plans = [
        ("1 неделя", "1week"),
        ("1 месяц", "1month"),
        ("3 месяца", "3month"),
        ("6 месяцев", "6month"),
    ]
    for label, plan in plans:
        builder.button(text=label, callback_data=f"pay:{method}:{plan}")
    builder.button(text="🔙 Меню", callback_data="menu:main")
    builder.adjust(2)
    return builder.as_markup()


def payment_methods_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ Telegram Stars", callback_data="pay_method:stars")
    builder.button(text="💎 CryptoBot", callback_data="pay_method:cryptobot")
    builder.button(text="💎 TON", callback_data="pay_method:ton")
    builder.button(text="🔙 Меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="menu:main")
    return builder.as_markup()


def confirm_kb(action: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"confirm:{action}")
    builder.button(text="❌ Отмена", callback_data="menu:main")
    builder.adjust(2)
    return builder.as_markup()


def admin_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="admin:stats")
    builder.button(text="🔑 Аккаунты", callback_data="admin:accounts")
    builder.button(text="👥 Пользователи", callback_data="admin:users")
    builder.button(text="🔙 Меню", callback_data="menu:main")
    builder.adjust(2)
    return builder.as_markup()


def admin_accounts_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить системный", callback_data="admin:add_system")
    builder.button(text="🔙 Назад", callback_data="admin:main")
    builder.adjust(1)
    return builder.as_markup()
