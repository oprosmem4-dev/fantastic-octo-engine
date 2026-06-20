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
    """Кнопки выбора тарифного плана. Оплата через администратора."""
    builder = InlineKeyboardBuilder()
    labels = {"1month": "1 месяц", "1week": "1 неделя", "6month": "6 месяцев"}
    for plan, info in SUBSCRIPTION_PRICES.items():
        label = f"{labels[plan]} — {info['stars']}⭐"
        builder.button(text=label, callback_data=f"pay:select:{plan}")
    builder.button(text="◀️ Назад", callback_data="menu:new")
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
    builder.button(text="◀️ Меню",        callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()


def kb_task_detail(task: Task) -> InlineKeyboardMarkup:
    """Управление конкретной задачей."""
    builder = InlineKeyboardBuilder()
    toggle_text = "⏸ Остановить" if task.is_active else "▶️ Запустить"
    builder.button(text=toggle_text,           callback_data=f"tasks:toggle:{task.id}")
    builder.button(text="🗑 Удалить",          callback_data=f"tasks:delete:{task.id}")
    builder.button(text="📊 Статистика",       callback_data=f"tasks:stats:{task.id}")
    builder.button(text="◀️ К задачам",        callback_data="tasks:list")
    builder.adjust(2, 1, 1)
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
        icon = acc.status_icon
        builder.button(
            text=f"{icon} {acc.phone} ({acc.chats_count} чатов)",
            callback_data=f"accounts:view:{acc.id}"
        )
    builder.button(text="➕ Добавить аккаунт", callback_data="accounts:add")
    builder.button(text="◀️ Меню",             callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()


def kb_account_detail(acc: Account) -> InlineKeyboardMarkup:
    """Управление аккаунтом."""
    builder = InlineKeyboardBuilder()

    if acc.status == "ok":
        toggle_text = "⏸ Отключить" if acc.is_active else "▶️ Включить"
        builder.button(text=toggle_text, callback_data=f"accounts:toggle:{acc.id}")

    builder.button(text="🗑 Удалить",       callback_data=f"accounts:delete:{acc.id}")
    builder.button(text="◀️ К аккаунтам",  callback_data="accounts:list")

    if acc.status == "ok":
        builder.adjust(2, 1)
    else:
        builder.adjust(1)

    return builder.as_markup()


def kb_cancel() -> InlineKeyboardMarkup:
    """Простая кнопка отмены."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="menu:new")
    ]])


def kb_back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Меню", callback_data="menu:new")
    ]])


def kb_access_error() -> InlineKeyboardMarkup:
    """Кнопки после неудачной проверки доступа к чатам."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 К задачам", callback_data="tasks:list")
    builder.button(text="◀️ Меню",     callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()


def kb_confirm_chats() -> InlineKeyboardMarkup:
    """Кнопка подтверждения после ввода чатов."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Продолжить", callback_data="tasks:confirm_chats")
    builder.button(text="❌ Отмена",     callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()


def kb_choose_sender(accounts: list[Account]) -> InlineKeyboardMarkup:
    """Клавиатура выбора аккаунта-отправителя при создании задачи."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🤖 Системные аккаунты", callback_data="tasks:sender:system")
    for acc in accounts:
        if acc.status != "ok":
            continue
        icon = "✅" if acc.is_active else "⏸"
        builder.button(
            text=f"{icon} {acc.phone}",
            callback_data=f"tasks:sender:acc:{acc.id}"
        )
    builder.button(text="❌ Отмена", callback_data="menu:new")
    builder.adjust(1)
    return builder.as_markup()


# ── Админ ─────────────────────────────────────────────────────────────────────

def kb_admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Пользователи",    callback_data="admin:users")
    builder.button(text="🤖 Сист. аккаунты", callback_data="admin:accounts")
    builder.button(text="👤 Акк. польз.",     callback_data="admin:all_accounts")
    builder.button(text="📋 Все задачи",      callback_data="admin:all_tasks")
    builder.button(text="📊 Статистика",      callback_data="admin:stats")
    builder.button(text="💳 Платёжный бот",   callback_data="admin:paybot")
    builder.button(text="📢 Рассылка всем",   callback_data="admin:broadcast")
    builder.button(text="◀️ Меню",            callback_data="menu:new")
    builder.adjust(2, 2, 2, 2)
    return builder.as_markup()

# ── Пагинация логов задачи ────────────────────────────────────────────────────

def kb_task_logs_page(task_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """
    Клавиатура для постраничного просмотра ссылок на сообщения задачи.
    20 ссылок на страницу. Навигация: ← / →, возврат к задаче.
    """
    builder = InlineKeyboardBuilder()

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"tasks:logs:{task_id}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"tasks:logs:{task_id}:{page + 1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(
        text=f"📄 {page + 1} / {total_pages}",
        callback_data="noop",
    ))
    builder.row(InlineKeyboardButton(text="📊 К статистике", callback_data=f"tasks:stats:{task_id}"))
    builder.row(InlineKeyboardButton(text="◀️ К задаче", callback_data=f"tasks:view:{task_id}"))
    return builder.as_markup()


def kb_task_stats(task_id: int, has_logs: bool) -> InlineKeyboardMarkup:
    """Клавиатура экрана статистики задачи."""
    builder = InlineKeyboardBuilder()
    if has_logs:
        builder.button(text="🔗 Ссылки на сообщения", callback_data=f"tasks:logs:{task_id}:0")
    builder.button(text="◀️ К задаче", callback_data=f"tasks:view:{task_id}")
    builder.adjust(1)
    return builder.as_markup()


# ── Админ: задачи всех пользователей ─────────────────────────────────────────

def kb_admin_task_detail(task_id: int, is_active: bool, user_id: int) -> InlineKeyboardMarkup:
    """Карточка задачи в панели администратора."""
    builder = InlineKeyboardBuilder()
    toggle_text = "⏸ Остановить задачу" if is_active else "▶️ Запустить задачу"
    builder.button(text=toggle_text, callback_data=f"admin:task:toggle:{task_id}")
    builder.button(text="🔗 Ссылки на сообщения", callback_data=f"admin:task:logs:{task_id}:0")
    builder.button(text="◀️ К задачам польз.", callback_data=f"admin:tasks:user:{user_id}")
    builder.button(text="◀️ Все задачи", callback_data="admin:all_tasks")
    builder.adjust(1)
    return builder.as_markup()


def kb_admin_tasks_logs_page(task_id: int, page: int, total_pages: int, user_id: int) -> InlineKeyboardMarkup:
    """Пагинация логов в панели администратора."""
    builder = InlineKeyboardBuilder()

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:task:logs:{task_id}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin:task:logs:{task_id}:{page + 1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(
        text=f"📄 {page + 1} / {total_pages}",
        callback_data="noop",
    ))
    builder.row(InlineKeyboardButton(text="◀️ К задаче", callback_data=f"admin:task:view:{task_id}"))
    return builder.as_markup()
