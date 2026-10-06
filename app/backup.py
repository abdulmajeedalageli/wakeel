"""Daily database backups.

Uses SQLite's online backup, which makes a consistent copy even while the bot is
running and writing. One file per day (Riyadh date) in data/backups/:
    bot-2026-09-26.db
Backups older than BACKUP_KEEP_DAYS are deleted. Keeping them only for a short time
is PDPL-friendly: a customer deleted with /forget disappears from backups within that time.

Run by hand (e.g. on the server):  python -m app.backup
"""

import logging
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from app.takeover import RIYADH, riyadh_now

logger = logging.getLogger(__name__)

_BACKUP_NAME = re.compile(r"^bot-(\d{4}-\d{2}-\d{2})\.db$")


def backup_dir_for(db_path: str) -> Path:
    """Backups live next to the database: data/bot.db -> data/backups/."""
    return Path(db_path).parent / "backups"


def backup_database(db_path: str, backup_dir: Path, keep_days: int = 14, now: datetime | None = None) -> Path | None:
    """Make today's backup if it doesn't exist yet, then delete old ones.

    Returns the new backup's path, or None if today's backup already existed.
    """
    today = (now or riyadh_now()).astimezone(RIYADH).date()
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"bot-{today.isoformat()}.db"

    created = None
    if not target.exists():
        # Write to a temporary name first, so a half-written file is never mistaken for a backup.
        partial = backup_dir / f"{target.name}.partial"
        source = sqlite3.connect(db_path)
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        partial.replace(target)
        created = target

    delete_old_backups(backup_dir, today, keep_days)
    return created


def delete_old_backups(backup_dir: Path, today: date, keep_days: int) -> list[Path]:
    """Keep today's backup and the ones from the previous keep_days - 1 days; delete older ones.

    Files that aren't named like a backup are never touched.
    """
    oldest_kept = today - timedelta(days=keep_days - 1)
    deleted = []
    for file in backup_dir.iterdir():
        match = _BACKUP_NAME.match(file.name)
        if match and date.fromisoformat(match.group(1)) < oldest_kept:
            file.unlink()
            deleted.append(file)
    if deleted:
        logger.info("Deleted %s old backup(s)", len(deleted))
    return deleted


def main() -> int:
    from app.config import get_settings

    settings = get_settings()
    folder = backup_dir_for(settings.db_path)
    created = backup_database(settings.db_path, folder, settings.backup_keep_days)
    print(f"Backup saved: {created}" if created else f"Today's backup already exists in {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
