"""Admin helper: view or change a chat's status from the command line.

Until the Telegram commands exist (Phase 5), use this to test human takeover.

Usage (from the project folder, with the virtual environment active):
    python -m scripts.chat_status list               # chats in human mode
    python -m scripts.chat_status human 966512341234 # you take over this chat
    python -m scripts.chat_status bot 966512341234   # hand it back to the bot
"""

import sys

from app.config import get_settings
from app.db import Database


def main(args: list[str]) -> int:
    db = Database(get_settings().db_path)
    db.init()

    if args == ["list"]:
        chats = db.list_chats_by_status("human")
        if not chats:
            print("No chats in human mode.")
        for chat in chats:
            print(f"{chat.phone}  (last customer message: {chat.last_customer_message_at})")
        return 0

    if len(args) == 2 and args[0] in ("bot", "human"):
        status, phone = args
        if status == "human":
            db.take_over(phone)  # also starts the HUMAN_TIMEOUT_HOURS clock
        else:
            db.hand_back(phone)
        print(f"Chat {phone} is now in {status} mode.")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
