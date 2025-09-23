import aiosqlite
from typing import Optional
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER NOT NULL,
    channel_name TEXT NOT NULL,
    date TEXT NOT NULL,
    raw_text TEXT,
    author TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    PRIMARY KEY (id, channel_name, date)
);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(SCHEMA)
            await db.commit()

    async def insert_message(
        self,
        message_id: int,
        channel_name: str,
        date: datetime,
        raw_text: str,
        author: Optional[str],
        status: str = "new",
    ) -> None:
        iso_date = date.isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO messages (id, channel_name, date, raw_text, author, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, channel_name, iso_date, raw_text, author, status),
            )
            await db.commit()
