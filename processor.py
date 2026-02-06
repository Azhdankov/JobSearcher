import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv

from db import Database


class ProcSettings:
    def __init__(self) -> None:
        load_dotenv()
        self.sqlite_db_path = os.getenv("SQLITE_DB_PATH", "./telegram_messages.db")
        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.poll_interval_seconds = int(os.getenv("PROCESSOR_INTERVAL_SECONDS", str(2 * 60 * 60)))
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.selection_prompt = os.getenv("SELECTION_PROMPT", "Отберите вакансии по моим критериям.")
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def call_openai_select(api_key: str, model: str, prompt: str, items: list[dict]) -> list[dict]:
    """Call OpenAI with list of items and return list of {id, channel_name, date} that match criteria.

    The model should return JSON with structure: { "selected": [ {"id": int, "channel_name": str, "date": str} ] }
    """
    # Prepare compact payload: send only required fields
    payload_items = [
        {
            "id": it["id"],
            "channel_name": it["channel_name"],
            "raw_text": it.get("raw_text", ""),
        }
        for it in items
    ]
    
    # ВАЖНО: Каждый запрос независим, без контекста предыдущих запросов
    system_message = (
        "Ты помощник по отбору вакансий. Это НЕЗАВИСИМЫЙ запрос без контекста предыдущих запросов.\n\n"
        "Тебе дается массив сообщений из Telegram с полями id, channel_name, raw_text.\n"
        "Проанализируй КАЖДОЕ сообщение независимо, применяя следующие критерии:\n\n"
        "Критерии ПОДХОДЯЩИХ вакансий (все 3 условия ДОЛЖНЫ выполняться):\n"
        "1. Должность (хотя бы один критерий): Веб-дизайнер, UX/UI-дизайнер, Junior Дизайнер, Дизайнер интерфейсов, Продуктовый дизайнер, Начинающий дизайнер (UX/UI или веб)\n"
        "2. Уровень: Junior (Junior, Начинающий, Стажер) или иногда Middle (если не указан уровень, но обязанности подходят). НЕ ПОДХОДЯТ: Senior, Lead, Principal, Head of Design\n"
        "3. Сфера деятельности (хотя бы один критерий): Веб-сайты/лендинги, Веб-приложения/SaaS/CRM, Мобильные приложения (UI/UX), Проектные задачи. НЕ ПОДХОДЯТ: Графический дизайн, моушн-дизайн, полиграфия, карточки товаров для маркетплейсов\n\n"
        "Критерии НЕПОДХОДЯЩИХ сообщений (отсеивай их):\n"
        "- Приветственные сообщения из чатов/каналов\n"
        "- Сообщения от дизайнеров, ищущих работу или рекламирующих услуги\n"
        "- Нерелевантные вакансии (не веб/UI/UX, графический дизайн, моушн)\n"
        "- Вакансии уровня Senior+\n"
        "- Фриланс-вакансии (маркеры: \"небольшой проект\", \"разовое задание\", \"несложный лендинг\", \"на 1-2 недели\", \"для стартапа с маленьким бюджетом\")\n\n"
        "Верни JSON строго в формате: {\"selected\":[{\"id\":number,\"channel_name\":string}]}\n"
        "Не включай ничего, кроме валидного JSON. Анализируй только предоставленные сообщения, без учета предыдущих запросов."
    )
    
    # Генерируем уникальный ID запроса для явного указания независимости
    request_id = str(uuid.uuid4())[:8]
    
    user_message = {
        "role": "user",
        "content": [
            {"type": "text", "text": f"[Запрос #{request_id}] Проанализируй следующие {len(payload_items)} сообщений независимо от любых предыдущих запросов:"},
            {"type": "text", "text": json.dumps(payload_items, ensure_ascii=False)},
        ],
    }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_message},
            user_message,
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
        selected = parsed.get("selected", [])
        # Basic validation
        result: list[dict] = []
        for item in selected:
            if all(k in item for k in ("id", "channel_name")):
                result.append({
                    "id": int(item["id"]),
                    "channel_name": str(item["channel_name"]),
                })
        return result
    except Exception:
        logging.getLogger("processor").exception("Failed to parse OpenAI response")
        return []


async def send_to_telegram_bot(token: str, chat_id: str, text: str) -> None:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(api_url, json={
            "chat_id": chat_id, 
            "text": text, 
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        })


def format_selected_for_message(all_items: list[dict], selected_keys: set[tuple[int, str, str]]) -> str:
    parts: list[str] = []
    for it in all_items:
        key = (it["id"], it["channel_name"], it["date"])
        if key in selected_keys:
            title = f"[{it['channel_name']}] #{it['id']} {it['date']}"
            snippet = (it.get("raw_text") or "").strip()
            parts.append(f"{title}\n{snippet}")
    if not parts:
        return "Подходящих сообщений не найдено за период."
    return "\n\n".join(parts)


def format_single_selected_message(item: dict) -> str:
    title = f"[{item['channel_name']}]"
    snippet = (item.get("raw_text") or "").strip()
    
    # Формируем ссылку на сообщение
    message_link = ""
    if item.get("channel_id") and item.get("id"):
        # Для приватных каналов используем формат с c/
        message_link = f"\n\n🔗 [Перейти к сообщению](https://t.me/c/{item['channel_id']}/{item['id']})"
    
    return f"{title}\n{snippet}{message_link}"


async def process_once(settings: ProcSettings) -> None:
    logger = logging.getLogger("processor")
    db = Database(settings.sqlite_db_path)
    await db.init()

    items = await db.select_new_messages_ordered()
    if not items:
        logger.info("Нет новых сообщений — спим до следующего цикла")
        return

    # Save earliest date among fetched rows
    try:
        earliest_iso = min(items, key=lambda x: x["date"]) ["date"]
    except Exception:
        earliest_iso = items[0]["date"]

    selected_keys: list[dict] = []
    if settings.openai_api_key:
        selected_keys = await call_openai_select(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            prompt=settings.selection_prompt,
            items=items,
        )
    else:
        logger.warning("OPENAI_API_KEY не задан — пропускаю анализ, выберу пустой набор")

    # Update statuses: all 'new' with date >= earliest become 'completed'
    updated = await db.update_status_completed_since(earliest_iso)
    logger.info("Помечено completed: %s", updated)

    # Notify Telegram bot if configured
    if settings.telegram_bot_token and settings.telegram_chat_id:
        selected_set = {(i["id"], i["channel_name"]) for i in selected_keys}
        # Отправлять отдельное сообщение на каждый выбранный элемент
        selected_items = [it for it in items if (it["id"], it["channel_name"]) in selected_set]
        try:
            if selected_items:
                for it in selected_items:
                    text = format_single_selected_message(it)
                    await send_to_telegram_bot(settings.telegram_bot_token, settings.telegram_chat_id, text)
                    logger.info("Отправлено сообщение для #%s (%s)", it["id"], it["channel_name"])
            else:
                logger.info("Нет подходящих сообщений — отправка пропущена")
        except Exception:
            logger.exception("Не удалось отправить сообщение в Telegram бота")
    else:
        logger.info("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — пропускаю отправку")


async def main() -> None:
    settings = ProcSettings()
    configure_logging(settings.log_level)
    logger = logging.getLogger("processor")
    logger.info("Запуск процессора. Интервал: %s сек", settings.poll_interval_seconds)
    # Immediate run, then sleep-loop
    while True:
        try:
            await process_once(settings)
        except Exception:
            logger.exception("Сбой в обработке цикла")
        await asyncio.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


