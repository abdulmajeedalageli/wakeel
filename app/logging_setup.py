"""Logging setup and helpers for keeping personal data out of logs (PDPL)."""

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    # Log lines can contain Arabic (e.g. district names); Windows consoles default to
    # a non-UTF-8 encoding and would print "?????" instead.
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # stderr was replaced (e.g. by pytest); nothing to do
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The HTTP libraries log every request's full URL at INFO level. For Telegram,
    # the bot TOKEN is part of the URL, so these loggers must stay quiet.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def mask_phone(phone: str | None) -> str:
    """Hide the middle of a phone number, e.g. 966512341234 -> 9665****1234.

    Always use this when a phone number goes into a log line.
    """
    if not phone:
        return "<none>"
    if len(phone) <= 8:
        # Too short to show both ends safely; show only the last 2 digits.
        return "****" + phone[-2:]
    return f"{phone[:4]}****{phone[-4:]}"
