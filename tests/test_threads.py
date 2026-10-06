"""One Telegram thread per customer (topics in the owner's private chat with the bot)."""

import asyncio
import json
import sqlite3

import httpx

from app import owner_console
from app.telegram import TelegramClient
from tests.conftest import OWNER_TELEGRAM_ID, TEST_PHONE_NUMBER_ID, load_fixture

CUSTOMER = "966512341234"
OTHER = "966500000000"


def threads_state(make_state, fake_telegram, **settings):
    fake_telegram.topics_enabled = True
    return make_state(**settings)


def owner_message(text: str, thread_id: int | None = None, sender: int = OWNER_TELEGRAM_ID,
                  reply_to: int | None = None, message_id: int = 1) -> dict:
    message = {"message_id": message_id, "from": {"id": sender}, "chat": {"id": sender}, "text": text}
    if thread_id is not None:
        message.update({"message_thread_id": thread_id, "is_topic_message": True})
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": message}


def run(coro):
    return asyncio.run(coro)


# --- Telegram client payloads ---

def make_client(handler) -> TelegramClient:
    return TelegramClient("123:SECRET", 4242, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_client_thread_methods_send_the_right_payloads():
    seen = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        seen.append((method, body))
        results = {
            "getMe": {"id": 1, "is_bot": True, "has_topics_enabled": True},
            "createForumTopic": {"message_thread_id": 77, "name": body.get("name")},
            "sendMessage": {"message_id": 9},
        }
        return httpx.Response(200, json={"ok": True, "result": results.get(method, True)})

    client = make_client(handler)
    assert run(client.get_me())["has_topics_enabled"] is True
    assert run(client.create_forum_topic("🤖 +966512341234")) == 77
    assert run(client.send_message("hi", thread_id=77)) == 9
    assert run(client.edit_forum_topic(77, "🙋 +966512341234")) is True
    assert run(client.delete_forum_topic(77)) is True

    by_method = dict(seen)
    assert by_method["createForumTopic"] == {"chat_id": 4242, "name": "🤖 +966512341234"}
    assert by_method["sendMessage"]["message_thread_id"] == 77
    assert by_method["editForumTopic"] == {"chat_id": 4242, "message_thread_id": 77, "name": "🙋 +966512341234"}
    assert by_method["deleteForumTopic"] == {"chat_id": 4242, "message_thread_id": 77}


# --- database ---

def test_thread_mapping_and_forget(db):
    db.save_message(CUSTOMER, "in", "text", "hi")
    db.set_thread(CUSTOMER, 501)
    assert db.get_thread(CUSTOMER) == 501
    assert db.phone_for_thread(501) == CUSTOMER
    assert db.customer_data_counts(CUSTOMER)["telegram_thread"] == 1
    db.forget_customer(CUSTOMER)
    assert db.get_thread(CUSTOMER) is None and db.phone_for_thread(501) is None


def test_v2_database_is_upgraded_to_v3(tmp_path):
    from app.db import Database

    path = tmp_path / "v2.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE chats (phone TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'bot', language TEXT,
                created_at TEXT NOT NULL, last_customer_message_at TEXT, last_owner_message_at TEXT);
            INSERT INTO chats VALUES ('966512341234', 'bot', NULL, '2026-09-01T00:00:00+00:00', NULL, NULL);
            PRAGMA user_version = 2;
        """)
    db = Database(str(path))
    db.init()
    assert db.get_chat(CUSTOMER).status == "bot"
    db.set_thread(CUSTOMER, 5)
    assert db.get_thread(CUSTOMER) == 5


# --- one thread per customer ---

def test_thread_is_created_once_and_reused(make_state, fake_telegram, db):
    state = threads_state(make_state, fake_telegram)
    first = run(owner_console.customer_thread(state, CUSTOMER))
    again = run(owner_console.customer_thread(state, CUSTOMER))
    other = run(owner_console.customer_thread(state, OTHER))
    assert first == again != other
    assert fake_telegram.topics[first] == "🤖 966512341234"
    assert len(fake_telegram.topics) == 2


def test_two_messages_at_once_never_create_two_threads(make_state, fake_telegram):
    state = threads_state(make_state, fake_telegram)

    async def both():
        return await asyncio.gather(
            owner_console.customer_thread(state, CUSTOMER),
            owner_console.customer_thread(state, CUSTOMER),
        )

    a, b = run(both())
    assert a == b
    assert len(fake_telegram.topics) == 1


def test_no_threads_when_topics_are_off(make_state, fake_telegram):
    state = make_state()  # topics_enabled stays False
    assert run(owner_console.customer_thread(state, CUSTOMER)) is None
    run(state.alerts.notify(CUSTOMER, "viewing_request", "يبي يعاين"))
    assert fake_telegram.sent_thread_ids == [None]  # the old single-chat behaviour


def test_alert_goes_into_the_customers_thread(make_state, fake_telegram, db):
    state = threads_state(make_state, fake_telegram)
    run(state.alerts.notify(CUSTOMER, "viewing_request", "يبي يعاين P-1007"))
    thread = db.get_thread(CUSTOMER)
    assert thread and fake_telegram.in_thread(thread)[0].startswith("🔔 طلب معاينة")


def test_broken_thread_falls_back_to_main_chat_and_is_forgotten(make_state, fake_telegram, db):
    state = threads_state(make_state, fake_telegram)
    thread = run(owner_console.customer_thread(state, CUSTOMER))
    fake_telegram.broken_threads.add(thread)  # e.g. you deleted the thread by hand
    run(state.alerts.notify(CUSTOMER, "wants_human", "يبي موظف"))
    assert fake_telegram.sent_thread_ids[-1] is None  # delivered in the main chat instead
    assert db.get_thread(CUSTOMER) is None  # a fresh thread will be made next time


# --- typing inside a thread ---

def test_typing_in_a_thread_answers_that_customer_and_takes_over(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    thread = run(owner_console.customer_thread(state, CUSTOMER))
    run(owner_console.customer_thread(state, OTHER))

    run(owner_console.handle_update(owner_message("هلا خالد، أنا من فريق frame", thread_id=thread), state))

    assert fake_wa.sent == [(CUSTOMER, "هلا خالد، أنا من فريق frame")]
    assert db.get_chat(CUSTOMER).status == "human"
    assert fake_telegram.topics[thread].startswith("🙋")  # the icon shows you're handling it
    assert "✅ انرسلت للعميل 966512341234" in fake_telegram.in_thread(thread)[-1]


def test_thread_wins_over_the_technical_reply_to_the_threads_first_message(make_state, fake_telegram, fake_wa):
    state = threads_state(make_state, fake_telegram)
    thread = run(owner_console.customer_thread(state, CUSTOMER))
    update = owner_message("مرحبا", thread_id=thread, reply_to=thread)  # Telegram adds this automatically
    run(owner_console.handle_update(update, state))
    assert fake_wa.sent == [(CUSTOMER, "مرحبا")]


def test_commands_inside_a_thread_need_no_number(make_state, fake_telegram, db):
    state = threads_state(make_state, fake_telegram)
    thread = run(owner_console.customer_thread(state, CUSTOMER))
    db.save_lead(CUSTOMER, {"name": "خالد"})

    run(owner_console.handle_update(owner_message("/human", thread_id=thread), state))
    assert db.get_chat(CUSTOMER).status == "human"
    assert fake_telegram.topics[thread] == "🙋 خالد · 966512341234"

    run(owner_console.handle_update(owner_message("/bot", thread_id=thread), state))
    assert db.get_chat(CUSTOMER).status == "bot"
    assert fake_telegram.topics[thread] == "🤖 خالد · 966512341234"

    run(owner_console.handle_update(owner_message("/lead", thread_id=thread), state))
    assert "الاسم: خالد" in fake_telegram.in_thread(thread)[-1]


def test_forget_inside_a_thread_deletes_the_thread_too(make_state, fake_telegram, db):
    state = threads_state(make_state, fake_telegram)
    db.save_message(CUSTOMER, "in", "text", "hi")
    thread = run(owner_console.customer_thread(state, CUSTOMER))

    run(owner_console.handle_update(owner_message("/forget", thread_id=thread), state))
    preview = fake_telegram.in_thread(thread)[-1]
    assert "موضوعه في تيليجرام" in preview and "/forget تأكيد" in preview
    assert [m.text for m in db.get_recent_messages(CUSTOMER)] == ["hi"]  # nothing deleted yet
    assert thread not in fake_telegram.deleted_topics

    run(owner_console.handle_update(owner_message("/forget تأكيد", thread_id=thread), state))
    assert thread in fake_telegram.deleted_topics
    assert not any(db.customer_data_counts(CUSTOMER).values())
    # The confirmation goes to the main chat (the thread is gone).
    assert fake_telegram.sent_thread_ids[-1] is None
    assert "وانحذف موضوعه في تيليجرام بعد" in fake_telegram.sent[-1][0]


def test_unlinked_thread_gets_a_hint(make_state, fake_telegram, fake_wa):
    state = threads_state(make_state, fake_telegram)
    run(owner_console.handle_update(owner_message("hello?", thread_id=999), state))
    assert fake_wa.sent == []
    assert "مو مربوط بعميل" in fake_telegram.sent[-1][0]


def test_non_owner_in_a_thread_is_ignored(make_state, fake_telegram, fake_wa):
    state = threads_state(make_state, fake_telegram)
    thread = run(owner_console.customer_thread(state, CUSTOMER))
    sent_before = len(fake_telegram.sent)
    run(owner_console.handle_update(owner_message("send this", thread_id=thread, sender=999), state))
    assert fake_wa.sent == []
    assert len(fake_telegram.sent) == sent_before


def test_status_shows_thread_mode(make_state, fake_telegram):
    run(owner_console.handle_update(owner_message("/status"), make_state()))
    assert "موضوع لكل عميل: غير مفعّل" in fake_telegram.sent[-1][0]
    run(owner_console.handle_update(owner_message("/status"), threads_state(make_state, fake_telegram)))
    assert "موضوع لكل عميل: مفعّل" in fake_telegram.sent[-1][0]


def test_learning_the_name_updates_the_thread_title(make_state, fake_telegram, db):
    state = threads_state(make_state, fake_telegram)
    thread = run(owner_console.customer_thread(state, CUSTOMER))
    db.save_lead(CUSTOMER, {"name": "سارة"})
    run(state.alerts.lead_updated(CUSTOMER, {"name": "سارة"}))
    assert fake_telegram.topics[thread] == "🤖 سارة · 966512341234"


# --- live copy of conversations (the full webhook flow) ---

def _process(state, payload):
    from app.main import process_payload

    run(process_payload(payload, state, TEST_PHONE_NUMBER_ID))


def test_bot_reply_is_copied_into_the_thread_as_one_message(make_state, fake_telegram, db):
    from tests.conftest import AGENT_REPLY

    state = threads_state(make_state, fake_telegram)
    _process(state, load_fixture("text_message.json"))
    thread = db.get_thread(CUSTOMER)
    assert fake_telegram.in_thread(thread) == [f"👤 السلام عليكم، عندكم شقق للإيجار في النرجس؟\n🤖 {AGENT_REPLY}"]


def test_photo_that_cant_be_downloaded_is_noted_in_the_thread(make_state, fake_telegram, db):
    state = threads_state(make_state, fake_telegram)
    _process(state, load_fixture("image_message.json"))  # Meta has no file for it (fake)
    note, bot_line = fake_telegram.in_thread(db.get_thread(CUSTOMER))
    assert note == "👤 📎 ما قدرت أحوّل صورة: ما قدرت أنزّله من واتساب."
    assert bot_line.startswith("🤖 ") and "ممكن تكتب سؤالك" in bot_line


def test_mirror_can_be_switched_off_but_alerts_still_use_threads(make_state, fake_telegram, fake_agent, db):
    state = threads_state(make_state, fake_telegram, telegram_live_mirror=False)
    _process(state, load_fixture("text_message.json"))
    assert fake_telegram.sent == []  # no live copy
    fake_agent.fail_with = "timed out"
    payload = load_fixture("text_message.json")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.SECOND"
    _process(state, payload)
    assert fake_telegram.in_thread(db.get_thread(CUSTOMER))[0].startswith("⚠️ البوت ما قدر يرد")


def test_human_mode_message_goes_into_the_thread_without_a_bot_reply(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    db.take_over(CUSTOMER)
    _process(state, load_fixture("text_message.json"))
    thread = db.get_thread(CUSTOMER)
    assert fake_wa.sent == []
    assert fake_telegram.in_thread(thread) == ["👤 السلام عليكم، عندكم شقق للإيجار في النرجس؟"]


def test_status_rechecks_topics_after_switching_them_on_in_botfather(make_state, fake_telegram):
    state = make_state()
    assert not fake_telegram.topics_enabled
    fake_telegram_real_value = True  # you switched topics on in @BotFather meanwhile

    async def get_me():
        return {"has_topics_enabled": fake_telegram_real_value}

    fake_telegram.get_me = get_me
    run(owner_console.handle_update(owner_message("/status"), state))
    assert fake_telegram.topics_enabled is True
    assert "موضوع لكل عميل: مفعّل" in fake_telegram.sent[-1][0]
