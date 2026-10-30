"""Persistencia local de historias conversacionales."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS stories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    character_id INTEGER NOT NULL,
                    genre TEXT NOT NULL DEFAULT 'Romance',
                    tone TEXT NOT NULL DEFAULT 'Cálido',
                    rating TEXT NOT NULL DEFAULT 'sfw',
                    summary TEXT NOT NULL DEFAULT '',
                    summarized_through INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS story_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    generation_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_story_messages_story
                    ON story_messages(story_id, id);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(stories)")}
            if "summary" not in columns:
                conn.execute("ALTER TABLE stories ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
            if "summarized_through" not in columns:
                conn.execute("ALTER TABLE stories ADD COLUMN summarized_through INTEGER NOT NULL DEFAULT 0")

    def create(self, title: str, character_id: int, genre: str, tone: str, rating: str) -> int:
        now = _now()
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO stories(title, character_id, genre, tone, rating, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (title, character_id, genre, tone, rating, now, now),
            )
            return int(cur.lastrowid)

    def list(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("""SELECT s.*, c.name AS character_name,
                (SELECT content FROM story_messages m WHERE m.story_id=s.id ORDER BY m.id DESC LIMIT 1) AS preview
                FROM stories s LEFT JOIN characters c ON c.id=s.character_id
                ORDER BY s.updated_at DESC, s.id DESC""").fetchall()
        return [dict(row) for row in rows]

    def get(self, story_id: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("""SELECT s.*, c.name AS character_name
                FROM stories s LEFT JOIN characters c ON c.id=s.character_id WHERE s.id=?""", (story_id,)).fetchone()
            if row is None:
                return None
            story = dict(row)
            story["messages"] = [dict(m) for m in conn.execute(
                "SELECT * FROM story_messages WHERE story_id=? ORDER BY id", (story_id,)
            ).fetchall()]
        return story

    def add_message(self, story_id: int, role: str, content: str) -> int:
        now = _now()
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO story_messages(story_id, role, content, created_at) VALUES(?,?,?,?)",
                (story_id, role, content, now),
            )
            conn.execute("UPDATE stories SET updated_at=? WHERE id=?", (now, story_id))
            return int(cur.lastrowid)

    def link_generation(self, message_id: int, generation_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE story_messages SET generation_id=? WHERE id=?", (generation_id, message_id))

    def delete_message(self, story_id: int, message_id: int) -> bool:
        """Remove one turn and invalidate summaries that could repeat its text."""
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM story_messages WHERE id=? AND story_id=?",
                (message_id, story_id),
            )
            if cur.rowcount:
                conn.execute(
                    "UPDATE stories SET summary='', summarized_through=0, updated_at=? WHERE id=?",
                    (_now(), story_id),
                )
            return cur.rowcount > 0

    def replace_message_content(
        self, story_id: int, message_id: int, content: str, *, clear_summary: bool = False
    ) -> bool:
        """Replace an assistant reply and unlink its now-stale illustration."""
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "UPDATE story_messages SET content=?, generation_id=NULL WHERE id=? AND story_id=?",
                (content, message_id, story_id),
            )
            if cur.rowcount:
                if clear_summary:
                    conn.execute(
                        "UPDATE stories SET summary='', summarized_through=0, updated_at=? WHERE id=?",
                        (_now(), story_id),
                    )
                else:
                    conn.execute("UPDATE stories SET updated_at=? WHERE id=?", (_now(), story_id))
            return cur.rowcount > 0

    def update_summary(self, story_id: int, summary: str, through_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE stories SET summary=?, summarized_through=?, updated_at=? WHERE id=?",
                (summary, through_id, _now(), story_id),
            )

    def delete(self, story_id: int) -> bool:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute("DELETE FROM stories WHERE id=?", (story_id,))
            return cur.rowcount > 0
