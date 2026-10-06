"""Admin helper: permanently delete everything stored about one customer (PDPL request).

Same as the Telegram command /forget, for when you're on the server.

Usage (from the project folder, with the virtual environment active):
    python -m scripts.forget_customer 966512345678
    python -m scripts.forget_customer 0512345678      (local format works too)

It shows what will be deleted and asks you to type "y" to confirm.
Stop the app first if you can, or at least make sure the customer isn't mid-conversation.
"""

import sys

from app.config import get_settings
from app.db import Database
from app.takeover import normalize_phone


def main(args: list[str]) -> int:
    phone = normalize_phone(args[0]) if len(args) == 1 else None
    if not phone:
        print(__doc__)
        return 1

    db = Database(get_settings().db_path)
    db.init()
    counts = db.customer_data_counts(phone)
    if not any(counts.values()):
        print(f"Nothing is stored for +{phone}.")
        return 0

    print(f"This PERMANENTLY deletes everything stored about +{phone}:")
    print(f"  {counts['messages']} messages, lead: {'yes' if counts['lead'] else 'no'}, "
          f"chat: {'yes' if counts['chat'] else 'no'}, Telegram reply links: {counts['telegram_links']}")
    if input("Type y to delete: ").strip().lower() != "y":
        print("Cancelled. Nothing was deleted.")
        return 1

    db.forget_customer(phone)
    print("Deleted. Remember to delete their messages in your Telegram chat with the bot by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
