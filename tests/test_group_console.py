"""Your console in a Telegram group with Topics (TELEGRAM_GROUP_ID): one topic per customer."""

import asyncio
import json
import re

import httpx

from app import owner_console, replies
from app.telegram import TelegramClient
from tests.conftest import OWNER_TELEGRAM_ID, TEST_PHONE_NUMBER_ID, load_fixture

CUSTOMER = "966512341234"
GROUP = -1001234567890
STAFF = 777


def run(coro):
    return asyncio.run(coro)


def group_state(make_state, fake_telegram):
    fake_telegram.chat_id = GROUP
    fake_telegram.topics_enabled = True
    return make_state()


def group_message(text: str, sender: int = OWNER_TELEGRAM_ID, chat: int = GROUP, thread_id: int | None = None) -> dict:
    message = {"message_id": 3, "from": {"id": sender}, "chat": {"id": chat, "type": "supergroup"}, "text": text}
    if thread_id is not None:
        message.update({"message_thread_id": thread_id, "is_topic_message": True})
    return {"update_id": 1, "message": message}


# --- starting up ---

def test_first_start_in_the_private_chat_keeps_existing_threads(make_state, fake_telegram, db):
    db.set_thread(CUSTOMER, 55)
    db.link_telegram_message(900, CUSTOMER)
    state = make_state()  # console = your private chat, like before this setting existed
    run(owner_console.start_console(state))
    assert db.get_thread(CUSTOMER) == 55
    assert db.phone_for_telegram_message(900) == CUSTOMER
    assert db.get_state("telegram_console_chat") == str(OWNER_TELEGRAM_ID)
    assert fake_telegram.commands_set == 1


def test_moving_the_console_to_a_group_starts_fresh_topics(make_state, fake_telegram, db):
    db.set_thread(CUSTOMER, 55)  # a thread in your private chat
    db.link_telegram_message(900, CUSTOMER)
    state = group_state(make_state, fake_telegram)
    run(owner_console.start_console(state))
    assert db.get_thread(CUSTOMER) is None  # topic IDs from another chat mean nothing here
    assert db.phone_for_telegram_message(900) is None
    assert db.get_state("telegram_console_chat") == str(GROUP)
    assert fake_telegram.topics_enabled is True  # the group has Topics (is_forum)

    db.set_thread(CUSTOMER, 12)
    run(owner_console.start_console(state))  # a restart in the same group keeps them
    assert db.get_thread(CUSTOMER) == 12


def test_group_without_topics_is_reported(make_state, fake_telegram):
    state = group_state(make_state, fake_telegram)
    fake_telegram.topics_enabled = False
    run(owner_console.handle_update(group_message("/status"), state))
    assert "فعّل المواضيع في إعدادات المجموعة" in fake_telegram.sent[-1][0]
    fake_telegram.topics_enabled = True
    run(owner_console.handle_update(group_message("/status"), state))
    assert "مفعّل (في المجموعة" in fake_telegram.sent[-1][0]


# --- customers get topics in the group ---

def test_customer_message_creates_a_topic_in_the_group(make_state, fake_telegram, db):
    from app.main import process_payload

    state = group_state(make_state, fake_telegram)
    run(process_payload(load_fixture("text_message.json"), state, TEST_PHONE_NUMBER_ID))
    thread = db.get_thread(CUSTOMER)
    assert thread in fake_telegram.topics
    assert fake_telegram.in_thread(thread)[0].startswith("👤 ")
    assert set(fake_telegram.sent_chat_ids) == {GROUP}


def test_you_answer_by_typing_in_the_customers_topic(make_state, fake_telegram, fake_wa, db):
    state = group_state(make_state, fake_telegram)
    db.get_or_create_chat(CUSTOMER)
    thread = run(owner_console.customer_thread(state, CUSTOMER))
    run(owner_console.handle_update(group_message("هلا، أنا فهد", thread_id=thread), state))
    assert fake_wa.sent == [(CUSTOMER, "هلا، أنا فهد")]
    assert db.get_chat(CUSTOMER).status == "human"


# --- security: only you, only in the console chat ---

def test_other_group_members_cannot_answer_customers_or_run_commands(make_state, fake_telegram, fake_wa, db, caplog):
    state = group_state(make_state, fake_telegram)
    db.get_or_create_chat(CUSTOMER)
    thread = run(owner_console.customer_thread(state, CUSTOMER))
    run(owner_console.handle_update(group_message("hi from staff", sender=STAFF, thread_id=thread), state))
    run(owner_console.handle_update(group_message("/pause", sender=STAFF), state))
    run(owner_console.handle_update(group_message("/chatid", sender=STAFF), state))
    assert fake_wa.sent == []
    assert not db.is_paused()
    assert fake_telegram.sent == []
    assert "Ignored Telegram message from user id 777" in caplog.text


def test_your_private_chat_is_ignored_once_the_console_is_a_group(make_state, fake_telegram, db):
    state = group_state(make_state, fake_telegram)
    run(owner_console.handle_update(group_message("/pause", chat=OWNER_TELEGRAM_ID), state))
    assert not db.is_paused()
    assert fake_telegram.sent == []


def test_chatid_tells_you_the_id_of_any_chat(make_state, fake_telegram):
    state = make_state()  # console is still your private chat
    new_group = -1009999
    run(owner_console.handle_update(group_message("/chatid", chat=new_group, thread_id=4), state))
    text, reply_to = fake_telegram.sent[-1]
    assert text == f"رقم هذي المحادثة:\nTELEGRAM_GROUP_ID={new_group}"
    assert fake_telegram.sent_chat_ids[-1] == new_group  # answered where you asked
    assert fake_telegram.sent_thread_ids[-1] == 4 and reply_to == 3


# --- the Telegram client in group mode ---

def client_for(handler, chat_id=None) -> TelegramClient:
    return TelegramClient("123:SECRET", OWNER_TELEGRAM_ID, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                          chat_id=chat_id)


def recording_handler(seen, results=None):
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") else {}
        seen.append((method, body))
        result = (results or {}).get(method, {"message_id": 1, "message_thread_id": 9})
        return httpx.Response(200, json={"ok": True, "result": result})

    return handler


def test_client_sends_everything_to_the_group():
    seen = []
    client = client_for(recording_handler(seen), chat_id=GROUP)
    run(client.send_message("hi", thread_id=9))
    run(client.create_forum_topic("🤖 966512341234"))
    run(client.delete_forum_topic(9))
    assert [body["chat_id"] for _, body in seen] == [GROUP, GROUP, GROUP]
    assert client.is_group


def test_client_checks_topics_with_getchat_in_a_group_and_getme_in_private():
    seen = []
    client = client_for(recording_handler(seen, {"getChat": {"id": GROUP, "is_forum": True}}), chat_id=GROUP)
    assert run(client.refresh_topics_enabled()) is True
    assert seen[-1] == ("getChat", {"chat_id": GROUP})

    seen.clear()
    private = client_for(recording_handler(seen, {"getMe": {"id": 1, "has_topics_enabled": False}}))
    assert run(private.refresh_topics_enabled()) is False
    assert seen[-1][0] == "getMe"
    assert not private.is_group and private.chat_id == OWNER_TELEGRAM_ID


def test_client_keeps_the_old_topics_value_when_telegram_is_unreachable():
    def failing(request):
        raise httpx.ConnectError("down")

    client = client_for(failing, chat_id=GROUP)
    client.topics_enabled = True
    assert run(client.refresh_topics_enabled()) is True


def test_command_menu_is_arabic_and_also_set_for_the_group():
    seen = []
    run(client_for(recording_handler(seen), chat_id=GROUP).set_my_commands())
    assert [method for method, _ in seen] == ["setMyCommands", "setMyCommands"]
    assert seen[1][1]["scope"] == {"type": "chat", "chat_id": GROUP}
    for command in seen[0][1]["commands"]:
        description = re.sub(r"/[a-z]+|9665X+", "", command["description"])
        assert not re.search(r"[A-Za-z]", description), command


def test_client_can_answer_in_another_chat():
    seen = []
    run(client_for(recording_handler(seen)).send_message("x", chat_id=-100555))
    assert seen[0][1]["chat_id"] == -100555


# --- the customer side: Arabic only ---

def test_fixed_replies_are_arabic_only():
    texts = [replies.FALLBACK_REPLY, replies.NON_TEXT_REPLY, *replies.MEDIA_RECEIVED.values()]
    for text in texts:
        assert not re.search(r"[A-Za-z]", text), text
