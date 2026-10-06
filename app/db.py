"""SQLite storage: chats, messages, leads, processed message IDs, and small app state.

Design notes (kept simple on purpose):
- One small file on disk (DB_PATH). No database server.
- Each operation opens its own short-lived connection. SQLite connections are
  cheap, and this avoids any problems with sharing a connection between threads.
- Tables are created on startup if they don't exist. `PRAGMA user_version`
  records the schema version, so future changes can be applied step by step.
- All times are stored as UTC ISO-8601 strings, e.g. "2026-09-25T13:45:00+00:00".
"""

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# v1: chats, messages, leads, processed_ids
# v2: + app_state (pause flag, Telegram offset) + telegram_links (owner replies)
# v3: + telegram_topics (one Telegram thread per customer)
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    phone                    TEXT PRIMARY KEY,
    status                   TEXT NOT NULL DEFAULT 'bot' CHECK (status IN ('bot', 'human')),
    language                 TEXT,
    created_at               TEXT NOT NULL,
    last_customer_message_at TEXT,
    last_owner_message_at    TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone         TEXT NOT NULL,
    direction     TEXT NOT NULL CHECK (direction IN ('in', 'out', 'owner')),
    type          TEXT NOT NULL,
    text          TEXT,
    wa_message_id TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages (phone, id);

CREATE TABLE IF NOT EXISTS leads (
    phone      TEXT PRIMARY KEY,
    name       TEXT,
    budget     TEXT,
    deal       TEXT,
    type       TEXT,
    districts  TEXT,
    bedrooms   TEXT,
    timeline   TEXT,
    notes      TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_ids (
    wa_message_id TEXT PRIMARY KEY,
    processed_at  TEXT NOT NULL
);

-- Small settings that must survive restarts, e.g. paused = "1".
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Which Telegram thread (topic) belongs to which customer, when threads are on.
CREATE TABLE IF NOT EXISTS telegram_topics (
    phone      TEXT PRIMARY KEY,
    thread_id  INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- Which customer each forwarded Telegram message belongs to, so that when
-- the owner presses "Reply" in Telegram we know who to send the answer to.
CREATE TABLE IF NOT EXISTS telegram_links (
    telegram_message_id INTEGER PRIMARY KEY,
    phone               TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
"""

CHAT_STATUSES = ("bot", "human")
MESSAGE_DIRECTIONS = ("in", "out", "owner")
# Lead details the agent may save. Also the ONLY column names used in lead SQL.
LEAD_FIELDS = ("name", "budget", "deal", "type", "districts", "bedrooms", "timeline", "notes")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Chat:
    phone: str
    status: str  # "bot" or "human"
    language: str | None
    created_at: str
    last_customer_message_at: str | None
    last_owner_message_at: str | None


@dataclass(frozen=True)
class Message:
    id: int
    phone: str
    direction: str  # "in" (customer), "out" (bot), "owner" (you)
    type: str
    text: str | None
    wa_message_id: str | None
    created_at: str


class Database:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, roll back on error, always close."""
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # --- setup / health ---

    def init(self) -> None:
        """Create the DB file, folder, and tables if they don't exist yet."""
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # WAL mode lets reads happen while a write is in progress.
            conn.execute("PRAGMA journal_mode=WAL")
            old_version = conn.execute("PRAGMA user_version").fetchone()[0]
            # Every table uses CREATE TABLE IF NOT EXISTS, so running the full schema
            # also upgrades an older database (so far, upgrades only add tables).
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if 0 < old_version < SCHEMA_VERSION:
            logger.info("Database upgraded from schema v%s to v%s", old_version, SCHEMA_VERSION)
        logger.info("Database ready (schema v%s)", SCHEMA_VERSION)

    def is_reachable(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1 FROM chats LIMIT 1")
            return True
        except sqlite3.Error:
            logger.exception("Database health check failed")
            return False

    # --- dedupe ---

    def mark_processed(self, wa_message_id: str) -> bool:
        """Record a WhatsApp message ID. Returns False if we've already seen it.

        Meta sometimes delivers the same webhook more than once. The insert
        is atomic, so even two deliveries arriving at the same moment are
        handled correctly: only one of them gets True.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_ids (wa_message_id, processed_at) VALUES (?, ?)",
                (wa_message_id, utc_now()),
            )
            return cur.rowcount == 1

    def cleanup_processed_ids(self, older_than_days: int = 7) -> int:
        """Delete dedupe entries older than N days. Returns how many were removed."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM processed_ids WHERE processed_at < ?", (cutoff,))
            return cur.rowcount

    # --- chats ---

    def get_chat(self, phone: str) -> Chat | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM chats WHERE phone = ?", (phone,)).fetchone()
        return Chat(**dict(row)) if row else None

    def get_or_create_chat(self, phone: str) -> tuple[Chat, bool]:
        """Return (chat, created). New chats start in "bot" status."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO chats (phone, status, created_at) VALUES (?, 'bot', ?)",
                (phone, utc_now()),
            )
            created = cur.rowcount == 1
            row = conn.execute("SELECT * FROM chats WHERE phone = ?", (phone,)).fetchone()
        return Chat(**dict(row)), created

    def set_chat_status(self, phone: str, status: str) -> None:
        if status not in CHAT_STATUSES:
            raise ValueError(f"status must be one of {CHAT_STATUSES}, got {status!r}")
        self.get_or_create_chat(phone)
        with self._connect() as conn:
            conn.execute("UPDATE chats SET status = ? WHERE phone = ?", (status, phone))

    def take_over(self, phone: str) -> None:
        """You take over this chat: the bot goes silent in it; the timeout clock starts now."""
        self.get_or_create_chat(phone)
        with self._connect() as conn:
            conn.execute(
                "UPDATE chats SET status = 'human', last_owner_message_at = ? WHERE phone = ?",
                (utc_now(), phone),
            )

    def hand_back(self, phone: str) -> None:
        """Give the chat back to the bot."""
        self.set_chat_status(phone, "bot")

    def record_owner_message(self, phone: str) -> None:
        """You wrote in this chat (restarts the HUMAN_TIMEOUT_HOURS clock)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE chats SET last_owner_message_at = ? WHERE phone = ?", (utc_now(), phone)
            )

    def expired_human_chats(self, timeout_hours: float) -> list[Chat]:
        """Human-mode chats where you haven't written for `timeout_hours` (or never)."""
        cutoff = (datetime.now(UTC) - timedelta(hours=timeout_hours)).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chats WHERE status = 'human'"
                " AND (last_owner_message_at IS NULL OR last_owner_message_at < ?)",
                (cutoff,),
            ).fetchall()
        return [Chat(**dict(r)) for r in rows]

    def record_customer_activity(self, phone: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chats SET last_customer_message_at = ? WHERE phone = ?",
                (utc_now(), phone),
            )

    def list_chats_by_status(self, status: str) -> list[Chat]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chats WHERE status = ? ORDER BY phone", (status,)
            ).fetchall()
        return [Chat(**dict(r)) for r in rows]

    # --- app state (survives restarts) ---

    def get_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def is_paused(self) -> bool:
        """True after /pause (the bot answers nobody until /resume)."""
        return self.get_state("paused") == "1"

    def set_paused(self, paused: bool) -> None:
        self.set_state("paused", "1" if paused else "0")

    # --- Telegram reply links ---

    def link_telegram_message(self, telegram_message_id: int, phone: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO telegram_links (telegram_message_id, phone, created_at)"
                " VALUES (?, ?, ?)",
                (telegram_message_id, phone, utc_now()),
            )

    def phone_for_telegram_message(self, telegram_message_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT phone FROM telegram_links WHERE telegram_message_id = ?",
                (telegram_message_id,),
            ).fetchone()
        return row["phone"] if row else None

    # --- Telegram threads (one per customer) ---

    def get_thread(self, phone: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT thread_id FROM telegram_topics WHERE phone = ?", (phone,)).fetchone()
        return row["thread_id"] if row else None

    def set_thread(self, phone: str, thread_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO telegram_topics (phone, thread_id, created_at) VALUES (?, ?, ?)",
                (phone, thread_id, utc_now()),
            )

    def clear_thread(self, phone: str) -> None:
        """Forget a customer's thread (e.g. it was deleted in Telegram); a new one is made next time."""
        with self._connect() as conn:
            conn.execute("DELETE FROM telegram_topics WHERE phone = ?", (phone,))

    def phone_for_thread(self, thread_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT phone FROM telegram_topics WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return row["phone"] if row else None

    def reset_telegram_mapping(self) -> None:
        """Forget all customer topics and reply links (the Telegram console moved to another chat)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM telegram_topics")
            conn.execute("DELETE FROM telegram_links")

    def cleanup_telegram_links(self, older_than_days: int = 7) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM telegram_links WHERE created_at < ?", (cutoff,))
            return cur.rowcount

    # --- leads ---

    def save_lead(self, phone: str, fields: dict) -> dict:
        """Create or update this customer's lead. Only non-empty fields are changed."""
        changes = {k: v for k, v in fields.items() if k in LEAD_FIELDS and v not in (None, "")}
        now = utc_now()
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO leads (phone, updated_at) VALUES (?, ?)", (phone, now))
            if changes:
                # Column names come from LEAD_FIELDS only (never from outside), so this is safe.
                assignments = ", ".join(f"{column} = ?" for column in changes)
                conn.execute(
                    f"UPDATE leads SET {assignments}, updated_at = ? WHERE phone = ?",
                    (*changes.values(), now, phone),
                )
            row = conn.execute("SELECT * FROM leads WHERE phone = ?", (phone,)).fetchone()
        return dict(row)

    def get_lead(self, phone: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM leads WHERE phone = ?", (phone,)).fetchone()
        return dict(row) if row else None

    def recent_leads(self, limit: int = 10) -> list[dict]:
        """Most recently updated leads first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM leads ORDER BY updated_at DESC, phone LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- deleting a customer's data (PDPL, /forget) ---

    def customer_data_counts(self, phone: str) -> dict:
        """What /forget would delete for this phone number."""
        with self._connect() as conn:
            return self._count_customer_data(conn, phone)

    def forget_customer(self, phone: str) -> dict:
        """Permanently delete everything stored about this customer. Returns what was deleted.

        Also removes the dedupe entries of their messages (a WhatsApp message ID
        contains the phone number in encoded form).
        """
        with self._connect() as conn:  # one transaction: all or nothing
            counts = self._count_customer_data(conn, phone)
            conn.execute(
                "DELETE FROM processed_ids WHERE wa_message_id IN"
                " (SELECT wa_message_id FROM messages WHERE phone = ? AND wa_message_id IS NOT NULL)",
                (phone,),
            )
            for table in ("messages", "leads", "chats", "telegram_links", "telegram_topics"):
                conn.execute(f"DELETE FROM {table} WHERE phone = ?", (phone,))
        return counts

    @staticmethod
    def _count_customer_data(conn: sqlite3.Connection, phone: str) -> dict:
        def count(table: str) -> int:
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE phone = ?", (phone,)).fetchone()[0]

        return {
            "messages": count("messages"),
            "lead": count("leads"),
            "chat": count("chats"),
            "telegram_links": count("telegram_links"),
            "telegram_thread": count("telegram_topics"),
        }

    # --- messages ---

    def save_message(
        self,
        phone: str,
        direction: str,
        type: str,
        text: str | None = None,
        wa_message_id: str | None = None,
    ) -> int:
        """Store one message and return its row ID."""
        if direction not in MESSAGE_DIRECTIONS:
            raise ValueError(f"direction must be one of {MESSAGE_DIRECTIONS}, got {direction!r}")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (phone, direction, type, text, wa_message_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (phone, direction, type, text, wa_message_id, utc_now()),
            )
            return cur.lastrowid

    def get_recent_messages(self, phone: str, limit: int = 20) -> list[Message]:
        """The last `limit` messages of ONE chat, oldest first (ready to use as history)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE phone = ? ORDER BY id DESC LIMIT ?",
                (phone, limit),
            ).fetchall()
        return [Message(**dict(r)) for r in reversed(rows)]
