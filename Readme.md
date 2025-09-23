## JobSearcher Telegram Listener

Собирает сообщения из Telegram-каналов, на которые подписан аккаунт (MTProto через Telethon), и сохраняет их в SQLite.

### Функциональность
- Подключение по MTProto (Telethon) от имени реального пользователя
- Сбор новых сообщений из всех каналов, где есть подписка
- Сохранение в SQLite (`messages` с полями: id, channel_name, date, raw_text, author, status)
- Статус сообщений по умолчанию — `new`

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
