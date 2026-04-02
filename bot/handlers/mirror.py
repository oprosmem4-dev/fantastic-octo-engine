from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import cancel_kb
from models import User, MirrorBot

router = Router()


class MirrorFSM(StatesGroup):
    token = State()


@router.message(Command("mirror"))
@router.callback_query(F.data == "menu:mirror")
async def show_mirror(event: Message | CallbackQuery, user: User, db: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    result = await db.execute(select(MirrorBot).where(MirrorBot.user_id == user.id))
    mirror = result.scalar_one_or_none()

    if mirror:
        status = "✅ Активен" if mirror.is_active else "❌ Неактивен"
        text = (
            f"🤖 Ваш зеркальный бот\n"
            f"Username: @{mirror.bot_username or '—'}\n"
            f"Статус: {status}"
        )
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑 Удалить", callback_data="mirror:delete")
        builder.button(text="🔙 Меню", callback_data="menu:main")
        builder.adjust(2)
        kb = builder.as_markup()
    else:
        text = (
            "🤖 У вас нет зеркального бота.\n\n"
            "Создайте бота через @BotFather и добавьте токен:"
        )
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Добавить бота", callback_data="mirror:add")
        builder.button(text="🔙 Меню", callback_data="menu:main")
        builder.adjust(2)
        kb = builder.as_markup()

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(F.data == "mirror:add")
async def mirror_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MirrorFSM.token)
    await callback.message.edit_text(
        "🤖 Введите токен бота (от @BotFather):",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(MirrorFSM.token)
async def mirror_set_token(message: Message, state: FSMContext, user: User, db: AsyncSession) -> None:
    token = message.text.strip()
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/bot{token}/getMe") as resp:
                data = await resp.json()
                if not data.get("ok"):
                    await message.answer("❌ Неверный токен бота.", reply_markup=cancel_kb())
                    return
                bot_username = data["result"].get("username")

        result = await db.execute(select(MirrorBot).where(MirrorBot.user_id == user.id))
        mirror = result.scalar_one_or_none()
        if mirror:
            mirror.token = token
            mirror.bot_username = bot_username
            mirror.is_active = True
        else:
            mirror = MirrorBot(user_id=user.id, token=token, bot_username=bot_username)
            db.add(mirror)
        await db.commit()
        await state.clear()
        await message.answer(f"✅ Зеркальный бот @{bot_username} добавлен!")
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}", reply_markup=cancel_kb())


@router.callback_query(F.data == "mirror:delete")
async def mirror_delete(callback: CallbackQuery, user: User, db: AsyncSession) -> None:
    result = await db.execute(select(MirrorBot).where(MirrorBot.user_id == user.id))
    mirror = result.scalar_one_or_none()
    if mirror:
        await db.delete(mirror)
        await db.commit()
        await callback.answer("✅ Зеркальный бот удалён.", show_alert=True)
    else:
        await callback.answer("Бот не найден.", show_alert=True)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Меню", callback_data="menu:main")
    await callback.message.edit_text("🤖 Зеркальный бот удалён.", reply_markup=builder.as_markup())
