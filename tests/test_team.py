"""Your team: employees in the Telegram group who can answer customers (added with /staff)."""

import asyncio

from app import owner_console
from tests.conftest import OWNER_TELEGRAM_ID
from tests.test_owner_console import english_words

CUSTOMER = "966512341234"
GROUP = -1001234567890
AHMAD = 777
STRANGER = 888


def run(coro):
    return asyncio.run(coro)


def group_state(make_state, fake_telegram):
    fake_telegram.chat_id = GROUP
    fake_telegram.topics_enabled = True
    return make_state()


def msg(text: str = "", sender: int = OWNER_TELEGRAM_ID, thread_id: int | None = None,
        reply_to_sender: int | None = None, first_name: str = "Ahmad", **content) -> dict:
    message = {"message_id": 5, "from": {"id": sender, "first_name": "x"}, "chat": {"id": GROUP}, **content}
    if text:
        message["text"] = text
    if thread_id is not None:
        message.update({"message_thread_id": thread_id, "is_topic_message": True})
    if reply_to_sender is not None:
        message["reply_to_message"] = {"message_id": 4, "from": {"id": reply_to_sender, "first_name": first_name}}
    return {"update_id": 1, "message": message}


def add_ahmad(state):
    run(owner_console.handle_update(msg("/staff إضافة أحمد", reply_to_sender=AHMAD), state))


def customer_topic(state, db) -> int:
    db.get_or_create_chat(CUSTOMER)
    return run(owner_console.customer_thread(state, CUSTOMER))


# --- adding and removing ---

def test_owner_adds_an_employee_by_replying_to_their_message(make_state, fake_telegram, db):
    state = group_state(make_state, fake_telegram)
    add_ahmad(state)
    assert fake_telegram.sent[-1][0].startswith("✅ أحمد صار من الفريق")
    run(owner_console.handle_update(msg("/staff"), state))
    assert f"• أحمد ({AHMAD})" in fake_telegram.sent[-1][0]


def test_adding_without_a_reply_explains_how(make_state, fake_telegram):
    state = group_state(make_state, fake_telegram)
    run(owner_console.handle_update(msg("/staff إضافة أحمد"), state))
    assert "سوّ رد على رسالته" in fake_telegram.sent[-1][0]
    run(owner_console.handle_update(msg("/staff"), state))
    assert "ما فيه موظفين" in fake_telegram.sent[-1][0]


def test_the_bot_itself_or_a_topic_start_is_never_added(make_state, fake_telegram, db):
    state = group_state(make_state, fake_telegram)
    upd = msg("/staff إضافة", reply_to_sender=12345)
    upd["message"]["reply_to_message"]["from"]["is_bot"] = True  # e.g. the topic's first message
    run(owner_console.handle_update(upd, state))
    assert owner_console._team(state) == {}


def test_employee_name_defaults_to_their_telegram_name(make_state, fake_telegram):
    state = group_state(make_state, fake_telegram)
    run(owner_console.handle_update(msg("/staff add", reply_to_sender=AHMAD, first_name="Ahmad"), state))
    assert owner_console._team(state) == {AHMAD: "Ahmad"}


def test_only_the_owner_can_add_or_remove_employees(make_state, fake_telegram):
    state = group_state(make_state, fake_telegram)
    add_ahmad(state)
    run(owner_console.handle_update(msg("/staff إضافة غريب", sender=AHMAD, reply_to_sender=STRANGER), state))
    assert fake_telegram.sent[-1][0] == "إضافة وحذف الموظفين للمالك بس."
    run(owner_console.handle_update(msg("/staff حذف أحمد", sender=AHMAD), state))
    assert owner_console._team(state) == {AHMAD: "أحمد"}


def test_removed_employee_is_ignored_again(make_state, fake_telegram, fake_wa, db):
    state = group_state(make_state, fake_telegram)
    add_ahmad(state)
    run(owner_console.handle_update(msg("/staff حذف أحمد"), state))
    assert fake_telegram.sent[-1][0] == "🗑 أحمد ما عاد من الفريق."
    thread = customer_topic(state, db)
    run(owner_console.handle_update(msg("hello", sender=AHMAD, thread_id=thread), state))
    assert fake_wa.sent == []


def test_team_survives_a_restart(make_state, fake_telegram, db):
    add_ahmad(group_state(make_state, fake_telegram))
    assert owner_console._team(make_state()) == {AHMAD: "أحمد"}  # a fresh state, same database


# --- employees answering customers ---

def test_employee_reply_reaches_the_customer_exactly_as_written(make_state, fake_telegram, fake_wa, db):
    state = group_state(make_state, fake_telegram)
    add_ahmad(state)
    thread = customer_topic(state, db)
    run(owner_console.handle_update(msg("هلا، الشقة متوفرة", sender=AHMAD, thread_id=thread), state))
    assert fake_wa.sent == [(CUSTOMER, "هلا، الشقة متوفرة")]  # no name added
    assert db.get_chat(CUSTOMER).status == "human"
    assert fake_telegram.in_thread(thread)[-1].startswith("✅ انرسلت للعميل 966512341234 (أحمد).")


def test_owner_confirmation_has_no_name(make_state, fake_telegram, fake_wa, db):
    state = group_state(make_state, fake_telegram)
    thread = customer_topic(state, db)
    run(owner_console.handle_update(msg("هلا", thread_id=thread), state))
    assert fake_telegram.in_thread(thread)[-1].startswith("✅ انرسلت للعميل 966512341234.")


def test_employee_photo_keeps_its_caption(make_state, fake_telegram, fake_wa, db):
    state = group_state(make_state, fake_telegram)
    add_ahmad(state)
    thread = customer_topic(state, db)
    fake_telegram.downloadable["P1"] = b"JPEG"
    photo = [{"file_id": "P1", "width": 800, "height": 600, "file_size": 1000}]
    run(owner_console.handle_update(msg(sender=AHMAD, thread_id=thread, photo=photo, caption="الصالة"), state))
    assert fake_wa.sent_media[0][:4] == (CUSTOMER, "image", "media.UP1", "الصالة")


def test_employees_can_use_all_the_owners_commands(make_state, fake_telegram, db):
    state = group_state(make_state, fake_telegram)
    add_ahmad(state)
    run(owner_console.handle_update(msg("/pause", sender=AHMAD), state))
    assert db.is_paused()
    run(owner_console.handle_update(msg(f"/human {CUSTOMER}", sender=AHMAD), state))
    assert db.get_chat(CUSTOMER).status == "human"
    run(owner_console.handle_update(msg(f"/bot {CUSTOMER}", sender=AHMAD), state))
    assert db.get_chat(CUSTOMER).status == "bot"


# --- still ignored ---

def test_people_not_on_the_team_are_ignored(make_state, fake_telegram, fake_wa, db):
    state = group_state(make_state, fake_telegram)
    add_ahmad(state)
    thread = customer_topic(state, db)
    sent_before = len(fake_telegram.sent)
    run(owner_console.handle_update(msg("hello", sender=STRANGER, thread_id=thread), state))
    run(owner_console.handle_update(msg("/pause", sender=STRANGER), state))
    assert fake_wa.sent == [] and not db.is_paused()
    assert len(fake_telegram.sent) == sent_before


def test_employees_private_chat_with_the_bot_is_ignored(make_state, fake_telegram, db):
    state = group_state(make_state, fake_telegram)
    add_ahmad(state)
    upd = msg("/pause", sender=AHMAD)
    upd["message"]["chat"]["id"] = AHMAD  # their private chat, not the group
    run(owner_console.handle_update(upd, state))
    assert not db.is_paused()


def test_team_texts_are_arabic(make_state, fake_telegram):
    state = group_state(make_state, fake_telegram)
    run(owner_console.handle_update(msg("/staff"), state))
    run(owner_console.handle_update(msg("/staff إضافة"), state))
    add_ahmad(state)
    run(owner_console.handle_update(msg("/staff"), state))
    run(owner_console.handle_update(msg("/staff حذف فلان"), state))
    run(owner_console.handle_update(msg("/staff إضافة", sender=AHMAD, reply_to_sender=STRANGER), state))
    run(owner_console.handle_update(msg("/help"), state))
    for text, _ in fake_telegram.sent:
        assert english_words(text) == [], text
