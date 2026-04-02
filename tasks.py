"""
bot/handlers/tasks.py — создание и управление задачами рассылок.
"""
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from models import User
from services import task_service, account_service
from bot.keyboards import (
    kb_tasks, kb_task_detail, kb_task_delete_confirm,
    kb_cancel, kb_back_to_menu, kb_confirm_chats
)

log = logging.getLogger(__name__)
router = Router()


# ── FSM состояния ─────────────────────────────────────────────────────────────
class CreateTask(StatesGroup):
    name     = State()
    message  = State()
    interval = State()
    chats    = State()


# ── Отмена FSM — ДОЛЖНА БЫТЬ ПЕРВОЙ ──────────────────────────────────────────

@router.callback_query(F.data == "menu")
async def cb_cancel_to_menu(query: CallbackQuery, state: FSMContext, user: User):
    """Отмена — сбрасываем FSM и возвращаем в меню."""
    current = await state.get_state()
    if current:
        await state.clear()
    from bot.keyboards import kb_main_menu
    await query.message.edit_text(
        f"👋 Главное меню\n{user.subscription_status}",
        reply_markup=kb_main_menu(user.has_access),
        parse_mode="Markdown"
    )


# ── Список задач ──────────────────────────────────────────────────────────────

@router.message(Command("tasks"))
async def cmd_tasks(message: Message, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()  # сбрасываем FSM на всякий случай
    tasks = await task_service.get_tasks(db, user.id)
    text = "📋 *Ваши задачи*" if tasks else "📋 У вас пока нет задач."
    await message.answer(text, reply_markup=kb_tasks(tasks), parse_mode="Markdown")


@router.callback_query(F.data == "tasks:list")
async def cb_tasks_list(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    tasks = await task_service.get_tasks(db, user.id)
    text = "📋 *Ваши задачи*" if tasks else "📋 У вас пока нет задач."
    await query.message.edit_text(text, reply_markup=kb_tasks(tasks), parse_mode="Markdown")


@router.callback_query(F.data.startswith("tasks:view:"))
async def view_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    task_id = int(query.data.split(":")[2])
    task = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return
    icon = "▶️" if task.is_active else "⏸"
    # Считаем длину через len — selectinload уже загрузил их
    chats_count = len(task.chats)
    accounts_count = len(task.accounts)
    text = (
        f"{icon} *{task.name}*\n\n"
        f"💬 Сообщение:\n_{task.message[:200]}_\n\n"
        f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
        f"📬 Чатов: {chats_count}\n"
        f"🤖 Аккаунтов: {accounts_count}"
    )
    await query.message.edit_text(text, reply_markup=kb_task_detail(task), parse_mode="Markdown")

@router.callback_query(F.data.startswith("tasks:toggle:"))
async def toggle_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    if not user.has_access:
        await query.answer("⚠️ Нужна активная подписка.", show_alert=True)
        return
    task_id = int(query.data.split(":")[2])
    new_state = await task_service.toggle_task(db, task_id, user.id)
    if new_state is None:
        await query.answer("Задача не найдена.", show_alert=True)
        return
    status = "запущена ▶️" if new_state else "остановлена ⏸"
    await query.answer(f"Задача {status}")
    # Обновляем карточку задачи
    task = await task_service.get_task(db, task_id, user.id)
    if task:
        icon = "▶️" if task.is_active else "⏸"
        text = (
            f"{icon} *{task.name}*\n\n"
            f"💬 Сообщение:\n_{task.message[:200]}_\n\n"
            f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
            f"📬 Чатов: {len(task.chats)}"
        )
        await query.message.edit_text(text, reply_markup=kb_task_detail(task), parse_mode="Markdown")


@router.callback_query(F.data.startswith("tasks:delete:"))
async def ask_delete_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    task_id = int(query.data.split(":")[2])
    task = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return
    await query.message.edit_text(
        f"⚠️ Удалить задачу *{task.name}*?\n\nЭто действие нельзя отменить.",
        reply_markup=kb_task_delete_confirm(task_id),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("tasks:confirm_delete:"))
async def confirm_delete_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    task_id = int(query.data.split(":")[2])
    deleted = await task_service.delete_task(db, task_id, user.id)
    if deleted:
        await query.answer("✅ Задача удалена.")
    else:
        await query.answer("❌ Не найдено.", show_alert=True)
    tasks = await task_service.get_tasks(db, user.id)
    text = "📋 *Ваши задачи*" if tasks else "📋 У вас пока нет задач."
    await query.message.edit_text(text, reply_markup=kb_tasks(tasks), parse_mode="Markdown")


# ── Создание задачи (FSM) ─────────────────────────────────────────────────────

@router.callback_query(F.data == "tasks:new")
async def cb_new_task(query: CallbackQuery, state: FSMContext, user: User):
    await state.clear()  # сбрасываем предыдущий FSM если был
    if not user.has_access:
        await query.answer("⚠️ Нужна активная подписка.", show_alert=True)
        return
    await query.message.edit_text(
        "➕ *Новая задача рассылки*\n\n"
        "*Шаг 1/4* — Введите название задачи:\n"
        "Например: `Реклама магазина`",
        reply_markup=kb_cancel(),
        parse_mode="Markdown"
    )
    await state.set_state(CreateTask.name)


@router.message(Command("newtask"))
async def cmd_new_task(message: Message, state: FSMContext, user: User):
    await state.clear()
    if not user.has_access:
        await message.answer("⚠️ Нужна активная подписка.")
        return
    await message.answer(
        "➕ *Новая задача рассылки*\n\n"
        "*Шаг 1/4* — Введите название задачи:",
        reply_markup=kb_cancel(),
        parse_mode="Markdown"
    )
    await state.set_state(CreateTask.name)


@router.message(CreateTask.name)
async def got_task_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer(
        "*Шаг 2/4* — Введите текст сообщения для рассылки:",
        reply_markup=kb_cancel(),
        parse_mode="Markdown"
    )
    await state.set_state(CreateTask.message)


@router.message(CreateTask.message)
async def got_task_message(message: Message, state: FSMContext):
    await state.update_data(message=message.text)
    await message.answer(
        "*Шаг 3/4* — Введите интервал в минутах:\n\n"
        "Минимум: *5 минут*\n"
        "⚠️ Рекомендуем не менее 15 минут\n"
        "Пример: `60` = каждый час",
        reply_markup=kb_cancel(),
        parse_mode="Markdown"
    )
    await state.set_state(CreateTask.interval)


@router.message(CreateTask.interval)
async def got_task_interval(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) < 5:
        await message.answer("❌ Минимум 5 минут. Введите число ≥ 5:")
        return
    await state.update_data(interval=int(text))
    await message.answer(
        "*Шаг 4/4* — Введите чаты:\n\n"
        "Вариант 1 — ссылка на папку:\n`https://t.me/addlist/XXXX`\n\n"
        "Вариант 2 — список через новую строку:\n"
        "`@username`\n"
        "`-1001234567890`\n\n"
        "Введите чаты и нажмите *Продолжить*",
        reply_markup=kb_cancel(),
        parse_mode="Markdown"
    )
    await state.set_state(CreateTask.chats)


@router.message(CreateTask.chats)
async def got_task_chats(message: Message, state: FSMContext, user: User, db: AsyncSession):
    raw = message.text.strip()
    chats = []

    # Вариант 1: папка
    if raw.startswith("https://t.me/addlist/"):
        await message.answer("🔍 Получаю список чатов из папки...")
        accounts = await account_service.get_accounts(db, owner_id=user.id)
        if not accounts:
            accounts = await account_service.get_accounts(db)
        if accounts:
            client = account_service.make_client(accounts[0])
            await client.connect()
            chats = await account_service.get_chats_from_folder(client, raw)
            await client.disconnect()
        if not chats:
            await message.answer("❌ Не удалось получить чаты. Попробуйте список вручную:")
            return

    # Вариант 2: список
    else:
        for line in raw.splitlines():
            line = line.strip().lstrip("@")
            if line:
                chats.append({"id": line, "title": line})

    if not chats:
        await message.answer("❌ Не нашёл чатов. Попробуйте снова:")
        return

    if len(chats) > user.max_chats:
        chats = chats[:user.max_chats]

    # Сохраняем и показываем подтверждение
    await state.update_data(chats=chats)

    preview = "\n".join(f"• {c['title']}" for c in chats[:10])
    if len(chats) > 10:
        preview += f"\n... и ещё {len(chats) - 10}"

    await message.answer(
        f"✅ Найдено чатов: *{len(chats)}*\n\n"
        f"{preview}\n\n"
        f"Нажмите *Продолжить* для создания задачи:",
        reply_markup=kb_confirm_chats(),
        parse_mode="Markdown"
    )
    # Остаёмся в состоянии chats — ждём нажатия кнопки

# СТАЛО:
@router.callback_query(F.data == "tasks:confirm_chats")
async def confirm_chats(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    data = await state.get_data()
    chats = data.get("chats", [])

    if not chats:
        await query.answer("❌ Чаты не найдены.", show_alert=True)
        return

    await state.clear()

    task = await task_service.create_task(
        db, user,
        name=data["name"],
        message=data["message"],
        interval_minutes=data["interval"],
        chats=chats,
    )

    if not task:
        await query.message.edit_text(
            "❌ Не удалось создать задачу.\nВозможно превышен лимит чатов.",
            reply_markup=kb_back_to_menu()
        )
        return

    # task теперь dict — обращаемся через []  а не через .
    await query.message.edit_text(
        f"✅ *Задача создана!*\n\n"
        f"📋 {task['name']}\n"
        f"📬 Чатов: {task['chats_count']}\n"
        f"⏱ Каждые {task['interval_minutes']} мин.",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )
    log.info("Создана задача %d для user %d", task['id'], user.id)
