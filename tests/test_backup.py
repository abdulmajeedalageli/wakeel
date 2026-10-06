"""Daily database backups."""

import sqlite3
from datetime import UTC, datetime

from app.backup import backup_database, backup_dir_for, delete_old_backups
from app.takeover import RIYADH

PHONE = "966512341234"


def riyadh(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=RIYADH)


def test_backup_is_a_complete_copy_even_while_the_db_is_open(db, tmp_path):
    db.save_message(PHONE, "in", "text", "hello")
    db.save_lead(PHONE, {"name": "Sara"})
    keep_open = sqlite3.connect(db.path)  # the running bot has connections open too
    try:
        created = backup_database(db.path, tmp_path / "backups", now=riyadh(26))
    finally:
        keep_open.close()

    assert created.name == "bot-2026-09-26.db"
    with sqlite3.connect(created) as copy:
        assert copy.execute("SELECT text FROM messages").fetchone()[0] == "hello"
        assert copy.execute("SELECT name FROM leads").fetchone()[0] == "Sara"
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_file_name_uses_the_riyadh_date(db, tmp_path):
    # 22:30 UTC on the 25th is already 01:30 on the 26th in Riyadh.
    created = backup_database(db.path, tmp_path, now=datetime(2026, 9, 25, 22, 30, tzinfo=UTC))
    assert created.name == "bot-2026-09-26.db"


def test_only_one_backup_per_day(db, tmp_path):
    assert backup_database(db.path, tmp_path, now=riyadh(26, 1)) is not None
    assert backup_database(db.path, tmp_path, now=riyadh(26, 23)) is None
    assert len(list(tmp_path.glob("bot-*.db"))) == 1


def test_old_backups_are_deleted_others_kept(tmp_path):
    for day in range(1, 27):
        (tmp_path / f"bot-2026-09-{day:02d}.db").write_text("x")
    (tmp_path / "notes.txt").write_text("keep me")
    (tmp_path / "bot-manual-copy.db").write_text("keep me")

    deleted = delete_old_backups(tmp_path, riyadh(26).date(), keep_days=14)

    remaining = sorted(f.name for f in tmp_path.glob("bot-2026-*.db"))
    assert remaining[0] == "bot-2026-09-13.db"  # 14 days: the 13th to the 26th
    assert remaining[-1] == "bot-2026-09-26.db"
    assert len(remaining) == 14
    assert len(deleted) == 12
    assert (tmp_path / "notes.txt").exists()  # non-backup files are never touched
    assert (tmp_path / "bot-manual-copy.db").exists()


def test_backup_folder_is_created(db, tmp_path):
    folder = tmp_path / "deep" / "backups"
    backup_database(db.path, folder, now=riyadh(26))
    assert (folder / "bot-2026-09-26.db").exists()


def test_backups_live_next_to_the_database():
    assert backup_dir_for("/app/data/bot.db").as_posix() == "/app/data/backups"
