from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import tasks_list_kb, task_actions_kb, cancel_kb
from models import User
from services import task_service

router = Router()


class CreateTaskFSM(StatesGroup):
    name = State()
    message = State()
    interval = State()
    chats = State()


@router.callback_query(F.data == "menu:main")
async def cb_cancel_to_menu(callback: CallbackQuery, state: FSMContext, user: User, db: AsyncSession) -> None:
    await state.clear()
    from bot.keyboards import main_menu_kb
    status = user.subscription_status
    status_emoji = {"active": "✅", "trial": "🆓", "expired": "❌", "blocked": "🚫"}.get(status, "❓")
    await callback.message.edit_text(
        f"Статус: {status_emoji} {status}\n\nВыберите действие:",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@router.message(Command("tasks"))
@router.callback_query(F.data == "menu:tasks")
async def show_tasks(event: Message | CallbackQuery, user: User, db: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    tasks = await task_service.get_tasks(db, user.id)
    text = f"📋 Ваши задачи ({len(tasks)}):"
    kb = tasks_list_kb(tasks)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("task:view:"))
async def view_task(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    task_id = int(callback.data.split(":")[2])
    task = await task_service.get_task(db, task_id, user.id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    status = "▶️ Активна" if task.is_active else "⏸ Остановлена"
    text = (
        f"📋 {task.name}\n"
        f"Статус: {status}\n"
        f"Интервал: {task.interval_minutes} мин\n"
        f"Чатов: {len(task.chats)}"
    )
    await callback.message.edit_text(text, reply_markup=task_actions_kb(task_id, task.is_active))
    await callback.answer()


@router.callback_query(F.data.startswith("task:toggle:"))
async def toggle_task(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    task_id = int(callback.data.split(":")[2])
    task = await task_service.toggle_task(db, task_id, user.id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    status = "▶️ Активна" if task.is_active else "⏸ Остановлена"
    await callback.answer(f"Статус изменён: {status}", show_alert=True)
    text = (
        f"📋 {task.name}\n"
        f"Статус: {status}\n"
        f"Интервал: {task.interval_minutes} мин\n"
        f"Чатов: {len(task.chats)}"
    )
    await callback.message.edit_text(text, reply_markup=task_actions_kb(task_id, task.is_active))


@router.callback_query(F.data.startswith("task:delete:"))
async def delete_task(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    task_id = int(callback.data.split(":")[2])
    deleted = await task_service.delete_task(db, task_id, user.id)
    if deleted:
        await callback.answer("✅ Задача удалена", show_alert=True)
    else:
        await callback.answer("❌ Задача не найдена", show_alert=True)
    tasks = await task_service.get_tasks(db, user.id)
    await callback.message.edit_text(f"📋 Ваши задачи ({len(tasks)}):", reply_markup=tasks_list_kb(tasks))


@router.callback_query(F.data == "task:new")
async def new_task_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CreateTaskFSM.name)
    await callback.message.edit_text(
        "📋 Введите название задачи рассылки:",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(CreateTaskFSM.name)
async def fsm_task_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(CreateTaskFSM.message)
    await message.answer("✍️ Введите текст сообщения для рассылки:", reply_markup=cancel_kb())


@router.message(CreateTaskFSM.message)
async def fsm_task_message(message: Message, state: FSMContext) -> None:
    await state.update_data(message=message.text)
    await state.set_state(CreateTaskFSM.interval)
    await message.answer("⏱ Введите интервал рассылки в минутах (например: 60):", reply_markup=cancel_kb())


@router.message(CreateTaskFSM.interval)
async def fsm_task_interval(message: Message, state: FSMContext) -> None:
    try:
        interval = int(message.text.strip())
        if interval < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число минут (минимум 1):", reply_markup=cancel_kb())
        return
    await state.update_data(interval=interval)
    await state.set_state(CreateTaskFSM.chats)
    await message.answer(
        "💬 Введите ID или username чатов через запятую или каждый с новой строки:",
        reply_markup=cancel_kb(),
    )


@router.message(CreateTaskFSM.chats)
async def fsm_task_chats(message: Message, state: FSMContext, user: User, db: AsyncSession) -> None:
    raw = message.text.strip()
    chat_ids = [c.strip() for c in raw.replace("\n", ",").split(",") if c.strip()]
    if not chat_ids:
        await message.answer("❌ Нужен хотя бы один чат. Попробуйте снова:", reply_markup=cancel_kb())
        return

    data = await state.get_data()
    task = await task_service.create_task(
        db,
        user_id=user.id,
        name=data["name"],
        message=data["message"],
        interval_minutes=data["interval"],
        chat_ids=chat_ids,
    )
    await state.clear()
    await message.answer(
        f"✅ Задача «{task['name']}» создана!\n"
        f"Чатов: {task['chats_count']}\n"
        f"Интервал: {task['interval_minutes']} мин"
    )
