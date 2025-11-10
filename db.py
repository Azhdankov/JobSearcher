import aiosqlite
from typing import Optional
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER NOT NULL,
    channel_name TEXT NOT NULL,
    channel_id INTEGER,
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
            # Ensure WAL and auto_vacuum to reclaim space; create schema and index
            await db.execute("PRAGMA journal_mode=WAL;")
            # Check current auto_vacuum mode; if not FULL (2), set and VACUUM once
            async with db.execute("PRAGMA auto_vacuum;") as cur:
                row = await cur.fetchone()
                current_mode = row[0] if row else 0
            if current_mode != 2:
                await db.execute("PRAGMA auto_vacuum=FULL;")
                # VACUUM is required after changing auto_vacuum to rebuild the database file
                await db.execute("VACUUM;")
            await db.execute(SCHEMA)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);"
            )
            await db.commit()

    async def insert_message(
        self,
        message_id: int,
        channel_name: str,
        channel_id: Optional[int],
        date: datetime,
        raw_text: str,
        author: Optional[str],
        status: str = "new",
    ) -> None:
        iso_date = date.isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO messages (id, channel_name, channel_id, date, raw_text, author, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, channel_name, channel_id, iso_date, raw_text, author, status),
            )
            await db.commit()

    async def delete_old_messages(self, older_than_days: int) -> int:
        """Delete messages older than N days based on ISO date string and return deleted rows count."""
        cutoff_dt = datetime.utcnow() - timedelta(days=older_than_days)
        cutoff_iso = cutoff_dt.isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM messages WHERE date < ?",
                (cutoff_iso,),
            )
            await db.commit()
            return cursor.rowcount or 0

    async def wal_checkpoint_truncate(self) -> None:
        """Trigger WAL checkpoint with TRUNCATE to shrink wal file size."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            await db.commit()

    async def vacuum(self) -> None:
        """Run VACUUM to force file compaction if needed (rare)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("VACUUM;")
            await db.commit()

    async def select_new_messages_ordered(self, limit: int | None = None) -> list[dict]:
        """Return list of messages with status 'new' ordered by date ASC.

        Each item is a dict with keys: id, channel_name, channel_id, date (ISO str), raw_text, author, status.
        """
        query = (
            "SELECT id, channel_name, channel_id, date, raw_text, author, status "
            "FROM messages WHERE status = 'new' ORDER BY datetime(date) ASC"
        )
        if limit is not None:
            query += " LIMIT ?"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if limit is not None:
                async with db.execute(query, (limit,)) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute(query) as cur:
                    rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def update_status_completed_since(self, since_iso: str) -> int:
        """Set status='completed' for all 'new' messages with date >= since_iso.

        Returns number of updated rows.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE messages SET status = 'completed' WHERE status = 'new' AND datetime(date) >= datetime(?)",
                (since_iso,),
            )
            await db.commit()
            return cursor.rowcount or 0