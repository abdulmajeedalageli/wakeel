"""Takeover rules: work hours, timeout, who answers, phone numbers."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db import Chat
from app.takeover import (
    RIYADH,
    in_work_hours,
    normalize_phone,
    owner_handles,
    parse_work_hours,
    timed_out,
)


def riyadh(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 25, hour, minute, tzinfo=RIYADH)


def chat(status: str = "bot", owner_hours_ago: float | None = None) -> Chat:
    last = None
    if owner_hours_ago is not None:
        last = (datetime.now(UTC) - timedelta(hours=owner_hours_ago)).isoformat(timespec="seconds")
    return Chat("966512341234", status, None, "2026-09-01T00:00:00+00:00", None, last)


# --- work hours (Riyadh = UTC+3) ---

@pytest.mark.parametrize("hour, minute, expected", [
    (8, 59, False),
    (9, 0, True),   # start is included
    (12, 30, True),
    (16, 59, True),
    (17, 0, False),  # end is not
    (23, 0, False),
])
def test_in_work_hours(hour, minute, expected):
    assert in_work_hours(riyadh(hour, minute), "09:00-17:00") is expected


def test_work_hours_use_riyadh_time_whatever_the_input_timezone():
    # 06:30 UTC = 09:30 in Riyadh -> within 09:00-17:00
    assert in_work_hours(datetime(2026, 9, 25, 6, 30, tzinfo=UTC), "09:00-17:00")
    # 15:00 UTC = 18:00 in Riyadh -> outside
    assert not in_work_hours(datetime(2026, 9, 25, 15, 0, tzinfo=UTC), "09:00-17:00")


@pytest.mark.parametrize("hour, expected", [(21, False), (22, True), (2, True), (5, True), (6, False)])
def test_overnight_work_hours(hour, expected):
    assert in_work_hours(riyadh(hour), "22:00-06:00") is expected


def test_work_hours_accept_spaces_and_24():
    assert parse_work_hours(" 9:00 - 24:00 ") == (540, 1440)


@pytest.mark.parametrize("bad", ["", "9-5", "09:00", "25:00-26:00", "09:60-10:00", "10:00-10:00", "24:30-01:00"])
def test_malformed_work_hours_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_work_hours(bad)


def test_settings_reject_malformed_work_hours():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="WORK_HOURS"):
        Settings(work_hours="9 to 5")


# --- timeout ---

def test_timed_out():
    now = datetime.now(UTC)
    assert not timed_out(chat("human", owner_hours_ago=23.9), now, 24)
    assert timed_out(chat("human", owner_hours_ago=24.1), now, 24)
    assert timed_out(chat("human", owner_hours_ago=None), now, 24)  # no activity recorded
    assert not timed_out(chat("bot", owner_hours_ago=100), now, 24)  # bot chats never "time out"


def test_timeout_accepts_decimal_hours():
    now = datetime.now(UTC)
    assert timed_out(chat("human", owner_hours_ago=0.02), now, 0.01)
    assert not timed_out(chat("human", owner_hours_ago=0.005), now, 0.01)


# --- who answers ---

def test_owner_handles_each_case():
    work, night = riyadh(10), riyadh(20)
    assert owner_handles(chat("human"), False, "always", "09:00-17:00", night) == "human"
    assert owner_handles(chat("bot"), True, "always", "09:00-17:00", night) == "paused"
    assert owner_handles(chat("bot"), False, "schedule", "09:00-17:00", work) == "work_hours"
    assert owner_handles(chat("bot"), False, "schedule", "09:00-17:00", night) is None
    assert owner_handles(chat("bot"), False, "always", "09:00-17:00", work) is None


def test_human_mode_wins_over_everything():
    assert owner_handles(chat("human"), True, "schedule", "09:00-17:00", riyadh(10)) == "human"


# --- phone numbers ---

@pytest.mark.parametrize("raw", [
    "966512345678",
    "+966512345678",
    "+966 51 234 5678",
    "00966512345678",
    "0512345678",
    "512345678",
    "٠٥١٢٣٤٥٦٧٨",
    "  +966-51-234-5678 ",
])
def test_normalize_saudi_numbers(raw):
    assert normalize_phone(raw) == "966512345678"


def test_normalize_keeps_other_countries():
    assert normalize_phone("+44 7700 900123") == "447700900123"


@pytest.mark.parametrize("raw", [None, "", "abc", "12345", "1234567890123456"])
def test_normalize_rejects_invalid(raw):
    assert normalize_phone(raw) is None
