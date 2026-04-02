"""
bot/handlers/tasks.py — создание и управление задачами рассылок.

Мастер создания задачи:
  1. Название
  2. Текст сообщения
  3. Интервал (минуты)
  4. Чаты (список вручную ИЛИ ссылка на папку)
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
    kb_cancel, kb_back_to_menu
)

log = logging.getLogger(__name__)
router = Router()


# ── FSM состояния ─────────────────────────────────────────────────────────────
class CreateTask(StatesGroup):
    name     = State()
    message  = State()
    interval = State()
    chats    = State()   # ввод чатов или папки


# ── Список задач ──────────────────────────────────────────────────────────────

@router.message(Command("tasks"))
@router.callback_query(F.data == "tasks:list")
async def show_tasks(event, user: User, db: AsyncSession):
    """Показать список задач."""
    tasks = await task_service.get_tasks(db, user.id)
    text = "📋 *Ваши задачи*" if tasks else "📋 У вас пока нет задач."
    kb = kb_tasks(tasks)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("tasks:view:"))
async def view_task(query: CallbackQuery, user: User, db: AsyncSession):
    task_id = int(query.data.split(":")[2])
    task = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return

    icon = "▶️" if task.is_active else "⏸"
    text = (
        f"{icon} *{task.name}*\n\n"
        f"💬 Сообщение:\n_{task.message[:200]}_\n\n"
        f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
        f"📬 Чатов: {len(task.chats)}\n"
        f"🤖 Аккаунтов: {len(task.accounts)}"
    )
    await query.message.edit_text(text, reply_markup=kb_task_detail(task), parse_mode="Markdown")


@router.callback_query(F.data.startswith("tasks:toggle:"))
async def toggle_task(query: CallbackQuery, user: User, db: AsyncSession):
    task_id = int(query.data.split(":")[2])

    if not user.has_access:
        await query.answer("⚠️ Нужна активная подписка.", show_alert=True)
        return

    new_state = await task_service.toggle_task(db, task_id, user.id)
    if new_state is None:
        await query.answer("Задача не найдена.", show_alert=True)
        return

    status = "запущена ▶️" if new_state else "остановлена ⏸"
    await query.answer(f"Задача {status}")
    await view_task(query, user, db)


@router.callback_query(F.data.startswith("tasks:delete:"))
async def ask_delete_task(query: CallbackQuery, user: User, db: AsyncSession):
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
async def confirm_delete_task(query: CallbackQuery, user: User, db: AsyncSession):
    task_id = int(query.data.split(":")[2])
    deleted = await task_service.delete_task(db, task_id, user.id)
    if deleted:
        await query.answer("✅ Задача удалена.")
    else:
        await query.answer("❌ Не найдено.", show_alert=True)
    await show_tasks(query, user, db)


# ── Создание задачи (FSM) ─────────────────────────────────────────────────────

@router.callback_query(F.data == "tasks:new")
@router.message(Command("newtask"))
async def start_create_task(event, state: FSMContext, user: User):
    if not user.has_access:
        text = "⚠️ Нужна активная подписка или триал."
        if isinstance(event, Message):
            await event.answer(text)
        else:
            await event.answer(text, show_alert=True)
        return

    msg = (
        "➕ *Новая задача рассылки*\n\n"
        "*Шаг 1/4* — Введите название задачи:\n"
        "Например: `Реклама магазина`"
    )
    if isinstance(event, Message):
        await event.answer(msg, reply_markup=kb_cancel(), parse_mode="Markdown")
    else:
        await event.message.edit_text(msg, reply_markup=kb_cancel(), parse_mode="Markdown")
    await state.set_state(CreateTask.name)


@router.message(CreateTask.name)
async def got_task_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer(
        "*Шаг 2/4* — Введите текст сообщения для рассылки:",
        reply_markup=kb_cancel(), parse_mode="Markdown"
    )
    await state.set_state(CreateTask.message)


@router.message(CreateTask.message)
async def got_task_message(message: Message, state: FSMContext):
    await state.update_data(message=message.text)
    await message.answer(
        "*Шаг 3/4* — Введите интервал в минутах:\n\n"
        "⚠️ Минимум: *15 минут*\n"
        "Пример: `60` = каждый час",
        reply_markup=kb_cancel(), parse_mode="Markdown"
    )
    await state.set_state(CreateTask.interval)


@router.message(CreateTask.interval)
async def got_task_interval(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) < 15:
        await message.answer(
            "❌ Минимальный интервал — 15 минут.\n"
            "Введите число ≥ 15:"
        )
        return
    await state.update_data(interval=int(text))
    await message.answer(
        "*Шаг 4/4* — Добавьте чаты:\n\n"
        "Варианты:\n"
        "1️⃣ Ссылка на папку: `https://t.me/addlist/XXXX`\n"
        "2️⃣ Список через новую строку:\n"
        "`@username1`\n"
        "`-1001234567890`\n\n"
        f"Максимум: {message.from_user.id} чатов",  # будет подставлен лимит ниже
        reply_markup=kb_cancel(), parse_mode="Markdown"
    )
    await state.set_state(CreateTask.chats)


@router.message(CreateTask.chats)
async def got_task_chats(message: Message, state: FSMContext, user: User, db: AsyncSession):
    raw = message.text.strip()
    data = await state.get_data()
    chats = []

    # Вариант 1: ссылка на папку
    if raw.startswith("https://t.me/addlist/"):
        await message.answer("🔍 Получаю список чатов из папки...")
        accounts = await account_service.get_accounts(db, owner_id=user.id)
        if not accounts:
            accounts = await account_service.get_accounts(db)  # системные
        if accounts:
            client = account_service.make_client(accounts[0])
            await client.connect()
            chats = await account_service.get_chats_from_folder(client, raw)
            await client.disconnect()
        if not chats:
            await message.answer("❌ Не удалось получить чаты из папки. Попробуйте список вручную:")
            return

    # Вариант 2: список вручную
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # Убираем @ если есть
            chat_id = line.lstrip("@")
            chats.append({"id": chat_id, "title": chat_id})

    if not chats:
        await message.answer("❌ Не нашёл ни одного чата. Попробуйте снова:")
        return

    # Ограничиваем лимитом
    if len(chats) > user.max_chats:
        chats = chats[:user.max_chats]
        await message.answer(f"⚠️ Ограничено до {user.max_chats} чатов.")

    # Создаём задачу
    task = await task_service.create_task(
        db, user,
        name=data["name"],
        message=data["message"],
        interval_minutes=data["interval"],
        chats=chats,
    )
    await state.clear()

    if not task:
        await message.answer(
            "❌ Не удалось создать задачу.\n"
            "Возможно, превышен лимит чатов.",
            reply_markup=kb_back_to_menu()
        )
        return

    await message.answer(
        f"✅ *Задача создана!*\n\n"
        f"📋 {task.name}\n"
        f"📬 Чатов: {len(task.chats)}\n"
        f"⏱ Каждые {task.interval_minutes} мин.\n\n"
        f"Задача будет запущена автоматически.",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )
    log.info("Пользователь %d создал задачу %d (%d чатов)", user.id, task.id, len(task.chats))
