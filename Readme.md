## JobSearcher Telegram Listener

Собирает сообщения из Telegram-каналов, на которые подписан аккаунт (MTProto через Telethon), и сохраняет их в SQLite.

### Функциональность
- Подключение по MTProto (Telethon) от имени реального пользователя
- Сбор новых сообщений из всех каналов, где есть подписка
- Сохранение в SQLite (`messages` с полями: id, channel_name, date, raw_text, author, status)
- Статус сообщений по умолчанию — `new`
 - Автоочистка: удаление сообщений старше N дней, освобождение места на диске (WAL checkpoint, auto_vacuum)
 - Фильтрация: сообщения не сохраняются, если содержат слова из `FILTER_EXCLUDE_WORDS`

### Структура БД (SQLite)
Таблица `messages`:
- id INTEGER
- channel_name TEXT
- date TEXT (ISO 8601)
- raw_text TEXT
- author TEXT
- status TEXT (default `new`)

Первичный ключ: `(id, channel_name, date)`

### Требования
- Python 3.11+

### Установка и локальный запуск
1. Скопируйте `.env.example` в `.env` и заполните:
```
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_PHONE_NUMBER=+7...
# Если включена 2FA — укажите пароль
TELEGRAM_PASSWORD=...

SQLITE_DB_PATH=./telegram_messages.db
SESSION_NAME=jobsearcher
LOG_LEVEL=INFO
RETENTION_DAYS=2
CLEANUP_INTERVAL_MINUTES=60
FILTER_EXCLUDE_WORDS=["middle", "senior", "6 лет", "большой опыт"]
```
2. Установите зависимости:
```
pip install -r requirements.txt
```
3. Запуск:
```
python app.py
```
При первом запуске потребуется ввести код из Telegram (смс/приложение). Если включена 2FA — понадобится пароль из `TELEGRAM_PASSWORD`.

### Запуск через Docker Compose (рекомендуется для сервера)
1. Подготовьте файл `.env` (как выше). `API_ID`, `API_HASH`, `PHONE_NUMBER` и, при необходимости, `TELEGRAM_PASSWORD` обязателены.
2. Соберите и запустите контейнер:
```
docker compose up -d --build
```
3. Важно: при первой авторизации потребуется код. Подключитесь к контейнеру и завершите логин:
```
docker attach jobsearcher-app
# Введите код, затем при необходимости 2FA пароль
```
Контейнер будет слушать новые сообщения. База и сессия хранятся в volume-мах:
- База: `./data/telegram_messages.db`
- Сессия: файлы в `./sessions`

UI для просмотра SQLite (sqlite-web):
- После `docker compose up -d` откройте `http://<host>:8081` (по умолчанию: `http://localhost:8081`).
- Доступ только на чтение (volume примонтирован как read-only).

### Автоочистка и освобождение диска
- По умолчанию включена очистка записей старше `RETENTION_DAYS` (по ISO-полю `date`).
- Интервал задачи — каждые `CLEANUP_INTERVAL_MINUTES` минут.
- Используется WAL и `wal_checkpoint(TRUNCATE)` для быстрой отдачи освободившегося места.
- Включен `PRAGMA auto_vacuum=FULL` (однократно выполняется `VACUUM` при первом включении), что позволяет физически сокращать файл БД после удалений.

Изменить поведение можно через переменные окружения:
- `RETENTION_DAYS` — сколько дней хранить записи (по умолчанию 2)
- `CLEANUP_INTERVAL_MINUTES` — частота очистки (по умолчанию 60)
- `FILTER_EXCLUDE_WORDS` — JSON-массив или CSV-строка со словами/фразами для исключения. Сравнение нечувствительно к регистру, по подстроке.

### Интеграция с n8n (сценарий выборки и отправки)
1. Установка (Linux/Docker):
   - Рекомендуется Docker: `docker run -it --rm -p 5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n:latest`
   - Через Docker Compose (пример):
     ```yaml
     services:
       n8n:
         image: n8nio/n8n:latest
         restart: unless-stopped
         ports:
           - "5678:5678"
         environment:
           - N8N_HOST=0.0.0.0
           - N8N_PORT=5678
           - GENERIC_TIMEZONE=Europe/Moscow
         volumes:
           - n8n_data:/home/node/.n8n
     volumes:
       n8n_data: {}
     ```
   - Откройте `http://<host>:5678`, создайте учётку.
2. Доступ к SQLite:
   - Вариант A (локальный доступ к файлу): пробросьте volume `./data:/data` в сервис n8n и используйте ноду `SQLite` с путём `/data/telegram_messages.db`.
   - Вариант B (HTTP API): добавить лёгкий API около БД (не требуется сейчас, можно позже).
3. Воркфлоу n8n:
   - Триггер: `Cron` (например, каждые 10 минут).
   - Нода `SQLite` (Query):
     ```sql
     SELECT id, channel_name, date, raw_text, author
     FROM messages
     WHERE status = 'new'
       AND datetime(date) >= datetime('now', '-2 days');
     ```
   - Нода `Code` (JavaScript): анализ через OpenAI (или использовать `OpenAI` ноду n8n) — отфильтровать ненужные записи.
   - Нода `Telegram` (Bot API) — отправить выбранные записи в вашего бота/чат.
   - Опционально: Нода `SQLite` (Update) — пометить обработанные записи `status='processed'`, чтобы не обрабатывать повторно:
     ```sql
     UPDATE messages SET status = 'processed' WHERE id = {{$json.id}} AND channel_name = {{$json.channel_name}} AND date = {{$json.date}};
     ```
4. Переменные окружения для n8n: ключ OpenAI, токен Telegram бота и т.п. добавьте в UI n8n как Credentials/Environment.

### Обновление
```
git pull
pip install -r requirements.txt
python app.py
```
или через Docker:
```
docker compose pull
docker compose up -d --build
```

### Примечания безопасности
- Не коммитьте `.env` и файлы сессий (`.session*`).
- Храните `API_HASH`, `2FA` пароль в безопасном месте.
