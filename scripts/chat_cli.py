"""Chat with the AI agent in the terminal: no WhatsApp or Meta setup needed.

Needs only GEMINI_API_KEY in .env (the free tier is fine for testing with sample data).
Uses a temporary database that is deleted afterwards (never data/bot.db).
Alerts that would go to your Telegram are printed here instead, exactly as you'd see them.

Usage (from the project folder, with the virtual environment active):
    python -m scripts.chat_cli --sample     # use the sample sheet (no Google setup needed)
    python -m scripts.chat_cli              # use your Google Sheet from .env
    python -m scripts.chat_cli --sample -v  # also show token usage and tool details
    python -m scripts.chat_cli --excel "data/عروض فريم 111.xlsx"   # your Excel listings file

While chatting:  /new = start a new conversation,  /quit = exit
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace

# The chat tool doesn't talk to WhatsApp, so fill in dummy WhatsApp settings if missing.
for _key in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"):
    os.environ.setdefault(_key, "not-used-by-chat-cli")

from app.agent import HISTORY_LIMIT, Agent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.locations import Locations  # noqa: E402
from app.owner_console import OwnerAlerts  # noqa: E402
from app.properties import LOCATIONS_FILE, SHEET_CONFIG_FILE, PropertyStore, build_property_store  # noqa: E402
from app.sheet import SheetConfig  # noqa: E402

SAMPLE_ROWS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sample_properties.json"


def load_store(use_sample: bool, settings, excel: str | None = None) -> PropertyStore | None:
    if excel:
        store = PropertyStore(None, SheetConfig.load(SHEET_CONFIG_FILE), Locations.load(LOCATIONS_FILE))
        store.use_excel(Path(excel))
    elif use_sample:
        rows = json.loads(SAMPLE_ROWS.read_text(encoding="utf-8"))
        store = PropertyStore(lambda: rows, SheetConfig.load(SHEET_CONFIG_FILE), Locations.load(LOCATIONS_FILE))
    else:
        store = build_property_store(settings)
    if not store.refresh():
        print(f"Could not load properties: {store.last_error or 'GOOGLE_SHEET_ID is not set (try --sample)'}")
        return None
    source = "Excel file" if excel else ("sample sheet" if use_sample else "Google Sheet")
    print(f"Loaded {len(store.properties)} properties ({source}).")
    return store


class TerminalTelegram:
    """Stands in for Telegram: prints what you would receive."""

    owner_id = 0

    def __init__(self):
        self._message_id = 0

    async def send_message(self, text: str, reply_to: int | None = None, thread_id: int | None = None) -> int:
        self._message_id += 1
        print("  [alert to owner]\n" + textwrap.indent(text, "    | "))
        return self._message_id


async def chat(agent: Agent, db: Database, alerts: OwnerAlerts) -> None:
    chat_number = 1
    phone = f"96650000000{chat_number}"
    print("\nType a message as the customer. /new = new conversation, /quit = exit.\n")
    while True:
        try:
            text = (await asyncio.to_thread(input, "Customer: ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/new":
            chat_number += 1
            phone = f"9665000000{chat_number:02d}"
            print("--- new conversation ---\n")
            continue

        db.save_message(phone, "in", "text", text)
        result = await agent.reply(db.get_recent_messages(phone, limit=HISTORY_LIMIT))
        db.save_message(phone, "out", "text", result.text)
        for call in result.tool_calls:
            print(f"  [tool] {call}")
        if any(call.startswith("save_lead") for call in result.tool_calls):
            lead = db.get_lead(phone) or {}
            saved = {k: v for k, v in lead.items() if v and k not in ("phone", "updated_at")}
            print(f"  [lead saved] {saved}")
        if not result.ok:
            print("  [fallback reply: see the error above]")
            await alerts.bot_error(phone, result.error)
        print(f"Bot: {result.text}\n")


def main() -> int:
    # So Arabic prints correctly on Windows (the log goes to stderr).
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", action="store_true", help="use the sample sheet instead of Google")
    parser.add_argument("--excel", help="use this Excel listings file (.xlsx) instead of the sheet")
    parser.add_argument("-v", "--verbose", action="store_true", help="show token usage and tool calls in the log")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="  [%(levelname)s] %(message)s",
    )

    settings = get_settings()
    if not settings.gemini_api_key:
        print("GEMINI_API_KEY is not set in .env. See docs/GEMINI_SETUP.md.")
        return 1
    store = load_store(args.sample, settings, args.excel)
    if store is None:
        return 1

    print(f"Model: {settings.gemini_model} (thinking: {settings.gemini_thinking_level})")
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "chat_cli.db"))
        db.init()
        alerts = OwnerAlerts(SimpleNamespace(db=db, telegram=TerminalTelegram()))
        agent = Agent(settings, store, db=db, alerts=alerts)
        asyncio.run(chat(agent, db, alerts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
