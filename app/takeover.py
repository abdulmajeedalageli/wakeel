"""Who answers a chat right now: the bot or you? Plus helpers for times and phone numbers.

Rules:
- A chat in "human" mode is yours: the bot stays silent in it.
- It goes back to the bot after HUMAN_TIMEOUT_HOURS without a message from you
  (or when you send /bot <phone>).
- /pause silences the bot in ALL chats until /resume.
- AVAILABILITY_MODE=schedule: the bot only answers OUTSIDE your WORK_HOURS.

All functions here are plain logic (no database, no network), so they're easy to test.
"""

import re
from datetime import datetime, timedelta, timezone

from app.db import Chat
from app.normalize import normalize_text

# Saudi Arabia is UTC+3 all year (no daylight saving), so no timezone package is needed.
RIYADH = timezone(timedelta(hours=3), "Asia/Riyadh")

_WORK_HOURS = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def riyadh_now() -> datetime:
    return datetime.now(RIYADH)


def parse_work_hours(text: str) -> tuple[int, int]:
    """"09:00-17:00" -> (540, 1020), minutes after midnight. Raises ValueError if malformed."""
    m = _WORK_HOURS.match(text or "")
    if not m:
        raise ValueError(f"WORK_HOURS must look like 09:00-17:00, got {text!r}")
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59 or (h2 == 24 and m2 != 0):
        raise ValueError(f"WORK_HOURS has an invalid time: {text!r}")
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise ValueError(f"WORK_HOURS start and end are the same: {text!r}")
    return start, end


def in_work_hours(now: datetime, work_hours: str) -> bool:
    """True if `now` (converted to Riyadh time) is within WORK_HOURS, every day of the week.

    Start is included, end is not: with 09:00-17:00, 09:00 is work time, 17:00 is not.
    Overnight ranges like 22:00-06:00 work too.
    """
    start, end = parse_work_hours(work_hours)
    local = now.astimezone(RIYADH)
    minute = local.hour * 60 + local.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # overnight


def timed_out(chat: Chat, now: datetime, timeout_hours: float) -> bool:
    """True if a human-mode chat should go back to the bot.

    No owner activity recorded at all also counts as timed out.
    """
    if chat.status != "human":
        return False
    if not chat.last_owner_message_at:
        return True
    last = datetime.fromisoformat(chat.last_owner_message_at)
    return now - last >= timedelta(hours=timeout_hours)


def owner_handles(
    chat: Chat, paused: bool, mode: str, work_hours: str, now: datetime
) -> str | None:
    """Why the bot must NOT answer this chat now, or None if the bot should answer.

    Returns "human" (you took it over), "paused" (/pause is on), or "work_hours"
    (schedule mode, during your working hours).
    """
    if chat.status == "human":
        return "human"
    if paused:
        return "paused"
    if mode == "schedule" and in_work_hours(now, work_hours):
        return "work_hours"
    return None


def normalize_phone(raw: str | None) -> str | None:
    """Turn what you type into WhatsApp's format (country code, digits only). None if invalid.

    "0512345678" -> "966512345678"   (Saudi local format)
    "+966 51 234 5678", "00966512345678", "٠٥١٢٣٤٥٦٧٨" -> "966512345678"
    """
    digits = re.sub(r"\D", "", normalize_text(raw))  # normalize_text converts Arabic digits
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("05"):
        digits = "966" + digits[1:]
    elif len(digits) == 9 and digits.startswith("5"):
        digits = "966" + digits
    if not 8 <= len(digits) <= 15:
        return None
    return digits
