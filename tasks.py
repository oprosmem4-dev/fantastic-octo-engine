"""
bot/handlers/tasks.py — создание и управление задачами рассылок.

НОВОЕ: хендлеры tasks:transfer_start и tasks:transfer_pick —
перенос задачи на другой аккаунт после уведомления об ограничении.
"""
import logging
import json
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any
from models import User, Account, TaskAccount
from services import task_service, account_service
from bot.keyboards import (
    kb_tasks, kb_task_detail, kb_task_delete_confirm,
    kb_cancel, kb_back_to_menu, kb_confirm_chats,
    kb_choose_sender, kb_access_error
)

log = logging.getLogger(__name__)
router = Router()


# ── FSM состояния ─────────────────────────────────────────────────────────────
class CreateTask(StatesGroup):
    name     = State()
    message  = State()
    interval = State()
    chats    = State()
    sender   = State()


# ── Отмена FSM — ДОЛЖНА БЫТЬ ПЕРВОЙ ──────────────────────────────────────────

@router.callback_query(F.data == "menu:new")
async def cb_cancel_to_menu(query: CallbackQuery, state: FSMContext, user: User):
    """Отмена FSM — сбрасываем состояние и отправляем НОВОЕ сообщение меню."""
    current = await state.get_state()
    if current:
        await state.clear()
    from bot.keyboards import kb_main_menu
    await query.answer()
    await query.message.answer(
        f"👋 Главное меню\n{user.subscription_status}",
        reply_markup=kb_main_menu(user.has_access),
        parse_mode="Markdown"
    )


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

    chats_lines = []
    for c in task.chats[:15]:
        title = (c.chat_title or c.chat_id).strip()
        status = "" if c.is_ok else " (⚠️ проблемы)"
        chats_lines.append(f"• {title} `{c.chat_id}`{status}")
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
        if acc:
            acc_name = acc.phone
            if acc.is_system:
                acc_name += " (system)"
            # Показываем статус если есть проблемы
            if acc.status != "ok":
                acc_name += f" {acc.status_icon}"
        else:
            acc_name = f"account_id={link.account_id}"

        shown = ids[:10]
        tail = f" …+{len(ids) - 10}" if len(ids) > 10 else ""
        chats_for_acc = ", ".join(f"`{x}`" for x in shown) + tail if ids else "—"
        acc_lines.append(f"• {acc_name}: {chats_for_acc}")

    accounts_block = "\n".join(acc_lines) if acc_lines else "—"

    text = (
        f"{icon} *{task.name}*\n\n"
        f"💬 Сообщение:\n_{task.message[:200]}_\n\n"
        f"⏱ Интервал: каждые {task.interval_minutes} мин.\n"
        f"📬 Чатов: {len(task.chats)}\n"
        f"🤖 Аккаунтов: {len(task.accounts)}\n\n"
        f"🏷 *Чаты рассылки:*\n{chats_block}\n\n"
        f"👤 *Распределение по аккаунтам:*\n{accounts_block}"
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
    await state.clear()
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
    text, entities_json = _extract_text_and_entities(message)

    photo_file_ids: list[str] = []
    if message.photo:
        photo_file_ids.append(message.photo[-1].file_id)

    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        data = await state.get_data()
        mg = data.get("media_group", {"id": media_group_id, "photos": [], "text": "", "entities": []})

        if mg.get("id") != media_group_id:
            mg = {"id": media_group_id, "photos": [], "text": "", "entities": []}

        if message.photo:
            mg["photos"].append(message.photo[-1].file_id)

        if text:
            mg["text"] = text
            mg["entities"] = entities_json

        await state.update_data(media_group=mg)

        if len(mg["photos"]) > 5:
            await state.update_data(media_group=None)
            await message.answer("❌ Фоток должно быть <= 5. Пришлите альбом заново или текст без фото.")
            return

        await message.answer(
            f"📸 Принял фото: {len(mg['photos'])}/5.\n"
            f"Если это всё — отправьте 'ок' или добавьте ещё фото."
        )
        return

    if len(photo_file_ids) > 5:
        await message.answer("❌ Фоток должно быть <= 5.")
        return

    data = await state.get_data()
    mg = data.get("media_group")
    if (message.text or "").strip().lower() in {"ок", "ok", "да", "done"} and mg and mg.get("photos"):
        text = mg.get("text", "")
        entities_json = mg.get("entities", [])
        photo_file_ids = mg.get("photos", [])
        await state.update_data(media_group=None)

    if not text and not photo_file_ids:
        await message.answer("❌ Пришлите текст сообщения или фото (до 5) с подписью.")
        return

    await state.update_data(
        message=text,
        format_entities=entities_json,
        photo_file_ids=photo_file_ids,
    )

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

    await state.update_data(chats=chats)

    preview = "\n".join(f"• {c['title']}" for c in chats[:10])
    if len(chats) > 10:
        preview += f"\n... и ещё {len(chats) - 10}"

    accounts = await account_service.get_accounts(db, owner_id=user.id)

    await message.answer(
        f"✅ Найдено чатов: *{len(chats)}*\n\n"
        f"{preview}\n\n"
        f"*Шаг 5/5* — Выберите, кто будет отправлять сообщения:",
        reply_markup=kb_choose_sender(accounts),
        parse_mode="Markdown"
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
        acc = await account_service.get_account_by_id(db, account_id)
        sender_text = f"👤 {acc.phone}" if acc else "👤 Выбранный аккаунт"

    data = await state.get_data()
    chats = data.get("chats", [])

    await query.message.edit_text(
        f"✅ Отправитель: *{sender_text}*\n\n"
        f"📋 Задача: *{data['name']}*\n"
        f"📬 Чатов: *{len(chats)}*\n"
        f"⏱ Каждые {data['interval']} мин.\n\n"
        f"Нажмите *Продолжить* для создания задачи:",
        reply_markup=kb_confirm_chats(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "tasks:confirm_chats")
async def confirm_chats(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    data = await state.get_data()
    chats = data.get("chats", [])
    if not chats:
        await query.answer("❌ Чаты не найдены.", show_alert=True)
        return

    sender_account_id = data.get("sender_account_id")
    check_account = None
    if sender_account_id is not None:
        check_account = await account_service.get_account_by_id(db, sender_account_id)
    else:
        accounts = await account_service.get_accounts(db, owner_id=user.id)
        if not accounts:
            accounts = await account_service.get_accounts(db)
        if accounts:
            check_account = accounts[0]

    if check_account is None:
        await state.clear()
        await query.message.edit_text(
            "❌ Нет доступных аккаунтов для проверки.\nДобавьте аккаунт в /accounts",
            reply_markup=kb_back_to_menu()
        )
        return

    await query.message.edit_text(
        f"🔍 Проверяю доступ к {len(chats)} чатам...\nЭто может занять несколько секунд.",
        parse_mode="Markdown"
    )

    client = account_service.make_client(check_account)
    try:
        await client.connect()
        results = await account_service.check_and_join_chats(client, chats)
    except Exception as e:
        log.error("Ошибка при проверке доступа к чатам: %s", e)
        results = [{"id": c["id"], "title": c.get("title", c["id"]),
                    "can_write": True, "reason": "ok", "link": None} for c in chats]
    finally:
        await client.disconnect()

    accessible   = [r for r in results if r["can_write"]]
    inaccessible = [r for r in results if not r["can_write"]]

    if not accessible:
        await state.clear()
        lines = []
        for r in inaccessible[:20]:
            reason_text = _reason_label(r["reason"])
            link_part = f" — [ссылка]({r['link']})" if r.get("link") else ""
            lines.append(f"• {r['title']} — {reason_text}{link_part}")
        inaccessible_list = "\n".join(lines)
        await query.message.edit_text(
            f"❌ *Аккаунт не может писать ни в один из указанных чатов.*\n\n"
            f"Причины:\n{inaccessible_list}",
            reply_markup=kb_access_error(),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    await state.clear()

    final_chats = [{"id": r["id"], "title": r["title"]} for r in accessible]

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
            "❌ Не удалось создать задачу.\nВозможно превышен лимит чатов.",
            reply_markup=kb_back_to_menu()
        )
        return

    if inaccessible:
        lines = []
        for r in inaccessible[:20]:
            reason_text = _reason_label(r["reason"])
            link_part = f" — [ссылка]({r['link']})" if r.get("link") else ""
            lines.append(f"• {r['title']} — {reason_text}{link_part}")
        if len(inaccessible) > 20:
            lines.append(f"... и ещё {len(inaccessible) - 20}")
        inaccessible_list = "\n".join(lines)
        await query.message.edit_text(
            f"⚠️ *Задача создана частично*\n\n"
            f"✅ Доступно: *{len(accessible)}* из *{len(results)}* чатов\n\n"
            f"❌ Недоступные чаты:\n{inaccessible_list}\n\n"
            f"📋 {task['name']}\n"
            f"📬 Чатов: {task['chats_count']}\n"
            f"⏱ Каждые {task['interval_minutes']} мин.",
            reply_markup=kb_back_to_menu(),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    preview = "\n".join(f"• {c.get('title') or c.get('id')}" for c in chats[:10])
    if len(chats) > 10:
        preview += f"\n…и ещё {len(chats) - 10}"

    await query.message.edit_text(
        f"✅ *Задача создана!*\n\n"
        f"📋 {task['name']}\n"
        f"📬 Чатов: {task['chats_count']}\n"
        f"⏱ Каждые {task['interval_minutes']} мин.\n\n"
        f"🏷 *Чаты рассылки:*\n{preview}",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )
    log.info("Создана задача %d для user %d", task['id'], user.id)


# ─────────────────────────────────────────────────────────────────────────────
# ПЕРЕНОС ЗАДАЧИ НА ДРУГОЙ АККАУНТ (вызывается из уведомления об ограничении)
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("tasks:transfer_start:"))
async def transfer_task_start(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    """
    Шаг 1: пользователь нажал "Перенести рассылку".
    Проверяем can_write_to_chat для каждого аккаунта ПЕРЕД показом списка.
    callback_data: tasks:transfer_start:{task_id}:{chat_id}
    """
    parts = query.data.split(":", 3)
    if len(parts) < 4:
        await query.answer("Ошибка: неверный формат.", show_alert=True)
        return

    task_id = int(parts[2])
    chat_id = parts[3]

    task = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return

    await query.answer()
    # Сообщаем что идёт проверка — это может занять время
    checking_msg = await query.message.answer(
        "🔍 Проверяю доступ аккаунтов к чату...",
        parse_mode="Markdown",
    )

    personal_accounts = await account_service.get_accounts(db, owner_id=user.id)
    personal_ok = [a for a in personal_accounts if a.status == "ok"]

    # Проверяем каждый личный аккаунт через can_write_to_chat
    available_personal = []
    for acc in personal_ok:
        client = account_service.make_client(acc)
        try:
            await client.connect()
            can_write, reason = await account_service.can_write_to_chat(client, chat_id)
            if can_write:
                available_personal.append(acc)
        except Exception:
            pass
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    # Для системных — просто показываем кнопку, проверка будет при выборе
    # (там слишком много аккаунтов чтобы проверять все заранее)

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🤖 Системные аккаунты",
        callback_data=f"tasks:transfer_pick:system:{task_id}:{chat_id}",
    )
    for acc in available_personal:
        builder.button(
            text=f"✅ {acc.phone}",
            callback_data=f"tasks:transfer_pick:acc:{acc.id}:{task_id}:{chat_id}",
        )
    builder.button(text="❌ Отмена", callback_data="menu:new")
    builder.adjust(1)

    # Удаляем сообщение "проверяю..."
    try:
        await checking_msg.delete()
    except Exception:
        pass

    no_personal_note = ""
    if personal_ok and not available_personal:
        no_personal_note = "\n\n⚠️ Ни один из ваших личных аккаунтов не может писать в этот чат."
    elif not personal_ok:
        no_personal_note = "\n\n💡 У вас нет личных аккаунтов. Можно добавить через /accounts."

    await query.message.answer(
        f"🔄 *Перенос рассылки*\n\n"
        f"Задача: *{task.name}*\n"
        f"Чат: `{chat_id}`\n\n"
        f"Выберите аккаунт для продолжения рассылки:{no_personal_note}",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("tasks:transfer_pick:"))
async def transfer_task_pick(query: CallbackQuery, state: FSMContext, user: User, db: AsyncSession):
    """
    Шаг 2: пользователь выбрал аккаунт.
    - Проверяем can_write_to_chat для выбранного аккаунта.
    - Если не может писать — показываем ошибку и возвращаем к списку выбора.
    - Если может — переносим все чаты и запускаем задачу.

    Форматы callback_data:
      tasks:transfer_pick:system:{task_id}:{chat_id}
      tasks:transfer_pick:acc:{account_id}:{task_id}:{chat_id}
    """
    parts = query.data.split(":")

    if parts[2] == "system":
        task_id  = int(parts[3])
        chat_id_check = parts[4]
        use_system = True
        new_account_id = None
    else:
        # tasks:transfer_pick:acc:{account_id}:{task_id}:{chat_id}
        new_account_id = int(parts[3])
        task_id = int(parts[4])
        chat_id_check = parts[5]
        use_system = False

    await state.clear()

    task = await task_service.get_task(db, task_id, user.id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return

    # ── Вспомогательная функция: показать список выбора заново ───────────────
    async def _back_to_account_list(error_text: str):
        """Показать ошибку и вернуть к выбору аккаунта новым сообщением."""
        await query.answer()

        personal_accounts = await account_service.get_accounts(db, owner_id=user.id)
        personal_ok = [a for a in personal_accounts if a.status == "ok"]

        # Перепроверяем личные аккаунты (быстро — уже отфильтрованы по статусу)
        available_personal = []
        for acc in personal_ok:
            client = account_service.make_client(acc)
            try:
                await client.connect()
                can_write, _ = await account_service.can_write_to_chat(client, chat_id_check)
                if can_write:
                    available_personal.append(acc)
            except Exception:
                pass
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        from aiogram.utils.keyboard import InlineKeyboardBuilder
        builder = InlineKeyboardBuilder()
        builder.button(
            text="🤖 Системные аккаунты",
            callback_data=f"tasks:transfer_pick:system:{task_id}:{chat_id_check}",
        )
        for acc in available_personal:
            builder.button(
                text=f"✅ {acc.phone}",
                callback_data=f"tasks:transfer_pick:acc:{acc.id}:{task_id}:{chat_id_check}",
            )
        builder.button(text="❌ Отмена", callback_data="menu:new")
        builder.adjust(1)

        await query.message.answer(
            f"❌ {error_text}\n\n"
            f"Выберите другой аккаунт:",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
        )

    # ── Определяем и проверяем новый аккаунт ─────────────────────────────────
    if use_system:
        # Берём системные по порядку нагрузки, ищем первый который может писать
        result = await db.execute(
            select(Account).where(
                Account.is_system == True,
                Account.is_active == True,
                Account.is_banned == False,
                Account.status == "ok",
            ).order_by(Account.chats_count.asc())
        )
        system_accounts = result.scalars().all()

        if not system_accounts:
            await _back_to_account_list(
                "Нет доступных системных аккаунтов.\n"
                "Попробуйте добавить личный аккаунт через /accounts."
            )
            return

        # Проверяем каждый системный пока не найдём рабочий
        await query.answer()
        checking_msg = await query.message.answer(
            "🔍 Проверяю системные аккаунты..."
        )

        new_acc = None
        for acc in system_accounts:
            client = account_service.make_client(acc)
            try:
                await client.connect()
                can_write, reason = await account_service.can_write_to_chat(client, chat_id_check)
                if can_write:
                    new_acc = acc
                    break
            except Exception:
                pass
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        try:
            await checking_msg.delete()
        except Exception:
            pass

        if not new_acc:
            await query.message.answer(
                "❌ *Ни один системный аккаунт не может писать в этот чат.*\n\n"
                "💡 Добавьте личный аккаунт через /accounts и попробуйте снова.",
                reply_markup=kb_back_to_menu(),
                parse_mode="Markdown",
            )
            return

    else:
        new_acc = await account_service.get_account_by_id(db, new_account_id)
        if not new_acc or new_acc.owner_id != user.id:
            await query.answer("Аккаунт не найден.", show_alert=True)
            return
        if new_acc.status != "ok":
            await _back_to_account_list(
                f"Аккаунт `{new_acc.phone}` недоступен: {new_acc.status_label}"
            )
            return

        # Проверяем может ли этот личный аккаунт писать в чат
        await query.answer()
        checking_msg = await query.message.answer(
            f"🔍 Проверяю аккаунт {new_acc.phone}..."
        )
        client = account_service.make_client(new_acc)
        can_write = False
        fail_reason = ""
        try:
            await client.connect()
            can_write, fail_reason = await account_service.can_write_to_chat(client, chat_id_check)
        except Exception as e:
            fail_reason = str(e)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        try:
            await checking_msg.delete()
        except Exception:
            pass

        if not can_write:
            reason_labels = {
                "banned":          "аккаунт заблокирован администратором чата",
                "write_forbidden": "нет прав на отправку сообщений",
                "kicked":          "аккаунт исключён из чата",
                "private":         "чат приватный",
                "not_found":       "чат не найден",
            }
            reason_text = reason_labels.get(fail_reason, fail_reason)
            await _back_to_account_list(
                f"Аккаунт `{new_acc.phone}` не может писать в этот чат.\n"
                f"Причина: {reason_text}"
            )
            return

    # Собираем все chat_id задачи из всех TaskAccount
    result = await db.execute(
        select(TaskAccount).where(TaskAccount.task_id == task_id)
    )
    old_task_accounts = result.scalars().all()

    all_chat_ids: list[str] = []
    for ta in old_task_accounts:
        try:
            ids = json.loads(ta.chat_ids or "[]")
            all_chat_ids.extend([str(x) for x in ids])
        except Exception:
            pass
        # Уменьшаем счётчик старого аккаунта
        result2 = await db.execute(
            select(Account).where(Account.id == ta.account_id)
        )
        old_acc_obj = result2.scalar_one_or_none()
        if old_acc_obj:
            old_acc_obj.chats_count = max(0, old_acc_obj.chats_count - len(
                json.loads(ta.chat_ids or "[]")
            ))
        await db.delete(ta)

    # Убираем дубли, сохраняем порядок
    all_chat_ids = list(dict.fromkeys(all_chat_ids))

    if not all_chat_ids:
        await query.message.answer(
            "⚠️ В задаче не осталось чатов.",
            reply_markup=kb_back_to_menu(),
        )
        return

    # Создаём новый TaskAccount
    db.add(TaskAccount(
        task_id=task_id,
        account_id=new_acc.id,
        chat_ids=json.dumps(all_chat_ids),
    ))
    new_acc.chats_count += len(all_chat_ids)

    # Включаем задачу
    task.is_active = True
    await db.commit()

    acc_display = f"`{new_acc.phone}`"
    if new_acc.is_system:
        acc_display += " _(системный)_"

    await query.answer()
    await query.message.answer(
        f"✅ *Рассылка перенесена и запущена*\n\n"
        f"Задача: *{task.name}*\n"
        f"Аккаунт: {acc_display}\n"
        f"Чатов: *{len(all_chat_ids)}*\n\n"
        f"Управление задачей: /tasks",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown",
    )
    log.info(
        "Пользователь %d перенёс задачу %d на аккаунт %s (%d чатов)",
        user.id, task_id, new_acc.phone, len(all_chat_ids),
    )


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _entities_to_json(entities) -> list[dict[str, Any]]:
    if not entities:
        return []
    out: list[dict[str, Any]] = []
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
        "private":            "приватный чат (нет ссылки)",
        "private_no_link":    "приватный чат (нет invite-ссылки)",
        "invite_expired":     "invite-ссылка устарела",
        "banned":             "аккаунт заблокирован в чате",
        "write_forbidden":    "нет прав писать",
        "no_send_permission": "нет разрешения отправлять",
        "broadcast_channel":  "broadcast-канал (только админы пишут)",
        "too_many_channels":  "аккаунт состоит в слишком многих чатах",
        "join_pending":       "заявка на вступление отправлена",
        "not_found":          "чат не найден",
        "invalid_id":         "неверный ID чата",
    }
    return labels.get(reason, reason)
