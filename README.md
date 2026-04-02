# fantastic-octo-engine
TG SaaS — Telegram Broadcast Service


Сервис для автоматической рассылки сообщений в Telegram-чаты через пул аккаунтов. Подписочная модель с триалом, зеркальными ботами и несколькими способами оплаты.



Текущее состояние проекта


Проект в активной разработке. Основной функционал реализован и работает.


✅ Работает


Регистрация пользователей с автоматическим триалом (1 день)
Добавление системных аккаунтов через /admin → Аккаунты → Добавить системный
Создание задач рассылки (FSM-wizard: название → текст → интервал → чаты)
Управление задачами (запуск/остановка/удаление)
Воркер рассылок (APScheduler, динамическая загрузка задач из БД каждые 30 сек)
Подписка через Telegram Stars
Админ-команды: /giveday, /block, /unblock, /setlimit, /userinfo
Уведомление админа о новых пользователях


🔧 Частично работает


CryptoBot — логика есть, нужно настроить webhook и токен в .env
TON — ручная проверка, автоматизация не реализована
Зеркальные боты — mirror_runner.py запускается отдельно
Папки t.me/addlist/ — реализовано, не тестировалось


❌ Не реализовано


Автоматическая проверка TON транзакций
Статистика отправок для пользователя
Уведомления об ошибках рассылки пользователю



Стек




Компонент
Технология




Бот
Python 3.12, aiogram 3.7


Telegram аккаунты
Telethon 1.36 (StringSession)


База данных
PostgreSQL + SQLAlchemy 2.0 async


Планировщик
APScheduler 3.10


API
FastAPI + uvicorn





Структура проекта


tg-saas/
├── config.py                  # Все настройки из .env
├── database.py                # async engine + SessionLocal
│
├── models/__init__.py         # Все ORM модели (см. раздел Models)
│
├── services/
│   ├── user_service.py        # get_or_create_user, add_subscription, block/unblock
│   ├── account_service.py     # Telethon: send_code, sign_in, make_client
│   ├── task_service.py        # create_task (возвращает dict!), get_tasks, toggle, delete
│   └── payment_service.py     # create_payment, confirm_payment, cryptobot_invoice
│
├── bot/
│   ├── middlewares.py         # AuthMiddleware — user+db в каждый handler
│   ├── keyboards.py           # Все InlineKeyboardMarkup
│   ├── main_bot.py            # Точка входа главного бота
│   ├── mirror_runner.py       # Запуск зеркал из БД каждые 60 сек
│   └── handlers/
│       ├── start.py           # /start, /help, menu callback, status callback
│       ├── accounts.py        # FSM добавления аккаунта (5 шагов)
│       ├── tasks.py           # FSM создания задачи (4 шага) + управление
│       ├── payment.py         # Stars, CryptoBot, TON
│       ├── admin.py           # /admin, FSM системного аккаунта, команды
│       └── mirror.py          # Зеркальный бот
│
├── worker/worker.py           # APScheduler: sync_tasks→run_task→send_to_chat
├── api/app.py                 # FastAPI: /webhook/cryptobot, /health
├── migrations/env.py          # Alembic
├── docker-compose.yml
└── .env.example




Модели БД (models/init.py)


User


id (BigInteger PK) — Telegram ID
username, full_name
is_blocked, is_admin
trial_ends_at, sub_ends_at
max_chats (default 100)
created_at



Свойства: has_access (bool), subscription_status (str)


Account


id, owner_id (FK users, nullable)
phone, api_id, api_hash
session_string (Telethon StringSession)
is_active, is_banned
is_system   — True = системный (owner_id=None, доступен всем пользователям)
chats_count — текущая нагрузка



Task


id, user_id (FK users)
name, message, interval_minutes
is_active, last_run_at
→ chats:    list[TaskChat]
→ accounts: list[TaskAccount]



TaskChat


id, task_id, chat_id (str), chat_title, is_ok



TaskAccount


id, task_id, account_id
chat_ids — JSON строка, список chat_id для этого аккаунта



Payment


id, user_id, method (stars|cryptobot|ton)
plan (1week|1month|3month|6month)
amount, currency, status (pending|paid|failed)
external_id, created_at, paid_at



MirrorBot


id, user_id (unique), token, bot_username, is_active



Log


id, task_id, account_id, chat_id, success (bool), error, created_at




Важные особенности кода


1. create_task возвращает dict, не ORM объект


После commit() обращение к lazy-loaded полям вызывает MissingGreenlet.
Поэтому task_service.create_task возвращает:


{"id": ..., "name": ..., "chats_count": ..., "interval_minutes": ...}



В handlers обращаться через task["name"], не task.name.


2. get_task / get_tasks используют selectinload


select(Task).options(selectinload(Task.chats), selectinload(Task.accounts))



3. AuthMiddleware


Каждый handler получает user: User и db: AsyncSession автоматически.
Сессия открыта на время одного запроса.


4. Системные аккаунты


is_system=True, owner_id=None — доступны всем.
_distribute_chats() берёт: личные аккаунты пользователя ИЛИ системные.
Добавляются через /admin → Аккаунты → Добавить системный.


5. FSM в tasks.py


Отмена (menu callback) делает state.clear() — иначе следующие сообщения
попадают в FSM. Обработчик cb_cancel_to_menu объявлен первым в роутере.


6. Воркер


sync_tasks() каждые 30 сек синхронизирует APScheduler с БД.
Добавляет новые jobs, удаляет неактивные. Перезапуск не нужен.



Переменные окружения


# Обязательные
BOT_TOKEN=
OWNER_ID=
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/tgsaas
API_SECRET=

# Опциональные
CRYPTOBOT_TOKEN=
CRYPTOBOT_WEBHOOK_SECRET=
TON_WALLET=
MAIN_BOT_LINK=https://t.me/your_bot
REDIS_URL=redis://localhost:6379/0
API_HOST=0.0.0.0
API_PORT=8000




Тарифы (config.py)


SUBSCRIPTION_PRICES = {
    "1week":  {"stars": 30,  "usdt": 1.5,  "days": 7},
    "1month": {"stars": 100, "usdt": 5.0,  "days": 30},
    "3month": {"stars": 250, "usdt": 12.0, "days": 90},
    "6month": {"stars": 450, "usdt": 20.0, "days": 180},
}
MAX_CHATS_PER_USER    = 100
MAX_CHATS_PER_ACCOUNT = 35
TRIAL_DAYS            = 1




Команды бота


Все пользователи




Команда / действие
Описание




/start
Главное меню, регистрация нового пользователя


/tasks
Список задач рассылки


/accounts
Мои Telegram аккаунты


/pay
Оплата подписки


/mirror
Зеркальный бот


Кнопка «Новая задача»
FSM: название→текст→интервал→чаты→подтверждение




Только администратор




Команда
Описание




/admin
Панель: статистика, аккаунты, пользователи


/giveday <id> <days>
Выдать подписку пользователю


/block <id>
Заблокировать


/unblock <id>
Разблокировать


/setlimit <id> <chats>
Изменить лимит чатов


/userinfo <id>
Информация о пользователе





Запуск на VPS (Ubuntu)


# База данных
sudo apt install postgresql python3-pip python3-venv -y
sudo -u postgres psql
  CREATE DATABASE tgsaas;
  CREATE USER tguser WITH PASSWORD 'password';
  GRANT ALL PRIVILEGES ON DATABASE tgsaas TO tguser;
  \c tgsaas
  GRANT ALL ON SCHEMA public TO tguser;
  \q

# Проект
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env

# Запуск
export PYTHONPATH=/root/tg-saas

screen -S bot    && python bot/main_bot.py    # Ctrl+A D
screen -S worker && python worker/worker.py   # Ctrl+A D




TODO


[ ] Автоматическая проверка TON через toncenter API
[ ] Уведомлять пользователя об ошибках рассылки
[ ] Статистика отправок для пользователя
[ ] Команда /getid @username
[ ] Напоминание об окончании подписки за 3 дня
[ ] Проверка доступа к чату перед добавлением в задачу
