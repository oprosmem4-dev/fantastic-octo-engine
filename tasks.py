"""
bot/handlers/tasks.py — создание и управление задачами рассылок.

ИЗМЕНЕНИЯ (медиа-рефакторинг):
  - Фото больше не хранится как file_id.
  - При создании задачи байты фото скачиваются через Bot API
    и сохраняются в таблицу TaskMedia (LargeBinary).
  - Воркер при первой отправке читает байты → отправляет через client.send_file()
    → кеширует полученный Telethon file_id в TaskMediaCache
    → удаляет строки из TaskMedia.
  - При повторных отправках воркер использует кеш (без байт).
"""
import html
import io
import logging
import json
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any

from models import User, TaskMedia
from services import task_service, account_service
from bot.keyboards import (
    kb_tasks, kb_task_detail, kb_task_delete_confirm,
    kb_cancel, kb_back_to_menu, kb_confirm_chats,
    kb_choose_sender, kb_access_error,
)

log = logging.getLogger(__name__)
router = Router()


# ── FSM ───────────────────────────────────────────────────────────────────────

class CreateTask(StatesGroup):
    name     = State()
    message  = State()
    interval = State()
    chats    = State()
    sender   = State()


# ── Отмена — ПЕРВОЙ в роутере ─────────────────────────────────────────────────

@router.callback_query(F.data == "menu")
async def cb_cancel_to_menu(query: CallbackQuery, state: FSMContext, user: User):
    current = await state.get_state()
    if current:
        await state.clear()
    from bot.keyboards import kb_main_menu
    await query.message.edit_text(
        f"👋 Главное меню\n{html.escape(user.subscription_status)}",
        reply_markup=kb_main_menu(user.has_access),
        parse_mode="HTML",
    )


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _normalize_chat_id(raw: str) -> str:
    s = raw.strip()
    if s.startswith("@"):
        return f"@{s.lstrip('@')}"
    return s


def _chat_display_from_task_chat(c) -> str:
    ct = c.chat_title or ""
    if ct.startswith("@"):
        uname = ct.lstrip("@")
        return f'<a href="https://t.me/{uname}">{html.escape(ct)}</a>'
    return html.escape(ct) if ct else html.escape(c.chat_id)


async def _download_photo_bytes(bot, file_id: str) -> bytes | None:
    """
    Скачать фото через Bot API и вернуть сырые байты.
    Возвращает None при ошибке.
    """
    try:
        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        return buf.getvalue()
    except Exception as e:
        log.error("Не удалось скачать фото file_id=%s: %s", file_id[:20], e)
        return None


# ── Список задач ──────────────────────────────────────────────────────────────

@router.message(Command("tasks"))
async def cmd_tasks(message: Message, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    tasks = await task_service.get_tasks(db, user.id)
    text = "📋 <b>Ваши задачи</b>" if tasks else "📋 У вас пока нет задач."
    await message.answer(text, reply_markup=kb_tasks(tasks), parse_mode="HTML")


@router.callback_query(F.data == "tasks:list")
async def cb_tasks_list(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    tasks = await task_service.get_tasks(db, user.id)
    text = "📋 <b>Ваши задачи</b>" if tasks else "📋 У вас пока нет задач."
    await query.message.edit_text(text, reply_markup=kb_tasks(tasks), parse_mode="HTML")


@router.callback_query(F.data.startswith("tasks:view:"))
async def view_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    task_id = int(query.data.split(":")[2])
    task = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return

    icon = "▶️" if task.is_active else "⏸"

    chats_lines = []
    for c in task.chats[:15]:
        display = _chat_display_from_task_chat(c)
        status  = "" if c.is_ok else " ⚠️"
        chats_lines.append(f"• {display}{status}")
    chats_block = "\n".join(chats_lines) if chats_lines else "—"
    if len(task.chats) > 15:
        chats_block += f"\n…и ещё {len(task.chats) - 15}"

    acc_lines = []
    for link in task.accounts:
        try:
            ids = json.loads(link.chat_ids) if link.chat_ids else []
        except Exception:
            ids = []
        acc = getattr(link, "account", None)
        acc_name = acc.phone if acc else f"acc#{link.account_id}"
        if acc and acc.is_system:
            acc_name += " (system)"
        acc_lines.append(f"• {html.escape(acc_name)}: {len(ids)} чатов")

    accounts_block = "\n".join(acc_lines) if acc_lines else "—"
    media_note = " 📷" if task.has_media else ""

    text = (
        f"{icon} <b>{html.escape(task.name)}</b>{media_note}\n\n"
        f"💬 Сообщение:\n<i>{html.escape(task.message[:200])}</i>\n\n"
        f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
        f"📬 Чатов: {len(task.chats)}\n"
        f"🤖 Аккаунтов: {len(task.accounts)}\n\n"
        f"🏷 <b>Чаты рассылки:</b>\n{chats_block}\n\n"
        f"👤 <b>Распределение:</b>\n{accounts_block}"
    )

    await query.message.edit_text(
        text,
        reply_markup=kb_task_detail(task),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("tasks:toggle:"))
async def toggle_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    if not user.has_access:
        await query.answer("⚠️ Нужна активная подписка.", show_alert=True)
        return
    task_id   = int(query.data.split(":")[2])
    new_state = await task_service.toggle_task(db, task_id, user.id)
    if new_state is None:
        await query.answer("Задача не найдена.", show_alert=True)
        return
    status = "запущена ▶️" if new_state else "остановлена ⏸"
    await query.answer(f"Задача {status}")
    task = await task_service.get_task(db, task_id, user.id)
    if task:
        icon = "▶️" if task.is_active else "⏸"
        text = (
            f"{icon} <b>{html.escape(task.name)}</b>\n\n"
            f"💬 Сообщение:\n<i>{html.escape(task.message[:200])}</i>\n\n"
            f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
            f"📬 Чатов: {len(task.chats)}"
        )
        await query.message.edit_text(
            text, reply_markup=kb_task_detail(task), parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("tasks:delete:"))
async def ask_delete_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    task_id = int(query.data.split(":")[2])
    task    = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return
    await query.message.edit_text(
        f"⚠️ Удалить задачу <b>{html.escape(task.name)}</b>?\n\nЭто действие нельзя отменить.",
        reply_markup=kb_task_delete_confirm(task_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("tasks:confirm_delete:"))
async def confirm_delete_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    task_id = int(query.data.split(":")[2])
    deleted = await task_service.delete_task(db, task_id, user.id)
    await query.answer("✅ Задача удалена." if deleted else "❌ Не найдено.", show_alert=not deleted)
    tasks = await task_service.get_tasks(db, user.id)
    text  = "📋 <b>Ваши задачи</b>" if tasks else "📋 У вас пока нет задач."
    await query.message.edit_text(text, reply_markup=kb_tasks(tasks), parse_mode="HTML")


# ── Создание задачи (FSM) ─────────────────────────────────────────────────────

@router.callback_query(F.data == "tasks:new")
async def cb_new_task(query: CallbackQuery, state: FSMContext, user: User):
    await state.clear()
    if not user.has_access:
        await query.answer("⚠️ Нужна активная подписка.", show_alert=True)
        return
    await query.message.edit_text(
        "➕ <b>Новая задача рассылки</b>\n\n"
        "<b>Шаг 1/4</b> — Введите название задачи:\n"
        "Например: <code>Реклама магазина</code>",
        reply_markup=kb_cancel(),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.name)


@router.message(Command("newtask"))
async def cmd_new_task(message: Message, state: FSMContext, user: User):
    await state.clear()
    if not user.has_access:
        await message.answer("⚠️ Нужна активная подписка.")
        return
    await message.answer(
        "➕ <b>Новая задача рассылки</b>\n\n"
        "<b>Шаг 1/4</b> — Введите название задачи:",
        reply_markup=kb_cancel(),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.name)


@router.message(CreateTask.name)
async def got_task_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer(
        "<b>Шаг 2/4</b> — Введите текст сообщения.\n\n"
        "Можно прикрепить до 5 фото (отправьте как медиагруппу или по одному).\n"
        "После отправки фото напишите <code>ок</code> чтобы продолжить.",
        reply_markup=kb_cancel(),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.message)


@router.message(CreateTask.message)
async def got_task_message(message: Message, state: FSMContext):
    """
    Принимаем текст и/или фото.
    Байты фото скачиваются сразу и сохраняются в FSM-состоянии как список bytes.
    В БД байты попадут только при финальном создании задачи (confirm_chats).
    """
    text, entities_json = _extract_text_and_entities(message)

    # ── Одиночное фото ─────────────────────────────────────────────────────
    if message.photo and not message.media_group_id:
        photo_bytes = await _download_photo_bytes(message.bot, message.photo[-1].file_id)
        await state.update_data(
            message=text,
            format_entities=entities_json,
            # храним список байт-объектов как base64 чтобы FSM мог их сериализовать
            photo_bytes_b64=[_b64(photo_bytes)] if photo_bytes else [],
        )
        await message.answer(
            "<b>Шаг 3/4</b> — Введите интервал в минутах:\n\n"
            "Минимум: <b>1 минута</b>\n"
            "⚠️ РЕКОМЕНДУЕМ ОТ 5 ДО 15 минут\n"
            "Пример: <code>60</code> = каждый час",
            reply_markup=kb_cancel(),
            parse_mode="HTML",
        )
        await state.set_state(CreateTask.interval)
        return

    # ── Медиагруппа ────────────────────────────────────────────────────────
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        data = await state.get_data()
        mg = data.get("media_group", {"id": media_group_id, "photos_b64": [], "text": "", "entities": []})
        if mg.get("id") != media_group_id:
            mg = {"id": media_group_id, "photos_b64": [], "text": "", "entities": []}

        if message.photo:
            if len(mg["photos_b64"]) < 5:
                photo_bytes = await _download_photo_bytes(message.bot, message.photo[-1].file_id)
                if photo_bytes:
                    mg["photos_b64"].append(_b64(photo_bytes))
        if text:
            mg["text"]     = text
            mg["entities"] = entities_json

        await state.update_data(media_group=mg)
        await message.answer(
            f"📸 Принял фото: {len(mg['photos_b64'])}/5. "
            "Добавьте ещё или отправьте <code>ок</code> для продолжения.",
            parse_mode="HTML",
        )
        return

    # ── "ок" после медиагруппы ──────────────────────────────────────────────
    data = await state.get_data()
    mg   = data.get("media_group")
    if (message.text or "").strip().lower() in {"ок", "ok", "да", "done"} and mg and mg.get("photos_b64"):
        text           = mg.get("text", "")
        entities_json  = mg.get("entities", [])
        photos_b64     = mg.get("photos_b64", [])
        await state.update_data(
            message=text,
            format_entities=entities_json,
            photo_bytes_b64=photos_b64,
            media_group=None,
        )
        await message.answer(
            "<b>Шаг 3/4</b> — Введите интервал в минутах:\n\n"
            "Минимум: <b>1 минута</b>\n"
            "⚠️ РЕКОМЕНДУЕМ ОТ 5 ДО 15 минут\n"
            "Пример: <code>60</code> = каждый час",
            reply_markup=kb_cancel(),
            parse_mode="HTML",
        )
        await state.set_state(CreateTask.interval)
        return

    # ── Только текст ────────────────────────────────────────────────────────
    if not text:
        await message.answer("❌ Пришлите текст или фото (до 5 штук) с подписью.")
        return

    await state.update_data(
        message=text,
        format_entities=entities_json,
        photo_bytes_b64=[],
    )
    await message.answer(
        "<b>Шаг 3/4</b> — Введите интервал в минутах:\n\n"
        "Минимум: <b>1 минута</b>\n"
        "⚠️ РЕКОМЕНДУЕМ ОТ 5 ДО 15 минут\n"
        "Пример: <code>60</code> = каждый час",
        reply_markup=kb_cancel(),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.interval)


@router.message(CreateTask.interval)
async def got_task_interval(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ Минимум 1 минута. Введите число ≥ 1:")
        return
    await state.update_data(interval=int(text))
    await message.answer(
        "<b>Шаг 4/4</b> — Введите чаты:\n\n"
        "Вариант 1 — ссылка на папку:\n<code>https://t.me/addlist/XXXX</code>\n\n"
        "Вариант 2 — список через новую строку:\n"
        "<code>@username</code>\n<code>-1001234567890</code>",
        reply_markup=kb_cancel(),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.chats)


@router.message(CreateTask.chats)
async def got_task_chats(message: Message, state: FSMContext, user: User, db: AsyncSession):
    raw   = message.text.strip()
    chats = []

    if raw.startswith("https://t.me/addlist/"):
        await message.answer("🔍 Получаю список чатов из папки...")

        accounts = await account_service.get_accounts(db, owner_id=user.id)
        if not accounts:
            accounts = await account_service.get_accounts(db)
        if not accounts:
            await message.answer(
                "❌ Нет доступных аккаунтов.\n"
                "Добавьте аккаунт через /accounts или обратитесь к администратору."
            )
            return

        client = account_service.make_client(accounts[0])
        try:
            await client.connect()
            await client.get_dialogs()
            chats = await account_service.get_chats_from_folder(client, raw)
        except Exception as e:
            log.error("Ошибка получения папки: %s", e)
            await message.answer(
                f"❌ Не удалось получить чаты из папки.\n<code>{html.escape(str(e))}</code>\n\nПопробуйте ввести вручную:",
                parse_mode="HTML",
            )
            return
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        if not chats:
            await message.answer(
                "❌ Папка пустая или недоступна.\n\n"
                "Убедитесь что ссылка вида <code>https://t.me/addlist/XXXX</code>\n\n"
                "Попробуйте ввести чаты вручную:",
                parse_mode="HTML",
            )
            return

    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("@"):
                username = line.lstrip("@")
                chat_id  = f"@{username}"
            elif line.lstrip("-").isdigit():
                username = None
                chat_id  = line
            else:
                username = line.lstrip("@")
                chat_id  = f"@{username}"

            chats.append({
                "id":          chat_id,
                "title":       f"@{username}" if username else chat_id,
                "username":    username,
                "access_hash": None,
                "folder_slug": None,
            })

    if not chats:
        await message.answer("❌ Не нашёл чатов. Попробуйте снова:")
        return

    if len(chats) > user.max_chats:
        chats = chats[:user.max_chats]

    await state.update_data(chats=chats)

    preview_lines = []
    for c in chats[:10]:
        uname = c.get("username")
        title = c.get("title") or (f"@{uname}" if uname else c["id"])
        preview_lines.append(f"• {html.escape(title)}")
    preview = "\n".join(preview_lines)
    if len(chats) > 10:
        preview += f"\n...и ещё {len(chats) - 10}"

    accounts = await account_service.get_accounts(db, owner_id=user.id)

    await message.answer(
        f"✅ Найдено чатов: <b>{len(chats)}</b>\n\n"
        f"{preview}\n\n"
        f"<b>Шаг 5/5</b> — Выберите отправителя:",
        reply_markup=kb_choose_sender(accounts),
        parse_mode="HTML",
    )
    await state.set_state(CreateTask.sender)


@router.callback_query(CreateTask.sender, F.data.startswith("tasks:sender:"))
async def got_sender_choice(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    choice = query.data

    if choice == "tasks:sender:system":
        await state.update_data(sender_account_id=None)
        sender_text = "🤖 Системные аккаунты"
    else:
        account_id = int(choice.split(":")[-1])
        await state.update_data(sender_account_id=account_id)
        acc         = await account_service.get_account_by_id(db, account_id)
        sender_text = f"👤 {acc.phone}" if acc else "👤 Выбранный аккаунт"

    data  = await state.get_data()
    chats = data.get("chats", [])
    has_photo = bool(data.get("photo_bytes_b64"))

    await query.message.edit_text(
        f"✅ Отправитель: <b>{html.escape(sender_text)}</b>\n\n"
        f"📋 Задача: <b>{html.escape(data['name'])}</b>\n"
        f"📬 Чатов: <b>{len(chats)}</b>\n"
        f"⏱ Каждые {data['interval']} мин.\n"
        f"{'📷 С фото' if has_photo else '📝 Только текст'}\n\n"
        f"Нажмите <b>Продолжить</b> для создания задачи:",
        reply_markup=kb_confirm_chats(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "tasks:confirm_chats")
async def confirm_chats(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    data  = await state.get_data()
    chats = data.get("chats", [])
    if not chats:
        await query.answer("❌ Чаты не найдены.", show_alert=True)
        return

    sender_account_id = data.get("sender_account_id")

    check_account = None
    if sender_account_id is not None:
        check_account = await account_service.get_account_by_id(db, sender_account_id)
    else:
        accounts = await account_service.get_accounts(db)
        if accounts:
            check_account = accounts[0]
        if not check_account:
            accounts = await account_service.get_accounts(db, owner_id=user.id)
            if accounts:
                check_account = accounts[0]

    if check_account is None:
        await state.clear()
        await query.message.edit_text(
            "❌ Нет доступных аккаунтов для проверки.\nДобавьте аккаунт в /accounts",
            reply_markup=kb_back_to_menu(),
        )
        return

    from_folder = any(c.get("folder_slug") for c in chats)
    if from_folder:
        await query.message.edit_text(
            f"🔍 Вступаю в {len(chats)} чатов из папки и проверяю доступ...\n"
            f"Обычно занимает меньше минуты.",
        )
    else:
        await query.message.edit_text(
            f"🔍 Проверяю доступ к {len(chats)} чатам...\n"
            f"Это может занять несколько минут.",
        )

    client = account_service.make_client(check_account)
    try:
        await client.connect()
        await client.get_dialogs()
        results = await account_service.check_and_join_chats(client, chats)
    except Exception as e:
        log.error("Ошибка при проверке чатов: %s", e)
        results = [
            {"id": c["id"], "title": c.get("title", c["id"]),
             "username": c.get("username"), "can_write": True, "reason": "ok", "link": None}
            for c in chats
        ]
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    accessible   = [r for r in results if r["can_write"]]
    inaccessible = [r for r in results if not r["can_write"]]

    def _fmt_chat(r: dict) -> str:
        uname = r.get("username")
        title = r.get("title") or (f"@{uname}" if uname else "—")
        link  = f"https://t.me/{uname}" if uname else r.get("link")
        if link:
            return f'<a href="{link}">{html.escape(title)}</a>'
        return html.escape(title)

    if not accessible:
        await state.clear()
        lines = []
        for r in inaccessible[:20]:
            lines.append(f"• {_fmt_chat(r)} — {html.escape(_reason_label(r['reason']))}")
        await query.message.edit_text(
            f"❌ <b>Аккаунт не может писать ни в один чат.</b>\n\n"
            + "\n".join(lines),
            reply_markup=kb_access_error(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    final_chats = [
        {"id": r["id"], "title": r.get("title", ""), "username": r.get("username")}
        for r in accessible
    ]

    # ── Собираем байты фото из FSM ────────────────────────────────────────────
    photos_b64: list[str] = data.get("photo_bytes_b64", [])
    photos_bytes: list[bytes] = [_unb64(b) for b in photos_b64 if b]

    await state.clear()

    task = await task_service.create_task(
        db, user,
        name=data["name"],
        message=data.get("message", ""),
        interval_minutes=data["interval"],
        chats=final_chats,
        preferred_account_id=sender_account_id,
        format_entities=data.get("format_entities", []),
        photo_bytes_list=photos_bytes,   # <-- передаём байты напрямую
    )

    if not task:
        await query.message.edit_text(
            "❌ Не удалось создать задачу. Возможно превышен лимит чатов.",
            reply_markup=kb_back_to_menu(),
        )
        return

    if inaccessible:
        lines = []
        for r in inaccessible[:20]:
            lines.append(f"• {_fmt_chat(r)} — {html.escape(_reason_label(r['reason']))}")
        if len(inaccessible) > 20:
            lines.append(f"…и ещё {len(inaccessible) - 20}")
        await query.message.edit_text(
            f"⚠️ <b>Задача создана частично</b>\n\n"
            f"✅ Доступно: <b>{len(accessible)}</b> из <b>{len(results)}</b>\n\n"
            f"❌ Недоступные:\n" + "\n".join(lines) + "\n\n"
            f"📋 {html.escape(task['name'])}\n"
            f"📬 Чатов: {task['chats_count']}\n"
            f"⏱ Каждые {task['interval_minutes']} мин.",
            reply_markup=kb_back_to_menu(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    preview_lines = [f"• {_fmt_chat(r)}" for r in accessible[:10]]
    if len(accessible) > 10:
        preview_lines.append(f"…и ещё {len(accessible) - 10}")

    media_note = " 📷 фото сохранено" if photos_bytes else ""

    await query.message.edit_text(
        f"✅ <b>Задача создана!</b>{media_note}\n\n"
        f"📋 {html.escape(task['name'])}\n"
        f"📬 Чатов: {task['chats_count']}\n"
        f"⏱ Каждые {task['interval_minutes']} мин.\n\n"
        f"🏷 <b>Чаты рассылки:</b>\n" + "\n".join(preview_lines),
        reply_markup=kb_back_to_menu(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    log.info("Создана задача %d для user %d (фото: %d)", task["id"], user.id, len(photos_bytes))


# ── Вспомогательные функции ───────────────────────────────────────────────────

import base64

def _b64(data: bytes) -> str:
    """bytes → base64-строка для хранения в FSM."""
    return base64.b64encode(data).decode()


def _unb64(s: str) -> bytes:
    """base64-строка → bytes."""
    return base64.b64decode(s)


def _entities_to_json(entities) -> list[dict[str, Any]]:
    if not entities:
        return []
    out = []
    for e in entities:
        d = {"type": e.type, "offset": e.offset, "length": e.length}
        url = getattr(e, "url", None)
        if url:
            d["url"] = url
        out.append(d)
    return out


def _extract_text_and_entities(msg: Message) -> tuple[str, list[dict[str, Any]]]:
    if msg.caption is not None:
        return msg.caption, _entities_to_json(msg.caption_entities)
    return msg.text or "", _entities_to_json(msg.entities)


def _reason_label(reason: str) -> str:
    labels = {
        "private":              "приватный чат",
        "invite_expired":       "invite-ссылка устарела",
        "banned":               "аккаунт заблокирован",
        "write_forbidden":      "нет прав писать",
        "too_many_channels":    "слишком много чатов",
        "join_pending":         "заявка отправлена",
        "not_found":            "чат не найден",
        "invalid_id":           "неверный ID",
        "discussion_no_parent": "нужно вступить в канал вручную",
    }
    return labels.get(reason, reason)

# ── Статистика задачи пользователя ────────────────────────────────────────────

LOGS_PER_PAGE = 20


def _make_message_link(chat_id: str, message_id: int | None) -> str | None:
    """Строим ссылку на отправленное сообщение."""
    if not message_id:
        return None
    s = str(chat_id).strip()
    if s.startswith("@"):
        return f"https://t.me/{s.lstrip('@')}/{message_id}"
    if s.lstrip("-").isdigit():
        raw = str(int(s))
        if raw.startswith("-100"):
            return f"https://t.me/c/{raw[4:]}/{message_id}"
    return None


@router.callback_query(F.data.startswith("tasks:stats:"))
async def task_stats(query: CallbackQuery, user: User, db: AsyncSession):
    """Статистика по задаче: кол-во отправок, успехи, ошибки."""
    from sqlalchemy import func
    from models import Log
    from bot.keyboards import kb_task_stats

    task_id = int(query.data.split(":")[2])
    task = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return

    # Агрегаты
    total_res = await db.execute(
        select(func.count(Log.id)).where(Log.task_id == task_id)
    )
    total: int = total_res.scalar() or 0

    success_res = await db.execute(
        select(func.count(Log.id)).where(Log.task_id == task_id, Log.success == True)
    )
    success_cnt: int = success_res.scalar() or 0

    fail_cnt = total - success_cnt

    # Последние отправки (для превью)
    last_res = await db.execute(
        select(Log)
        .where(Log.task_id == task_id, Log.success == True)
        .order_by(Log.created_at.desc())
        .limit(3)
    )
    recent_logs = last_res.scalars().all()

    recent_lines = []
    for lg in recent_logs:
        link = _make_message_link(lg.chat_id, lg.message_id)
        ts   = lg.created_at.strftime("%d.%m %H:%M") if lg.created_at else "—"
        if link:
            recent_lines.append(f'• <a href="{link}">{html.escape(lg.chat_id)}</a> — {ts}')
        else:
            recent_lines.append(f'• {html.escape(lg.chat_id)} — {ts}')

    recent_block = "\n".join(recent_lines) if recent_lines else "  (нет данных)"

    icon = "▶️" if task.is_active else "⏸"
    text = (
        f"{icon} <b>Статистика: {html.escape(task.name)}</b>\n\n"
        f"📤 Всего отправок: <b>{total}</b>\n"
        f"✅ Успешно: <b>{success_cnt}</b>\n"
        f"❌ Ошибок: <b>{fail_cnt}</b>\n\n"
        f"🕐 <b>Последние доставки:</b>\n{recent_block}"
    )

    await query.message.edit_text(
        text,
        reply_markup=kb_task_stats(task_id, success_cnt > 0),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("tasks:logs:"))
async def task_logs_page(query: CallbackQuery, user: User, db: AsyncSession):
    """Постраничный список ссылок на отправленные сообщения (20 на страницу)."""
    from models import Log
    from bot.keyboards import kb_task_logs_page

    parts   = query.data.split(":")   # tasks:logs:TASK_ID:PAGE
    task_id = int(parts[2])
    page    = int(parts[3]) if len(parts) > 3 else 0

    # Проверка прав
    task = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return

    # Только успешные логи с message_id
    from sqlalchemy import func
    count_res = await db.execute(
        select(func.count(Log.id)).where(
            Log.task_id == task_id,
            Log.success == True,
            Log.message_id.isnot(None),
        )
    )
    linkable_count: int = count_res.scalar() or 0

    total_pages = max(1, -(-linkable_count // LOGS_PER_PAGE))   # ceiling div
    page = max(0, min(page, total_pages - 1))

    logs_res = await db.execute(
        select(Log)
        .where(Log.task_id == task_id, Log.success == True, Log.message_id.isnot(None))
        .order_by(Log.created_at.desc())
        .limit(LOGS_PER_PAGE)
        .offset(page * LOGS_PER_PAGE)
    )
    logs = logs_res.scalars().all()

    lines = []
    for i, lg in enumerate(logs, start=page * LOGS_PER_PAGE + 1):
        link = _make_message_link(lg.chat_id, lg.message_id)
        ts   = lg.created_at.strftime("%d.%m %H:%M") if lg.created_at else "—"
        if link:
            lines.append(f'{i}. <a href="{link}">{html.escape(lg.chat_id)}</a> — {ts}')
        else:
            lines.append(f'{i}. {html.escape(lg.chat_id)} — {ts}')

    if not lines:
        lines = ["  (нет отправок с публичной ссылкой)"]

    text = (
        f"🔗 <b>Отправленные сообщения</b>\n"
        f"Задача: <b>{html.escape(task.name)}</b>\n"
        f"Страница {page + 1} / {total_pages} · всего {linkable_count} ссылок\n\n"
        + "\n".join(lines)
    )

    await query.message.edit_text(
        text,
        reply_markup=kb_task_logs_page(task_id, page, total_pages),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# noop handler for page indicator button
@router.callback_query(F.data == "noop")
async def cb_noop(query: CallbackQuery):
    await query.answer()
