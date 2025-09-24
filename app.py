import asyncio
import json
import logging
import os
import signal
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl import types, functions

from db import Database


class Settings(BaseModel):
    api_id: int = Field(..., alias="TELEGRAM_API_ID")
    api_hash: str = Field(..., alias="TELEGRAM_API_HASH")
    phone_number: str = Field(..., alias="TELEGRAM_PHONE_NUMBER")
    password: Optional[str] = Field(None, alias="TELEGRAM_PASSWORD")
    sqlite_db_path: str = Field("./telegram_messages.db", alias="SQLITE_DB_PATH")
    session_name: str = Field("jobsearcher", alias="SESSION_NAME")
    string_session: Optional[str] = Field(None, alias="TELEGRAM_STRING_SESSION")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    retention_days: int = Field(2, alias="RETENTION_DAYS")
    cleanup_interval_minutes: int = Field(60, alias="CLEANUP_INTERVAL_MINUTES")
    exclude_words: list[str] = Field(default_factory=list, alias="FILTER_EXCLUDE_WORDS")


def load_settings() -> Settings:
    load_dotenv()
    env = {
        "TELEGRAM_API_ID": os.getenv("TELEGRAM_API_ID"),
        "TELEGRAM_API_HASH": os.getenv("TELEGRAM_API_HASH"),
        "TELEGRAM_PHONE_NUMBER": os.getenv("TELEGRAM_PHONE_NUMBER"),
        "TELEGRAM_PASSWORD": os.getenv("TELEGRAM_PASSWORD"),
        "SQLITE_DB_PATH": os.getenv("SQLITE_DB_PATH", "./telegram_messages.db"),
        "SESSION_NAME": os.getenv("SESSION_NAME", "jobsearcher"),
        "TELEGRAM_STRING_SESSION": os.getenv("TELEGRAM_STRING_SESSION"),
        "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO"),
        "RETENTION_DAYS": int(os.getenv("RETENTION_DAYS", "2")),
        "CLEANUP_INTERVAL_MINUTES": int(os.getenv("CLEANUP_INTERVAL_MINUTES", "60")),
        "FILTER_EXCLUDE_WORDS": os.getenv("FILTER_EXCLUDE_WORDS", ""),
    }
    # Parse FILTER_EXCLUDE_WORDS as JSON array or comma-separated string
    raw_words = env["FILTER_EXCLUDE_WORDS"]
    if isinstance(raw_words, list):
        parsed_words = [str(w).strip() for w in raw_words if str(w).strip()]
    else:
        text = str(raw_words).strip()
        if not text:
            parsed_words = []
        else:
            try:
                maybe_list = json.loads(text)
                if isinstance(maybe_list, list):
                    parsed_words = [str(w).strip() for w in maybe_list if str(w).strip()]
                else:
                    parsed_words = [s.strip() for s in text.split(",") if s.strip()]
            except json.JSONDecodeError:
                parsed_words = [s.strip() for s in text.split(",") if s.strip()]
    env["FILTER_EXCLUDE_WORDS"] = parsed_words

    if env["TELEGRAM_API_ID"] is None:
        raise RuntimeError("TELEGRAM_API_ID is not set")
    return Settings(**env)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("telethon").setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def normalize_session_path(name: str) -> str:
    session_path = name
    if not session_path.endswith(".session"):
        session_path = f"{session_path}.session"
    session_dir = os.path.dirname(session_path) or "."
    os.makedirs(session_dir, exist_ok=True)
    return session_path


async def run_service(settings: Settings) -> None:
    configure_logging(settings.log_level)
    logger = logging.getLogger("app")

    db = Database(settings.sqlite_db_path)
    await db.init()

    # Устойчивые параметры клиента
    common_kwargs = dict(
        connection_retries=None,
        request_retries=100,
        retry_delay=1,
        timeout=10,
        sequential_updates=False,
        flood_sleep_threshold=60,
    )

    using_string = bool(settings.string_session)
    if using_string:
        client = TelegramClient(
            StringSession(settings.string_session),
            settings.api_id,
            settings.api_hash,
            **common_kwargs,
        )
        session_path_display = "<StringSession>"
    else:
        session_path = normalize_session_path(settings.session_name)
        client = TelegramClient(
            session_path,
            settings.api_id,
            settings.api_hash,
            **common_kwargs,
        )
        session_path_display = session_path

    # Диагностика «слишком длинных» апдейтов
    @client.on(events.Raw)
    async def _raw_diag(update):
        if isinstance(update, (types.UpdatesTooLong, types.UpdateChannelTooLong)):
            logger.warning("Raw: got *TooLong* update -> Telethon will fetch difference soon")

    # Основной обработчик
    @client.on(events.NewMessage(incoming=True))
    async def handler(event: events.newmessage.NewMessage.Event) -> None:
        try:
            message = event.message
            peer = await event.get_chat()
            channel_name: Optional[str] = getattr(peer, "title", None) or getattr(peer, "username", None) or "unknown"
            channel_id = getattr(peer, "id", None)

            message_id = message.id
            date: datetime = message.date
            raw_text: str = message.raw_text or ""
            author: Optional[str] = None

            try:
                sender = await message.get_sender()
                if sender is not None:
                    author = getattr(sender, "username", None) or getattr(sender, "first_name", None)
            except Exception:
                author = None

            # Фильтр по стоп-словам
            text_lower = raw_text.lower()
            if any(word.lower() in text_lower for word in settings.exclude_words):
                logger.info(
                    "Skipped message %s (chan=%s id=%s) due to exclude words",
                    message_id, channel_name, channel_id
                )
                return

            await db.insert_message(
                message_id=message_id,
                channel_name=channel_name,
                date=date,
                raw_text=raw_text,
                author=author,
                status="new",
            )
            logger.info(
                "Saved message %s (chan=%s id=%s) at %s",
                message_id, channel_name, channel_id, date.isoformat()
            )
        except Exception as e:
            logger.exception("Failed to process message: %s", e)

    async def cleanup_job() -> None:
        while True:
            try:
                deleted = await db.delete_old_messages(settings.retention_days)
                if deleted:
                    logger.info("Cleanup: deleted %s old rows", deleted)
                await db.wal_checkpoint_truncate()
            except Exception:
                logger.exception("Cleanup job failed")
            await asyncio.sleep(settings.cleanup_interval_minutes * 60)

    async def health_job() -> None:
        while True:
            try:
                state = await client(functions.updates.GetStateRequest())
                logger.debug(
                    "Health: updates state pts=%s qts=%s seq=%s date=%s",
                    getattr(state, "pts", None),
                    getattr(state, "qts", None),
                    getattr(state, "seq", None),
                    getattr(state, "date", None),
                )
            except Exception as e:
                logger.warning("Health: failed to get updates state: %r", e)
            finally:
                await asyncio.sleep(60)

    # Подключение без интерактива
    await client.connect()
    cleanup_task = None
    health_task = None
    try:
        if not await client.is_user_authorized():
            if using_string:
                logger.error("StringSession не авторизована. Сгенерируйте новую TELEGRAM_STRING_SESSION через auth_cli.py.")
            else:
                logger.error(
                    "Нет валидной файловой сессии: %s. Рекомендуется перейти на TELEGRAM_STRING_SESSION (auth_cli.py).",
                    session_path_display,
                )
            return

        # Warm-up
        try:
            await client.get_dialogs(limit=1)
        except Exception:
            logger.exception("Warm-up get_dialogs failed")

        me = await client.get_me()
        logger.info(
            "Authorized as %s. Session: %s",
            getattr(me, "username", None) or getattr(me, "id", "unknown"),
            session_path_display,
        )

        logger.info("Client started. Listening for new messages...")
        cleanup_task = asyncio.create_task(cleanup_job())
        health_task = asyncio.create_task(health_job())

        # Ожидаем сигнал ОС ИЛИ разрыв клиента
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass

        # Важно: client.disconnected — Future, не оборачиваем в create_task
        # А вот stop_event.wait() — корутина, её нужно превратить в Task
        stop_wait_task = asyncio.create_task(stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_wait_task, client.disconnected},
                return_when=asyncio.FIRST_COMPLETED
            )
            # Если отключился клиент — сообщим
            if client.disconnected in done and not stop_event.is_set():
                logger.warning("Client disconnected unexpectedly — shutting down gracefully")
        finally:
            if not stop_wait_task.done():
                stop_wait_task.cancel()
                try:
                    await stop_wait_task
                except asyncio.CancelledError:
                    pass

    finally:
        # Аккуратно гасим фоновые задачи, чтобы aiosqlite не писала в закрытый loop
        for task in (cleanup_task, health_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logging.getLogger("app").exception("Background task failed during shutdown")

        # Сохраняем файловую сессию, если надо
        if not using_string:
            try:
                client.session.save()
            except Exception:
                logging.getLogger("app").exception("Failed to save session on shutdown")

        # Отключаемся от Telegram
        try:
            await client.disconnect()
        finally:
            # Корректно закрываем БД (если в вашем Database есть close)
            try:
                close_coro = getattr(db, "close", None)
                if callable(close_coro):
                    await close_coro()
            except Exception:
                logging.getLogger("app").exception("Failed to close database")


async def main() -> None:
    settings = load_settings()
    await run_service(settings)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
