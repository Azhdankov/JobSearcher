import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv

async def main():
    load_dotenv()
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone = os.environ["TELEGRAM_PHONE_NUMBER"]
    password = os.environ.get("TELEGRAM_PASSWORD") or None

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        # это единственное место, где есть интерактив
        await client.start(phone=phone, password=password)
        s = client.session.save()
        print("\n=== TELEGRAM_STRING_SESSION (скопируйте в .env) ===\n")
        print(s)
        print("\n===================================================\n")

if __name__ == "__main__":
    asyncio.run(main())
