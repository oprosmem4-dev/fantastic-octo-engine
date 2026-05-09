"""
bot/handlers/tasks.py — создание и управление задачами рассылок.

ИЗМЕНЕНИЯ:
  - view_task: чаты показываются как гиперссылки "@username" или просто названия,
    числовые ID нигде не отображаются пользователю.
  - got_task_chats: для папок передаём полные данные (username, folder_slug).
  - confirm_chats: недоступные чаты показываются с названием/ссылкой, без ID.
"""
import logging
import json
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any

from models import User
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
        f"👋 Главное меню\n{user.subscription_status}",
        reply_markup=kb_main_menu(user.has_access),
        parse_mode="Markdown",
    )


# ── Утилита: отображаемое имя чата ───────────────────────────────────────────

def _chat_display(title: str, username: str | None, link: str | None) -> str:
    """
    Формирует строку для отображения чата в боте:
      • Если есть username → гиперссылка [title](https://t.me/username)
      • Если есть link     → гиперссылка [title](link)
      • Иначе              → просто title
    """
    url = f"https://t.me/{username}" if username else link
    if url:
        return f"[{title}]({url})"
    return title


def _chat_display_from_task_chat(c) -> str:
    """
    Отображение чата из объекта TaskChat.
    chat_title хранится как "@username" или просто название.
    """
    ct = c.chat_title or ""
    if ct.startswith("@"):
        uname = ct.lstrip("@")
        return f"[{ct}](https://t.me/{uname})"
    return ct or c.chat_id  # числовой ID только если совсем нет данных


# ── Список задач ──────────────────────────────────────────────────────────────

@router.message(Command("tasks"))
async def cmd_tasks(message: Message, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
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

    # Список чатов — показываем названия/ссылки, БЕЗ числовых ID
    chats_lines = []
    for c in task.chats[:15]:
        display = _chat_display_from_task_chat(c)
        status  = "" if c.is_ok else " ⚠️"
        chats_lines.append(f"• {display}{status}")
    chats_block = "\n".join(chats_lines) if chats_lines else "—"
    if len(task.chats) > 15:
        chats_block += f"\n…и ещё {len(task.chats) - 15}"

    # Аккаунты — показываем телефон и количество чатов
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
        acc_lines.append(f"• {acc_name}: {len(ids)} чатов")

    accounts_block = "\n".join(acc_lines) if acc_lines else "—"

    text = (
        f"{icon} *{task.name}*\n\n"
        f"💬 Сообщение:\n_{task.message[:200]}_\n\n"
        f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
        f"📬 Чатов: {len(task.chats)}\n"
        f"🤖 Аккаунтов: {len(task.accounts)}\n\n"
        f"🏷 *Чаты рассылки:*\n{chats_block}\n\n"
        f"👤 *Распределение:*\n{accounts_block}"
    )

    await query.message.edit_text(
        text,
        reply_markup=kb_task_detail(task),
        parse_mode="Markdown",
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
            f"{icon} *{task.name}*\n\n"
            f"💬 Сообщение:\n_{task.message[:200]}_\n\n"
            f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
            f"📬 Чатов: {len(task.chats)}"
        )
        await query.message.edit_text(
            text, reply_markup=kb_task_detail(task), parse_mode="Markdown"
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
        f"⚠️ Удалить задачу *{task.name}*?\n\nЭто действие нельзя отменить.",
        reply_markup=kb_task_delete_confirm(task_id),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("tasks:confirm_delete:"))
async def confirm_delete_task(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    await state.clear()
    task_id = int(query.data.split(":")[2])
    deleted = await task_service.delete_task(db, task_id, user.id)
    await query.answer("✅ Задача удалена." if deleted else "❌ Не найдено.", show_alert=not deleted)
    tasks = await task_service.get_tasks(db, user.id)
    text  = "📋 *Ваши задачи*" if tasks else "📋 У вас пока нет задач."
    await query.message.edit_text(text, reply_markup=kb_tasks(tasks), parse_mode="Markdown")


# ── Создание задачи (FSM) ─────────────────────────────────────────────────────

@router.callback_query(F.data == "tasks:new")
async def cb_new_task(query: CallbackQuery, state: FSMContext, user: User):
    await state.clear()
    if not user.has_access:
        await query.answer("⚠️ Нужна активная подписка.", show_alert=True)
        return
    await query.message.edit_text(
        "➕ *Новая задача рассылки*\n\n"
        "*Шаг 1/4* — Введите название задачи:\n"
        "Например: `Реклама магазина`",
        reply_markup=kb_cancel(),
        parse_mode="Markdown",
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
        parse_mode="Markdown",
    )
    await state.set_state(CreateTask.name)


@router.message(CreateTask.name)
async def got_task_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer(
        "*Шаг 2/4* — Введите текст сообщения для рассылки:",
        reply_markup=kb_cancel(),
        parse_mode="Markdown",
    )
    await state.set_state(CreateTask.message)


@router.message(CreateTask.message)
async def got_task_message(message: Message, state: FSMContext):
    text, entities_json = _extract_text_and_entities(message)

    photo_file_ids: list[str] = []
    if message.photo:
        photo_file_ids.append(message.photo[-1].file_id)

    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        data = await state.get_data()
        mg   = data.get("media_group", {"id": media_group_id, "photos": [], "text": "", "entities": []})
        if mg.get("id") != media_group_id:
            mg = {"id": media_group_id, "photos": [], "text": "", "entities": []}
        if message.photo:
            mg["photos"].append(message.photo[-1].file_id)
        if text:
            mg["text"]     = text
            mg["entities"] = entities_json
        await state.update_data(media_group=mg)
        if len(mg["photos"]) > 5:
            await state.update_data(media_group=None)
            await message.answer("❌ Максимум 5 фото. Пришлите заново:")
            return
        await message.answer(f"📸 Принял фото: {len(mg['photos'])}/5. Добавьте ещё или отправьте 'ок'.")
        return

    if len(photo_file_ids) > 5:
        await message.answer("❌ Максимум 5 фото.")
        return

    data = await state.get_data()
    mg   = data.get("media_group")
    if (message.text or "").strip().lower() in {"ок", "ok", "да", "done"} and mg and mg.get("photos"):
        text           = mg.get("text", "")
        entities_json  = mg.get("entities", [])
        photo_file_ids = mg.get("photos", [])
        await state.update_data(media_group=None)

    if not text and not photo_file_ids:
        await message.answer("❌ Пришлите текст или фото (до 5) с подписью.")
        return

    await state.update_data(
        message=text,
        format_entities=entities_json,
        photo_file_ids=photo_file_ids,
    )
    await message.answer(
        "*Шаг 3/4* — Введите интервал в минутах:\n\n"
        "Минимум: *3 минут*\n"
        "⚠️ Рекомендуем не менее 15 минут\n"
        "Пример: `60` = каждый час",
        reply_markup=kb_cancel(),
        parse_mode="Markdown",
    )
    await state.set_state(CreateTask.interval)


@router.message(CreateTask.interval)
async def got_task_interval(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) < 3:
        await message.answer("❌ Минимум 3 минут. Введите число ≥ 3:")
        return
    await state.update_data(interval=int(text))
    await message.answer(
        "*Шаг 4/4* — Введите чаты:\n\n"
        "Вариант 1 — ссылка на папку:\n`https://t.me/addlist/XXXX`\n\n"
        "Вариант 2 — список через новую строку:\n"
        "`@username`\n`-1001234567890`",
        reply_markup=kb_cancel(),
        parse_mode="Markdown",
    )
    await state.set_state(CreateTask.chats)


@router.message(CreateTask.chats)
async def got_task_chats(message: Message, state: FSMContext, user: User, db: AsyncSession):
    raw   = message.text.strip()
    chats = []

    if raw.startswith("https://t.me/addlist/"):
        # ── Папка ─────────────────────────────────────────────────────────────
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
                f"❌ Не удалось получить чаты из папки.\n`{e}`\n\nПопробуйте ввести вручную:",
                parse_mode="Markdown",
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
                "Убедитесь что ссылка вида `https://t.me/addlist/XXXX`\n\n"
                "Попробуйте ввести чаты вручную:",
                parse_mode="Markdown",
            )
            return

    else:
        # ── Ручной ввод ───────────────────────────────────────────────────────
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            username = None
            if line.startswith("@"):
                username = line.lstrip("@")
                chat_id  = f"@{username}"
            elif line.lstrip("-").isdigit():
                chat_id = line
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

    # Превью — показываем названия/username, без числовых ID
    preview_lines = []
    for c in chats[:10]:
        uname = c.get("username")
        title = c.get("title") or (f"@{uname}" if uname else c["id"])
        preview_lines.append(f"• {title}")
    preview = "\n".join(preview_lines)
    if len(chats) > 10:
        preview += f"\n...и ещё {len(chats) - 10}"

    accounts = await account_service.get_accounts(db, owner_id=user.id)

    await message.answer(
        f"✅ Найдено чатов: *{len(chats)}*\n\n"
        f"{preview}\n\n"
        f"*Шаг 5/5* — Выберите отправителя:",
        reply_markup=kb_choose_sender(accounts),
        parse_mode="Markdown",
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

    await query.message.edit_text(
        f"✅ Отправитель: *{sender_text}*\n\n"
        f"📋 Задача: *{data['name']}*\n"
        f"📬 Чатов: *{len(chats)}*\n"
        f"⏱ Каждые {data['interval']} мин.\n\n"
        f"Нажмите *Продолжить* для создания задачи:",
        reply_markup=kb_confirm_chats(),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "tasks:confirm_chats")
async def confirm_chats(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    data  = await state.get_data()
    chats = data.get("chats", [])
    if not chats:
        await query.answer("❌ Чаты не найдены.", show_alert=True)
        return

    sender_account_id = data.get("sender_account_id")

    # Аккаунт для проверки доступа
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
        """Отформатировать строку чата без числового ID."""
        uname = r.get("username")
        title = r.get("title") or (f"@{uname}" if uname else "—")
        link  = f"https://t.me/{uname}" if uname else r.get("link")
        if link:
            return f"[{title}]({link})"
        return title

    # Все недоступны
    if not accessible:
        await state.clear()
        lines = []
        for r in inaccessible[:20]:
            lines.append(f"• {_fmt_chat(r)} — {_reason_label(r['reason'])}")
        await query.message.edit_text(
            f"❌ *Аккаунт не может писать ни в один чат.*\n\n"
            + "\n".join(lines),
            reply_markup=kb_access_error(),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    await state.clear()

    final_chats = [
        {"id": r["id"], "title": r.get("title", ""), "username": r.get("username")}
        for r in accessible
    ]

    task = await task_service.create_task(
        db, user,
        name=data["name"],
        message=data.get("message", ""),
        interval_minutes=data["interval"],
        chats=final_chats,
        preferred_account_id=sender_account_id,
        photo_file_ids=data.get("photo_file_ids", []),
        format_entities=data.get("format_entities", []),
    )

    if not task:
        await query.message.edit_text(
            "❌ Не удалось создать задачу. Возможно превышен лимит чатов.",
            reply_markup=kb_back_to_menu(),
        )
        return

    # Часть недоступна
    if inaccessible:
        lines = []
        for r in inaccessible[:20]:
            lines.append(f"• {_fmt_chat(r)} — {_reason_label(r['reason'])}")
        if len(inaccessible) > 20:
            lines.append(f"…и ещё {len(inaccessible) - 20}")
        await query.message.edit_text(
            f"⚠️ *Задача создана частично*\n\n"
            f"✅ Доступно: *{len(accessible)}* из *{len(results)}*\n\n"
            f"❌ Недоступные:\n" + "\n".join(lines) + "\n\n"
            f"📋 {task['name']}\n"
            f"📬 Чатов: {task['chats_count']}\n"
            f"⏱ Каждые {task['interval_minutes']} мин.",
            reply_markup=kb_back_to_menu(),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    # Всё OK
    preview_lines = [f"• {_fmt_chat(r)}" for r in accessible[:10]]
    if len(accessible) > 10:
        preview_lines.append(f"…и ещё {len(accessible) - 10}")

    await query.message.edit_text(
        f"✅ *Задача создана!*\n\n"
        f"📋 {task['name']}\n"
        f"📬 Чатов: {task['chats_count']}\n"
        f"⏱ Каждые {task['interval_minutes']} мин.\n\n"
        f"🏷 *Чаты рассылки:*\n" + "\n".join(preview_lines),
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    log.info("Создана задача %d для user %d", task["id"], user.id)


# ── Вспомогательные функции ───────────────────────────────────────────────────

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
