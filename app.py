import asyncio
import logging
import os
from datetime import datetime
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
    }
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

    client = TelegramClient(settings.session_name, settings.api_id, settings.api_hash)

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
        await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
