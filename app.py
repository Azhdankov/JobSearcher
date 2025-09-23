import asyncio
import json
import logging
import os
from datetime import datetime
import asyncio
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

from db import Database


class Settings(BaseModel):
    api_id: int = Field(..., alias="TELEGRAM_API_ID")
    api_hash: str = Field(..., alias="TELEGRAM_API_HASH")
    phone_number: str = Field(..., alias="TELEGRAM_PHONE_NUMBER")
    password: Optional[str] = Field(None, alias="TELEGRAM_PASSWORD")
    sqlite_db_path: str = Field("./telegram_messages.db", alias="SQLITE_DB_PATH")
    session_name: str = Field("jobsearcher", alias="SESSION_NAME")
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
        "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO"),
        "RETENTION_DAYS": int(os.getenv("RETENTION_DAYS", "2")),
        "CLEANUP_INTERVAL_MINUTES": int(os.getenv("CLEANUP_INTERVAL_MINUTES", "60")),
        "FILTER_EXCLUDE_WORDS": os.getenv("FILTER_EXCLUDE_WORDS", ""),
    }
    # Parse FILTER_EXCLUDE_WORDS as JSON array or comma-separated string
    raw_words = env["FILTER_EXCLUDE_WORDS"]
    parsed_words: list[str]
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


async def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger("app")

    db = Database(settings.sqlite_db_path)
    await db.init()

    # Normalize session path to always end with .session and ensure directory exists
    session_path = settings.session_name
    if not session_path.endswith(".session"):
        session_path = f"{session_path}.session"
    session_dir = os.path.dirname(session_path) or "."
    os.makedirs(session_dir, exist_ok=True)

    client = TelegramClient(session_path, settings.api_id, settings.api_hash)

    @client.on(events.NewMessage())
    async def handler(event: events.newmessage.NewMessage.Event) -> None:
        try:
            message = event.message
            peer = await event.get_chat()
            channel_name: Optional[str] = getattr(peer, "title", None) or getattr(peer, "username", None) or "unknown"

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

            # Exclude messages that contain any of configured words (case-insensitive substring)
            text_lower = raw_text.lower()
            exclude_hit = any(word.lower() in text_lower for word in settings.exclude_words)
            if exclude_hit:
                logger.info(
                    "Skipped message %s from %s due to exclude words", message_id, channel_name
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
            logger.info("Saved message %s from %s", message_id, channel_name)
        except Exception as e:
            logger.exception("Failed to process message: %s", e)

    async def cleanup_job() -> None:
        while True:
            try:
                deleted = await db.delete_old_messages(settings.retention_days)
                if deleted:
                    logger.info("Cleanup: deleted %s old rows", deleted)
                # Shrink WAL to reclaim disk space immediately
                await db.wal_checkpoint_truncate()
            except Exception:
                logger.exception("Cleanup job failed")
            # Sleep until next run
            await asyncio.sleep(settings.cleanup_interval_minutes * 60)

    async with client:
        logger.info("Connecting as %s", settings.phone_number)
        if not await client.is_user_authorized():
            await client.send_code_request(settings.phone_number)
            print("Введите код из Telegram (смс/приложение): ", end="", flush=True)
            code = input().strip()
            try:
                await client.sign_in(settings.phone_number, code)
            except SessionPasswordNeededError:
                if not settings.password:
                    print("Включена 2FA, нужен пароль TELEGRAM_PASSWORD в .env")
                    raise
                await client.sign_in(password=settings.password)
        logger.info("Client started. Listening for new messages...")
        # Run cleanup in background
        asyncio.create_task(cleanup_job())
        await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
