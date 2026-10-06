import json

from app import replies
from app.db import Database
from app.webhook import IncomingMessage, StatusUpdate, extract_events
from tests.conftest import (
    AGENT_REPLY,
    TEST_PHONE_NUMBER_ID,
    TEST_VERIFY_TOKEN,
    load_fixture,
    sign,
)


def post_webhook(client, payload, signature=None):
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    headers = {"Content-Type": "application/json"}
    headers["X-Hub-Signature-256"] = signature if signature is not None else sign(body)
    return client.post("/webhook", content=body, headers=headers)


# --- GET /webhook (Meta verification handshake) ---

def test_verification_returns_challenge(client):
    resp = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": TEST_VERIFY_TOKEN, "hub.challenge": "12345"},
    )
    assert resp.status_code == 200
    assert resp.text == "12345"


def test_verification_with_wrong_token_is_forbidden(client):
    resp = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
    )
    assert resp.status_code == 403


def test_verification_without_params_is_forbidden(client):
    assert client.get("/webhook").status_code == 403


# --- POST /webhook ---

def test_text_message_is_answered_by_the_agent(client, fake_wa):
    resp = post_webhook(client, load_fixture("text_message.json"))
    assert resp.status_code == 200
    assert fake_wa.read == ["wamid.TEXT1"]
    assert fake_wa.sent == [("966512341234", AGENT_REPLY)]


def test_bad_signature_is_rejected_and_nothing_sent(client, fake_wa):
    resp = post_webhook(client, load_fixture("text_message.json"), signature="sha256=deadbeef")
    assert resp.status_code == 403
    assert fake_wa.sent == []


def test_missing_signature_is_rejected(client, fake_wa):
    body = json.dumps(load_fixture("text_message.json")).encode()
    resp = client.post("/webhook", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 403
    assert fake_wa.sent == []


def test_status_update_sends_nothing(client, fake_wa):
    resp = post_webhook(client, load_fixture("status_delivered.json"))
    assert resp.status_code == 200
    assert fake_wa.sent == []
    assert fake_wa.read == []


def test_image_message_is_stored_and_customer_asked_to_type(client, fake_wa, db):
    # Meta has no file for this photo (fake), so it can't reach the owner: "please type".
    resp = post_webhook(client, load_fixture("image_message.json"))
    assert resp.status_code == 200
    assert fake_wa.sent == [("966512341234", replies.NON_TEXT_REPLY)]
    stored = db.get_recent_messages("966512341234")
    assert [(m.direction, m.type) for m in stored] == [("in", "image"), ("out", "text")]


def test_message_for_other_phone_number_id_is_ignored(client, fake_wa):
    payload = load_fixture("text_message.json")
    payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999"
    resp = post_webhook(client, payload)
    assert resp.status_code == 200
    assert fake_wa.sent == []


def test_malformed_json_with_valid_signature_returns_400(client, fake_wa):
    resp = post_webhook(client, b"{not json")
    assert resp.status_code == 400
    assert fake_wa.sent == []


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["sheet"]["status"] == "not_configured"  # tests never use a real sheet


def test_health_reports_db_error(client, tmp_path):
    from app.main import app

    # A folder is not a valid database file, so the check fails.
    app.state.db = Database(str(tmp_path))
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["db"] == "error"


# --- Phase 2: storage, dedupe, chat status ---

def test_messages_are_stored_in_both_directions(client, db):
    post_webhook(client, load_fixture("text_message.json"))
    stored = db.get_recent_messages("966512341234")
    assert [(m.direction, m.text, m.wa_message_id) for m in stored] == [
        ("in", "السلام عليكم، عندكم شقق للإيجار في النرجس؟", "wamid.TEXT1"),
        ("out", AGENT_REPLY, "wamid.FAKE_OUT1"),
    ]


def test_new_chat_starts_in_bot_mode(client, db):
    post_webhook(client, load_fixture("text_message.json"))
    chat = db.get_chat("966512341234")
    assert chat.status == "bot"
    assert chat.last_customer_message_at is not None


def test_duplicate_delivery_is_answered_only_once(client, fake_wa, db):
    payload = load_fixture("text_message.json")
    assert post_webhook(client, payload).status_code == 200
    assert post_webhook(client, payload).status_code == 200  # Meta retry, same message ID
    assert fake_wa.sent == [("966512341234", AGENT_REPLY)]
    assert len(db.get_recent_messages("966512341234")) == 2  # one "in", one "out"


def test_human_mode_chat_is_stored_but_not_answered(client, fake_wa, db):
    db.take_over("966512341234")
    post_webhook(client, load_fixture("text_message.json"))
    assert fake_wa.sent == []
    assert fake_wa.read == []  # leave it unread for you
    stored = db.get_recent_messages("966512341234")
    assert [m.direction for m in stored] == ["in"]


def test_human_mode_only_affects_that_chat(client, fake_wa, db):
    db.take_over("966500000000")  # a different customer
    post_webhook(client, load_fixture("text_message.json"))
    assert fake_wa.sent == [("966512341234", AGENT_REPLY)]


def test_chat_back_in_bot_mode_is_answered_again(client, fake_wa, db):
    db.take_over("966512341234")
    db.set_chat_status("966512341234", "bot")
    post_webhook(client, load_fixture("text_message.json"))
    assert fake_wa.sent == [("966512341234", AGENT_REPLY)]


def test_reaction_is_stored_but_not_answered(client, fake_wa, db):
    payload = load_fixture("text_message.json")
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg["type"] = "reaction"
    msg.pop("text")
    msg["reaction"] = {"message_id": "wamid.OUT1", "emoji": "👍"}
    post_webhook(client, payload)
    assert fake_wa.sent == []
    assert [m.type for m in db.get_recent_messages("966512341234")] == ["reaction"]


def test_failed_send_is_not_stored_as_sent(client, fake_wa, db):
    fake_wa.fail_sends = True
    post_webhook(client, load_fixture("text_message.json"))
    assert [m.direction for m in db.get_recent_messages("966512341234")] == ["in"]


# --- extract_events (parsing only) ---

def test_extract_text_message():
    events = extract_events(load_fixture("text_message.json"), TEST_PHONE_NUMBER_ID)
    assert events == [
        IncomingMessage(
            wa_message_id="wamid.TEXT1",
            from_phone="966512341234",
            type="text",
            text="السلام عليكم، عندكم شقق للإيجار في النرجس؟",
            timestamp="1749416383",
            profile_name="Test Customer",
        )
    ]


def test_extract_status_update():
    events = extract_events(load_fixture("status_delivered.json"), TEST_PHONE_NUMBER_ID)
    assert events == [
        StatusUpdate(wa_message_id="wamid.OUT1", status="delivered", recipient_phone="966512341234")
    ]


def test_extract_ignores_other_fields_and_empty_payloads():
    assert extract_events({}, TEST_PHONE_NUMBER_ID) == []
    payload = {"entry": [{"changes": [{"field": "account_update", "value": {}}]}]}
    assert extract_events(payload, TEST_PHONE_NUMBER_ID) == []


# --- Phase 4: the agent ---

def test_agent_gets_only_this_customers_history_including_new_message(client, fake_agent, db):
    db.save_message("966500000000", "in", "text", "another customer's message")
    db.save_message("966512341234", "in", "text", "earlier message")
    db.save_message("966512341234", "out", "text", "earlier reply")

    post_webhook(client, load_fixture("text_message.json"))

    [history] = fake_agent.histories
    assert {m.phone for m in history} == {"966512341234"}
    assert [m.text for m in history] == [
        "earlier message",
        "earlier reply",
        "السلام عليكم، عندكم شقق للإيجار في النرجس؟",  # the message that just arrived
    ]


def test_agent_is_not_used_for_non_text_or_human_mode(client, fake_agent, db):
    post_webhook(client, load_fixture("image_message.json"))
    db.take_over("966512341234")
    post_webhook(client, load_fixture("text_message.json"))
    assert fake_agent.histories == []


def test_health_reports_ai_status(client):
    assert client.get("/health").json()["ai"] == "configured"


def _text_payload(message_id: str, text: str, phone: str = "966512341234") -> dict:
    payload = load_fixture("text_message.json")
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"][0].update({"id": message_id, "from": phone, "text": {"body": text}})
    value["contacts"][0]["wa_id"] = phone
    return payload


def test_two_quick_messages_from_one_customer_are_answered_in_order(fake_agent, make_state):
    """The second reply must see the first one, so the agent never runs twice at once per chat."""
    import asyncio

    from app.main import process_payload

    fake_agent.delay = 0.05
    state = make_state()

    async def both():
        await asyncio.gather(
            process_payload(_text_payload("wamid.A", "first"), state, TEST_PHONE_NUMBER_ID),
            process_payload(_text_payload("wamid.B", "second"), state, TEST_PHONE_NUMBER_ID),
        )

    asyncio.run(both())

    assert fake_agent.max_running == 1
    second_history = [m.text for m in fake_agent.histories[1]]
    assert second_history == ["first", AGENT_REPLY, "second"]


def test_different_customers_are_answered_in_parallel(fake_agent, make_state):
    import asyncio

    from app.main import process_payload

    fake_agent.delay = 0.05
    state = make_state()

    async def both():
        await asyncio.gather(
            process_payload(_text_payload("wamid.A", "hi", "966511111111"), state, TEST_PHONE_NUMBER_ID),
            process_payload(_text_payload("wamid.B", "hi", "966522222222"), state, TEST_PHONE_NUMBER_ID),
        )

    asyncio.run(both())
    assert fake_agent.max_running == 2


# --- Phase 5: takeover, forwarding to Telegram, pause, schedule, echo ---

def _process(state, payload) -> None:
    import asyncio

    from app.main import process_payload

    asyncio.run(process_payload(payload, state, TEST_PHONE_NUMBER_ID))


def test_human_mode_message_is_forwarded_not_answered(make_state, fake_wa, fake_agent, fake_telegram, db):
    db.take_over("966512341234")
    _process(make_state(), load_fixture("text_message.json"))

    assert fake_agent.histories == []
    assert fake_wa.sent == [] and fake_wa.read == []  # no reply, left unread for you
    [(text, _)] = fake_telegram.sent
    assert text.startswith("📩 966512341234 (Test Customer) · استلمت هذي المحادثة")
    assert "عندكم شقق للإيجار" in text
    # Pressing Reply on it in Telegram will reach this customer:
    assert db.phone_for_telegram_message(fake_telegram.last_message_id) == "966512341234"


def test_takeover_only_affects_that_chat(make_state, fake_wa, fake_telegram, db):
    db.take_over("966500000000")  # a different customer
    _process(make_state(), load_fixture("text_message.json"))
    assert fake_wa.sent == [("966512341234", AGENT_REPLY)]
    assert fake_telegram.sent == []


def test_paused_bot_forwards_everything(make_state, fake_wa, fake_agent, fake_telegram, db):
    db.set_paused(True)
    _process(make_state(), load_fixture("text_message.json"))
    _process(make_state(), _text_payload("wamid.OTHER", "hi", "966500000000"))

    assert fake_agent.histories == [] and fake_wa.sent == []
    assert len(fake_telegram.sent) == 2
    assert all("البوت موقوف" in text for text, _ in fake_telegram.sent)


def test_schedule_mode_during_work_hours_forwards(make_state, fake_wa, fake_telegram):
    state = make_state(availability_mode="schedule", work_hours="00:00-24:00")  # always "work time"
    _process(state, load_fixture("text_message.json"))
    assert fake_wa.sent == []
    assert "وقت دوامك" in fake_telegram.sent[0][0]


def test_schedule_mode_outside_work_hours_bot_answers(make_state, fake_wa, fake_telegram, monkeypatch):
    from datetime import UTC, datetime

    import app.main as main_module

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 25, 19, 0, tzinfo=UTC)  # 22:00 in Riyadh

    monkeypatch.setattr(main_module, "datetime", FixedTime)
    state = make_state(availability_mode="schedule", work_hours="09:00-17:00")
    _process(state, load_fixture("text_message.json"))
    assert fake_wa.sent == [("966512341234", AGENT_REPLY)]
    assert fake_telegram.sent == []


def test_expired_takeover_returns_to_bot_and_tells_you(make_state, fake_wa, fake_telegram, db):
    import sqlite3
    from datetime import UTC, datetime, timedelta

    db.take_over("966512341234")
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat(timespec="seconds")
    with sqlite3.connect(db.path) as conn:
        conn.execute("UPDATE chats SET last_owner_message_at = ?", (old,))

    _process(make_state(human_timeout_hours=24), load_fixture("text_message.json"))

    assert db.get_chat("966512341234").status == "bot"
    assert fake_wa.sent == [("966512341234", AGENT_REPLY)]  # the bot answers again
    assert "رجع للبوت" in fake_telegram.sent[0][0]


def test_recent_takeover_is_not_expired(make_state, fake_wa, db):
    db.take_over("966512341234")
    _process(make_state(human_timeout_hours=24), load_fixture("text_message.json"))
    assert db.get_chat("966512341234").status == "human"
    assert fake_wa.sent == []


def test_forwarding_without_telegram_still_stores(make_state, fake_wa, db):
    db.take_over("966512341234")
    state = make_state()
    state.telegram = None
    _process(state, load_fixture("text_message.json"))
    assert fake_wa.sent == []
    assert [m.direction for m in db.get_recent_messages("966512341234")] == ["in"]


def test_reaction_in_human_mode_is_not_forwarded(make_state, fake_telegram, db):
    db.take_over("966512341234")
    payload = load_fixture("text_message.json")
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.update({"type": "reaction", "reaction": {"message_id": "x", "emoji": "👍"}})
    msg.pop("text")
    _process(make_state(), payload)
    assert fake_telegram.sent == []


# --- coexistence echo (dormant Plan A) ---

def test_extract_owner_echo():
    from app.webhook import OwnerEcho

    events = extract_events(load_fixture("owner_echo.json"), TEST_PHONE_NUMBER_ID)
    assert events == [
        OwnerEcho("wamid.ECHO1", "966512341234", "text", "هلا، أنا فهد من frame. أتواصل معك الحين")
    ]


def test_owner_echo_takes_over_the_chat(client, fake_wa, db):
    assert post_webhook(client, load_fixture("owner_echo.json")).status_code == 200
    assert post_webhook(client, load_fixture("owner_echo.json")).status_code == 200  # duplicate

    chat = db.get_chat("966512341234")
    assert chat.status == "human" and chat.last_owner_message_at
    stored = db.get_recent_messages("966512341234")
    assert [(m.direction, m.wa_message_id) for m in stored] == [("owner", "wamid.ECHO1")]
    assert fake_wa.sent == []

    # The customer's next message is now yours, not the bot's.
    post_webhook(client, load_fixture("text_message.json"))
    assert fake_wa.sent == []


def test_health_reports_telegram_and_pause(client, db):
    body = client.get("/health").json()
    assert body["telegram"] == "configured"
    assert body["paused"] is False
    db.set_paused(True)
    assert client.get("/health").json()["paused"] is True



# --- Phase 6: alert when the bot can't answer ---

def test_fallback_reply_alerts_the_owner(make_state, fake_wa, fake_agent, fake_telegram, db):
    fake_agent.fail_with = "timed out"
    _process(make_state(), load_fixture("text_message.json"))

    assert fake_wa.sent == [("966512341234", "fallback")]  # the customer still gets a reply
    [(text, _)] = fake_telegram.sent
    assert text.startswith("⚠️ البوت ما قدر يرد على 966512341234 (انتهت المهلة).")
    assert db.get_chat("966512341234").status == "bot"


def test_successful_reply_sends_no_alert(make_state, fake_telegram):
    _process(make_state(), load_fixture("text_message.json"))
    assert fake_telegram.sent == []


# --- Phase 7: no public API documentation pages ---

def test_api_docs_pages_are_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


# --- public privacy page (needed to publish the Meta app) ---

def test_privacy_page_is_public_and_has_deletion_section(client):
    resp = client.get("/privacy")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "سياسة الخصوصية" in resp.text and "Privacy Policy" in resp.text
    assert 'id="delete"' in resp.text  # Meta's "Data Deletion" URL points here
    assert "frame" in resp.text


def test_prompt_handles_data_deletion_requests():
    from app.agent import load_system_prompt

    prompt = load_system_prompt("frame")
    assert "احذف بياناتي" in prompt and "notify_owner" in prompt
