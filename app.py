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
from telethon.errors import AuthKeyUnregisteredError, RPCError
from telethon.tl import functions

from db import Database


class Settings(BaseModel):
    api_id: int = Field(..., alias="TELEGRAM_API_ID")
    api_hash: str = Field(..., alias="TELEGRAM_API_HASH")
    phone_number: str = Field(..., alias="TELEGRAM_PHONE_NUMBER")
    password: Optional[str] = Field(None, alias="TELEGRAM_PASSWORD")
    sqlite_db_path: str = Field("./telegram_messages.db", alias="SQLITE_DB_PATH")
    # session_name оставляем в модели, но не используем (никаких файловых .session)
    session_name: str = Field("jobsearcher", alias="SESSION_NAME")
    string_session: str = Field(..., alias="TELEGRAM_STRING_SESSION")
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
        "TELEGRAM_STRING_SESSION": (os.getenv("TELEGRAM_STRING_SESSION") or "").strip(),
        "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO"),
        "RETENTION_DAYS": int(os.getenv("RETENTION_DAYS", "2")),
        "CLEANUP_INTERVAL_MINUTES": int(os.getenv("CLEANUP_INTERVAL_MINUTES", "60")),
        "FILTER_EXCLUDE_WORDS": os.getenv("FILTER_EXCLUDE_WORDS", ""),
    }
    # FILTER_EXCLUDE_WORDS: JSON-массив или CSV
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
                parsed_words = (
                    [str(w).strip() for w in maybe_list if str(w).strip()]
                    if isinstance(maybe_list, list)
                    else [s.strip() for s in text.split(",") if s.strip()]
                )
            except json.JSONDecodeError:
                parsed_words = [s.strip() for s in text.split(",") if s.strip()]
    env["FILTER_EXCLUDE_WORDS"] = parsed_words

    if not env["TELEGRAM_API_ID"]:
        raise RuntimeError("TELEGRAM_API_ID is not set")
    if not env["TELEGRAM_API_HASH"]:
        raise RuntimeError("TELEGRAM_API_HASH is not set")
    if not env["TELEGRAM_STRING_SESSION"]:
        raise RuntimeError("TELEGRAM_STRING_SESSION is not set (сгенерируйте через auth_cli.py)")

    return Settings(**env)


def configure_logging(level: str) -> None:
    base = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=base, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    # поменьше шума
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def run_service(settings: Settings) -> None:
    configure_logging(settings.log_level)
    logger = logging.getLogger("app")

    db = Database(settings.sqlite_db_path)
    await db.init()

    # Конфиг клиента. Только StringSession. Никаких start() / интерактива.
    client = TelegramClient(
        StringSession(settings.string_session),
        settings.api_id,
        settings.api_hash,
        # аккуратные ретраи; не агрессивничаем
        connection_retries=None,
        request_retries=5,
        retry_delay=2,
        timeout=10,
        flood_sleep_threshold=60,
        sequential_updates=False,
        device_model="JobSearcher",
        system_version="Linux",
        app_version="1.0",
        lang_code="en",
        system_lang_code="en",
    )

    @client.on(events.NewMessage(incoming=True))
    async def handler(event: events.newmessage.NewMessage.Event) -> None:
        try:
            message = event.message
            peer = await event.get_chat()
            channel_name: Optional[str] = getattr(peer, "title", None) or getattr(peer, "username", None) or "unknown"
            channel_id = getattr(peer, "id", None)

            message_id = message.id
            date: datetime = message.date
            raw_text: str = (message.raw_text or "").strip()
            author: Optional[str] = None

            try:
                sender = await message.get_sender()
                if sender is not None:
                    author = getattr(sender, "username", None) or getattr(sender, "first_name", None)
            except Exception:
                author = None

            # фильтры — как были
            if not raw_text or len(raw_text) < 20:
                logger.info("Skipped message %s (chan=%s id=%s) due to empty/short text", message_id, channel_name, channel_id)
                return
            if any(word.lower() in raw_text.lower() for word in settings.exclude_words):
                logger.info("Skipped message %s (chan=%s id=%s) due to exclude words", message_id, channel_name, channel_id)
                return

            await db.insert_message(
                message_id=message_id,
                channel_name=channel_name,
                date=date,
                raw_text=raw_text,
                author=author,
                status="new",
            )
            logger.info("Saved message %s (chan=%s id=%s) at %s", message_id, channel_name, channel_id, date.isoformat())
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
                logging.getLogger("app.health").debug(
                    "pts=%s qts=%s seq=%s date=%s",
                    getattr(state, "pts", None), getattr(state, "qts", None),
                    getattr(state, "seq", None), getattr(state, "date", None),
                )
            except Exception as e:
                logging.getLogger("app.health").warning("GetState failed: %r", e)
            finally:
                await asyncio.sleep(60)

    # Грациозная остановка
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    # Подключение + основной цикл с авто-reconnect
    while not stop_event.is_set():
        try:
            await client.connect()

            if not await client.is_user_authorized():
                logger.error("StringSession не авторизована. Сгенерируйте новую TELEGRAM_STRING_SESSION через auth_cli.py.")
                return

            # тёплый вызов (не обязателен)
            try:
                await client.get_me()
            except Exception:
                logger.exception("Warm-up get_me failed")

            logger.info("Authorized. Session=<StringSession>. Listening for new messages...")
            cleanup_task = asyncio.create_task(cleanup_job())
            health_task = asyncio.create_task(health_job())

            waiter = asyncio.create_task(client.run_until_disconnected())
            stopper = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)

            # аккуратно закрываем фоновые задачи
            for t in (cleanup_task, health_task):
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

            if stopper in done:
                # пришёл сигнал остановки
                break

            # если сюда дошли — клиент отключился сам, пробуем позже переподключиться
            logger.warning("Client disconnected; will try to reconnect soon...")

        except AuthKeyUnregisteredError:
            # это чёткий признак «сессия протухла/отозвана» → нужна новая строка
            logger.error("AuthKeyUnregisteredError: сессия отозвана сервером. Пересоздайте TELEGRAM_STRING_SESSION через auth_cli.py.")
            return
        except RPCError as e:
            logger.exception("Telegram RPC error: %r", e)
            await asyncio.sleep(5)
        except Exception:
            logger.exception("Unexpected error in main loop")
            await asyncio.sleep(3)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    # финал
    try:
        await client.disconnect()
    except Exception:
        pass


async def main() -> None:
    settings = load_settings()
    await run_service(settings)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
