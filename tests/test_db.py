import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.db import Database

PHONE_A = "966511111111"
PHONE_B = "966522222222"


# --- schema ---

def test_init_creates_all_tables_and_can_run_twice(db):
    db.init()  # second run must not fail or wipe anything
    with sqlite3.connect(db.path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert {"chats", "messages", "leads", "processed_ids", "app_state", "telegram_links",
            "telegram_topics"} <= tables
    assert version == 3


def test_init_creates_missing_folder(tmp_path):
    database = Database(str(tmp_path / "new" / "folder" / "bot.db"))
    database.init()
    assert database.is_reachable()


# --- dedupe ---

def test_mark_processed_is_true_only_the_first_time(db):
    assert db.mark_processed("wamid.1") is True
    assert db.mark_processed("wamid.1") is False
    assert db.mark_processed("wamid.2") is True


def test_cleanup_removes_only_old_processed_ids(db):
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    with sqlite3.connect(db.path) as conn:
        conn.execute("INSERT INTO processed_ids VALUES ('wamid.old', ?)", (old,))
    db.mark_processed("wamid.new")

    assert db.cleanup_processed_ids(older_than_days=7) == 1
    assert db.mark_processed("wamid.old") is True  # gone, so it counts as new again
    assert db.mark_processed("wamid.new") is False  # still remembered


# --- chats ---

def test_new_chat_starts_as_bot(db):
    chat, created = db.get_or_create_chat(PHONE_A)
    assert created is True
    assert chat.status == "bot"
    assert chat.created_at

    again, created_again = db.get_or_create_chat(PHONE_A)
    assert created_again is False
    assert again == chat


def test_get_chat_returns_none_for_unknown_phone(db):
    assert db.get_chat(PHONE_A) is None


def test_set_status_changes_only_that_chat(db):
    db.get_or_create_chat(PHONE_A)
    db.get_or_create_chat(PHONE_B)
    db.set_chat_status(PHONE_A, "human")

    assert db.get_chat(PHONE_A).status == "human"
    assert db.get_chat(PHONE_B).status == "bot"
    assert [c.phone for c in db.list_chats_by_status("human")] == [PHONE_A]


def test_set_status_creates_chat_if_needed(db):
    db.set_chat_status(PHONE_A, "human")
    assert db.get_chat(PHONE_A).status == "human"


def test_invalid_status_is_rejected(db):
    with pytest.raises(ValueError):
        db.set_chat_status(PHONE_A, "paused")


def test_record_customer_activity_sets_timestamp(db):
    db.get_or_create_chat(PHONE_A)
    assert db.get_chat(PHONE_A).last_customer_message_at is None
    db.record_customer_activity(PHONE_A)
    assert db.get_chat(PHONE_A).last_customer_message_at is not None


# --- messages ---

def test_recent_messages_are_oldest_first_and_limited(db):
    for i in range(25):
        db.save_message(PHONE_A, "in", "text", f"msg {i}", f"wamid.{i}")

    history = db.get_recent_messages(PHONE_A, limit=20)
    assert len(history) == 20
    assert history[0].text == "msg 5"
    assert history[-1].text == "msg 24"


def test_messages_are_kept_separate_per_customer(db):
    db.save_message(PHONE_A, "in", "text", "from A")
    db.save_message(PHONE_B, "in", "text", "from B")
    db.save_message(PHONE_A, "out", "text", "reply to A")

    assert [m.text for m in db.get_recent_messages(PHONE_A)] == ["from A", "reply to A"]
    assert [m.text for m in db.get_recent_messages(PHONE_B)] == ["from B"]


def test_invalid_direction_is_rejected(db):
    with pytest.raises(ValueError):
        db.save_message(PHONE_A, "sideways", "text", "x")


# --- schema v2: upgrade, takeover, app state, Telegram links ---

V1_SCHEMA = """
CREATE TABLE chats (phone TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'bot',
    language TEXT, created_at TEXT NOT NULL, last_customer_message_at TEXT,
    last_owner_message_at TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL,
    direction TEXT NOT NULL, type TEXT NOT NULL, text TEXT, wa_message_id TEXT,
    created_at TEXT NOT NULL);
CREATE TABLE leads (phone TEXT PRIMARY KEY, name TEXT, budget TEXT, deal TEXT, type TEXT,
    districts TEXT, bedrooms TEXT, timeline TEXT, notes TEXT, updated_at TEXT NOT NULL);
CREATE TABLE processed_ids (wa_message_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL);
INSERT INTO chats VALUES ('966511111111', 'human', NULL, '2026-09-01T10:00:00+00:00', NULL, NULL);
INSERT INTO messages (phone, direction, type, text, created_at)
    VALUES ('966511111111', 'in', 'text', 'kept after upgrade', '2026-09-01T10:00:00+00:00');
PRAGMA user_version = 1;
"""


def test_v1_database_is_upgraded_without_losing_data(tmp_path, caplog):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(V1_SCHEMA)

    database = Database(str(path))
    with caplog.at_level(logging.INFO):
        database.init()

    assert "upgraded from schema v1 to v3" in caplog.text
    assert database.get_chat(PHONE_A).status == "human"
    assert [m.text for m in database.get_recent_messages(PHONE_A)] == ["kept after upgrade"]
    database.set_paused(True)  # new table works
    assert database.is_paused()


def test_take_over_sets_human_and_starts_the_clock(db):
    db.take_over(PHONE_A)
    chat = db.get_chat(PHONE_A)
    assert chat.status == "human"
    assert chat.last_owner_message_at is not None


def test_hand_back(db):
    db.take_over(PHONE_A)
    db.hand_back(PHONE_A)
    assert db.get_chat(PHONE_A).status == "bot"


def _set_owner_time(db, phone, hours_ago):
    when = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    with sqlite3.connect(db.path) as conn:
        conn.execute("UPDATE chats SET last_owner_message_at = ? WHERE phone = ?", (when, phone))


def test_expired_human_chats(db):
    db.take_over(PHONE_A)
    db.take_over(PHONE_B)
    _set_owner_time(db, PHONE_A, hours_ago=25)
    _set_owner_time(db, PHONE_B, hours_ago=1)
    db.get_or_create_chat("966533333333")  # bot mode: never expires

    assert [c.phone for c in db.expired_human_chats(24)] == [PHONE_A]
    db.record_owner_message(PHONE_A)  # you wrote again: the clock restarts
    assert db.expired_human_chats(24) == []


def test_human_chat_without_owner_time_counts_as_expired(db):
    db.set_chat_status(PHONE_A, "human")  # old-style, no timestamp
    assert [c.phone for c in db.expired_human_chats(24)] == [PHONE_A]


def test_app_state_and_pause(db):
    assert db.get_state("anything") is None
    assert db.is_paused() is False
    db.set_paused(True)
    assert db.is_paused() is True
    db.set_paused(False)
    assert db.is_paused() is False
    db.set_state("telegram_offset", "10")
    db.set_state("telegram_offset", "11")  # overwrite, not duplicate
    assert db.get_state("telegram_offset") == "11"


def test_telegram_links(db):
    db.link_telegram_message(501, PHONE_A)
    db.link_telegram_message(502, PHONE_B)
    assert db.phone_for_telegram_message(501) == PHONE_A
    assert db.phone_for_telegram_message(502) == PHONE_B
    assert db.phone_for_telegram_message(999) is None


def test_cleanup_telegram_links(db):
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    with sqlite3.connect(db.path) as conn:
        conn.execute("INSERT INTO telegram_links VALUES (1, ?, ?)", (PHONE_A, old))
    db.link_telegram_message(2, PHONE_B)
    assert db.cleanup_telegram_links(older_than_days=7) == 1
    assert db.phone_for_telegram_message(1) is None
    assert db.phone_for_telegram_message(2) == PHONE_B



# --- leads and /forget (Phase 6) ---

def test_save_lead_creates_and_merges(db):
    first = db.save_lead(PHONE_A, {"name": "Sara", "budget": "900k"})
    second = db.save_lead(PHONE_A, {"budget": "1M", "timeline": "3 months", "name": ""})
    assert (second["name"], second["budget"], second["timeline"]) == ("Sara", "1M", "3 months")
    assert second["updated_at"] >= first["updated_at"]


def test_save_lead_ignores_unknown_fields(db):
    lead = db.save_lead(PHONE_A, {"name": "Sara", "phone": PHONE_B, "status": "vip"})
    assert lead["phone"] == PHONE_A
    assert db.get_lead(PHONE_B) is None


def test_leads_are_per_customer_and_recent_first(db):
    db.save_lead(PHONE_A, {"name": "A"})
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    with sqlite3.connect(db.path) as conn:
        conn.execute("UPDATE leads SET updated_at = ? WHERE phone = ?", (old, PHONE_A))
    db.save_lead(PHONE_B, {"name": "B"})
    assert [lead["phone"] for lead in db.recent_leads()] == [PHONE_B, PHONE_A]
    assert db.get_lead(PHONE_A)["name"] == "A"


def test_forget_customer_deletes_only_that_customer(db):
    for phone in (PHONE_A, PHONE_B):
        db.get_or_create_chat(phone)
        db.save_message(phone, "in", "text", f"hi from {phone}", wa_message_id=f"wamid.{phone}")
        db.mark_processed(f"wamid.{phone}")
        db.save_lead(phone, {"name": phone})
        db.link_telegram_message(int(phone[-4:]), phone)

    assert db.customer_data_counts(PHONE_A) == {"messages": 1, "lead": 1, "chat": 1, "telegram_links": 1, "telegram_thread": 0}
    deleted = db.forget_customer(PHONE_A)

    assert deleted == {"messages": 1, "lead": 1, "chat": 1, "telegram_links": 1, "telegram_thread": 0}
    assert db.customer_data_counts(PHONE_A) == {"messages": 0, "lead": 0, "chat": 0, "telegram_links": 0, "telegram_thread": 0}
    assert db.mark_processed(f"wamid.{PHONE_A}") is True  # its dedupe entry is gone too
    # The other customer is untouched.
    assert db.customer_data_counts(PHONE_B) == {"messages": 1, "lead": 1, "chat": 1, "telegram_links": 1, "telegram_thread": 0}
    assert db.mark_processed(f"wamid.{PHONE_B}") is False
