"""
bot/generator_bot.py — бот-регистратор зеркал.

Сюда приходит пользователь впервые.
Отдаёт токен своего бота → мы валидируем, сохраняем MirrorBot,
mirror_runner подхватывает и запускает зеркало автоматически.

Этот бот сам по себе НЕ является рассылочным ботом.
Только регистрация + статус зеркала + инструкция.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy import select

from config import GENERATOR_BOT_TOKEN, OWNER_ID, MAIN_BOT_LINK
from database import SessionLocal, create_all_tables
from models import MirrorBot, User
from services.user_service import get_or_create_user

log = logging.getLogger(__name__)
router = Router()


# ── FSM ───────────────────────────────────────────────────────────────────────

class RegisterMirror(StatesGroup):
    token = State()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    async with SessionLocal() as db:
        user, _ = await get_or_create_user(
            db,
            tg_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )

        result = await db.execute(
            select(MirrorBot).where(MirrorBot.user_id == user.id)
        )
        mirror = result.scalar_one_or_none()

    if mirror:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Мой бот", callback_data="mirror:status")],
            [InlineKeyboardButton(text="🗑 Удалить и заменить", callback_data="mirror:delete")],
        ])
        text = (
            f"👋 *Добро пожаловать!*\n\n"
            f"У вас уже подключён бот:\n"
            f"*@{mirror.bot_username or '?'}*\n"
            f"Статус: {'✅ Активен' if mirror.is_active else '⏸ Остановлен'}\n\n"
            f"Перейдите в него чтобы пользоваться сервисом."
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Подключить своего бота", callback_data="mirror:register")],
            [InlineKeyboardButton(text="❓ Как это работает?", callback_data="mirror:howto")],
        ])
        text = (
            "👋 *Добро пожаловать в сервис рассылок!*\n\n"
            "Здесь вы можете подключить *своего Telegram-бота* "
            "и использовать его для автоматических рассылок.\n\n"
            "🔹 Ваш бот работает на нашем сервере\n"
            "🔹 Вы управляете им лично\n"
            "🔹 Никто кроме вас не имеет доступа\n\n"
            "Нажмите *Подключить своего бота* чтобы начать."
        )

    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


# ── Инструкция ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "mirror:howto")
async def howto(query: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Подключить своего бота", callback_data="mirror:register")],
    ])
    await query.message.edit_text(
        "❓ *Как это работает?*\n\n"
        "1️⃣ Создайте нового бота у @BotFather\n"
        "   Команда: `/newbot`\n\n"
        "2️⃣ Скопируйте токен бота\n"
        "   Формат: `1234567890:AABBccdd...`\n\n"
        "3️⃣ Вставьте токен здесь\n\n"
        "4️⃣ Мы подключим бота к нашему сервису\n\n"
        "5️⃣ Перейдите в своего бота и начните работу!\n\n"
        "⚠️ *Важно:* Не передавайте токен третьим лицам.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


# ── Регистрация ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "mirror:register")
async def start_register(query: CallbackQuery, state: FSMContext):
    async with SessionLocal() as db:
        result = await db.execute(
            select(MirrorBot).where(MirrorBot.user_id == query.from_user.id)
        )
        if result.scalar_one_or_none():
            await query.answer("У вас уже есть подключённый бот.", show_alert=True)
            return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="start")],
    ])
    await query.message.edit_text(
        "➕ *Подключение бота*\n\n"
        "Отправьте токен вашего бота.\n"
        "Получить можно у @BotFather → /mybots → выбрать бота → API Token\n\n"
        "Формат: `1234567890:AABBccdd...`",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await state.set_state(RegisterMirror.token)


@router.message(RegisterMirror.token)
async def got_token(message: Message, state: FSMContext):
    token = message.text.strip()

    # Базовая проверка формата
    if ":" not in token or len(token) < 30:
        await message.answer(
            "❌ Неверный формат токена.\n"
            "Должно быть что-то вроде: `1234567890:AABBccdd...`\n\n"
            "Попробуйте ещё раз:",
            parse_mode="Markdown",
        )
        return

    # Проверяем токен через Bot API
    await message.answer("🔍 Проверяю токен...")
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await resp.json()

        if not data.get("ok"):
            await message.answer(
                "❌ Токен не прошёл проверку.\n"
                "Убедитесь что бот существует и токен скопирован правильно.\n\n"
                "Попробуйте ещё раз:",
            )
            return

        bot_info = data["result"]
        bot_username = bot_info.get("username", "unknown")

    except Exception as e:
        log.error("Ошибка проверки токена: %s", e)
        await message.answer(
            "❌ Не удалось проверить токен (сетевая ошибка).\n"
            "Попробуйте позже."
        )
        await state.clear()
        return

    # Проверяем что этот токен не занят другим пользователем
    async with SessionLocal() as db:
        result = await db.execute(
            select(MirrorBot).where(MirrorBot.token == token)
        )
        existing = result.scalar_one_or_none()
        if existing:
            await message.answer(
                "❌ Этот токен уже используется другим пользователем.\n"
                "Создайте нового бота у @BotFather."
            )
            await state.clear()
            return

        # Также проверяем что у пользователя ещё нет зеркала
        result = await db.execute(
            select(MirrorBot).where(MirrorBot.user_id == message.from_user.id)
        )
        if result.scalar_one_or_none():
            await message.answer("❌ У вас уже есть подключённый бот.")
            await state.clear()
            return

        # Создаём запись
        mirror = MirrorBot(
            user_id=message.from_user.id,
            token=token,
            bot_username=bot_username,
            is_active=True,
        )
        db.add(mirror)
        await db.commit()

    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🚀 Перейти к @{bot_username}",
            url=f"https://t.me/{bot_username}"
        )],
        [InlineKeyboardButton(text="📊 Статус подключения", callback_data="mirror:status")],
    ])

    await message.answer(
        f"✅ *Бот @{bot_username} подключён!*\n\n"
        f"⏳ Запуск занимает до 60 секунд.\n\n"
        f"После этого перейдите в своего бота и отправьте `/start`.\n\n"
        f"Там доступно всё: создание задач, добавление аккаунтов, управление рассылками.",
        reply_markup=kb,
        parse_mode="Markdown",
    )

    # Уведомляем владельца сервиса
    try:
        gen_bot = Bot(token=GENERATOR_BOT_TOKEN)
        await gen_bot.send_message(
            OWNER_ID,
            f"🆕 *Новое зеркало зарегистрировано*\n\n"
            f"Пользователь: `{message.from_user.id}` @{message.from_user.username or '—'}\n"
            f"Бот: @{bot_username}",
            parse_mode="Markdown",
        )
        await gen_bot.session.close()
    except Exception:
        pass

    log.info("Новое зеркало: user=%d bot=@%s", message.from_user.id, bot_username)


# ── Статус зеркала ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "mirror:status")
async def mirror_status(query: CallbackQuery):
    async with SessionLocal() as db:
        result = await db.execute(
            select(MirrorBot).where(MirrorBot.user_id == query.from_user.id)
        )
        mirror = result.scalar_one_or_none()

    if not mirror:
        await query.answer("Зеркало не найдено.", show_alert=True)
        return

    status = "✅ Активен" if mirror.is_active else "⏸ Остановлен"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🚀 Перейти к @{mirror.bot_username}",
            url=f"https://t.me/{mirror.bot_username}"
        )],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="mirror:delete")],
    ])
    await query.message.edit_text(
        f"🤖 *Ваш бот*\n\n"
        f"Username: @{mirror.bot_username}\n"
        f"Статус: {status}\n\n"
        f"Перейдите в бота для работы с рассылками.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


# ── Удаление зеркала ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "mirror:delete")
async def delete_mirror(query: CallbackQuery):
    async with SessionLocal() as db:
        result = await db.execute(
            select(MirrorBot).where(MirrorBot.user_id == query.from_user.id)
        )
        mirror = result.scalar_one_or_none()
        if mirror:
            await db.delete(mirror)
            await db.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Подключить нового бота", callback_data="mirror:register")],
    ])
    await query.message.edit_text(
        "✅ *Бот отключён.*\n\n"
        "Все задачи и данные сохранены.\n"
        "Вы можете подключить другого бота в любой момент.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


# ── Возврат на старт ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "start")
async def back_to_start(query: CallbackQuery, state: FSMContext):
    await state.clear()
    # имитируем /start
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Подключить своего бота", callback_data="mirror:register")],
        [InlineKeyboardButton(text="❓ Как это работает?", callback_data="mirror:howto")],
    ])
    await query.message.edit_text(
        "👋 *Добро пожаловать в сервис рассылок!*\n\n"
        "Нажмите *Подключить своего бота* чтобы начать.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
    await create_all_tables()

    bot = Bot(token=GENERATOR_BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    log.info("Generator bot запущен...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [GENERATOR] %(levelname)s: %(message)s",
    )
    asyncio.run(main())
