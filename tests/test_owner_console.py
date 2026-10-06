"""Your Telegram console: commands, relaying replies, forwards and auto-return (all texts in Arabic)."""

import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from app import owner_console
from app.webhook import IncomingMessage
from tests.conftest import OWNER_TELEGRAM_ID

CUSTOMER = "966512341234"


def update(text: str, sender: int = OWNER_TELEGRAM_ID, reply_to: int | None = None, message_id: int = 1) -> dict:
    message = {"message_id": message_id, "from": {"id": sender}, "chat": {"id": sender}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": message}


def run(state, text: str, **kwargs) -> None:
    asyncio.run(owner_console.handle_update(update(text, **kwargs), state))


def last_text(fake_telegram) -> str:
    return fake_telegram.sent[-1][0]


# --- security ---

def test_messages_from_anyone_else_are_ignored(make_state, fake_telegram, db, caplog):
    state = make_state()
    run(state, f"/human {CUSTOMER}", sender=999)
    run(state, "/pause", sender=999)
    assert fake_telegram.sent == []
    assert db.get_chat(CUSTOMER) is None
    assert not db.is_paused()
    assert "Ignored Telegram message from user id 999" in caplog.text


def test_group_chat_is_ignored_even_from_owner(make_state, fake_telegram):
    state = make_state()
    upd = update("/pause")
    upd["message"]["chat"]["id"] = -100123  # a group that isn't the console chat
    asyncio.run(owner_console.handle_update(upd, state))
    assert fake_telegram.sent == []


# --- commands ---

def test_human_takes_over_and_sends_context(make_state, fake_telegram, db):
    db.save_message(CUSTOMER, "in", "text", "أبي شقة في النرجس")
    db.save_message(CUSTOMER, "out", "text", "عندنا P-1030")
    state = make_state()

    run(state, "/human 0512341234")  # local format works

    chat = db.get_chat(CUSTOMER)
    assert chat.status == "human" and chat.last_owner_message_at
    text = last_text(fake_telegram)
    assert "استلمت محادثة 966512341234" in text
    assert "👤 العميل: أبي شقة في النرجس" in text
    assert "🤖 البوت: عندنا P-1030" in text
    # You can Reply to this message to answer the customer:
    assert db.phone_for_telegram_message(fake_telegram.last_message_id) == CUSTOMER


def test_bot_hands_back(make_state, fake_telegram, db):
    db.take_over(CUSTOMER)
    run(make_state(), f"/bot +{CUSTOMER}")
    assert db.get_chat(CUSTOMER).status == "bot"
    assert last_text(fake_telegram) == "🤖 البوت رجع يرد على 966512341234."


def test_human_without_number_explains(make_state, fake_telegram, db):
    run(make_state(), "/human")
    assert "اكتب رقم العميل" in last_text(fake_telegram)
    run(make_state(), "/human abc")
    assert "اكتب رقم العميل" in last_text(fake_telegram)


def test_pause_and_resume_persist(make_state, fake_telegram, db):
    run(make_state(), "/pause")
    assert db.is_paused()
    assert "تم الإيقاف" in last_text(fake_telegram)
    run(make_state(), "/resume")
    assert not db.is_paused()
    assert "تم التشغيل" in last_text(fake_telegram)


def test_status(make_state, fake_telegram, db):
    db.take_over(CUSTOMER)
    db.set_paused(True)
    run(make_state(human_timeout_hours=24), "/status")
    text = last_text(fake_telegram)
    assert "البوت موقوف" in text
    assert "الوضع: دائماً" in text
    assert "966512341234: ترجع للبوت بعد 23س 59د" in text or "ترجع للبوت بعد 24س 0د" in text


def test_status_schedule_mode(make_state, fake_telegram):
    run(make_state(availability_mode="schedule", work_hours="00:00-24:00"), "/status")
    assert "الحين: أنت." in last_text(fake_telegram)


def test_status_with_no_takeovers(make_state, fake_telegram):
    run(make_state(), "/status")
    assert "ما فيه محادثات مستلمة" in last_text(fake_telegram)


def test_help_and_unknown_command(make_state, fake_telegram):
    run(make_state(), "/start")
    assert "/human 9665XXXXXXXX" in last_text(fake_telegram)
    run(make_state(), "/dance")
    assert "أمر غير معروف: /dance" in last_text(fake_telegram)


def test_command_with_bot_name_suffix(make_state, db):
    run(make_state(), "/pause@frame_bot")
    assert db.is_paused()


def test_plain_text_without_reply_gives_a_hint(make_state, fake_telegram, fake_wa):
    run(make_state(), "hello?")
    assert fake_wa.sent == []
    assert "للرد على عميل" in last_text(fake_telegram)


def test_service_messages_are_ignored(make_state, fake_telegram, fake_wa):
    upd = update("", reply_to=None)
    upd["message"].pop("text")
    upd["message"]["forum_topic_edited"] = {"name": "renamed"}  # e.g. you renamed a topic
    asyncio.run(owner_console.handle_update(upd, make_state()))
    assert fake_telegram.sent == [] and fake_wa.sent == []


# --- relaying your replies ---

def test_reply_is_sent_to_the_right_customer_as_owner(make_state, fake_telegram, fake_wa, db):
    db.link_telegram_message(500, CUSTOMER)
    db.link_telegram_message(501, "966500000000")

    run(make_state(), "هلا، أنا فهد من frame. متى يناسبك نتصل؟", reply_to=500, message_id=9)

    assert fake_wa.sent == [(CUSTOMER, "هلا، أنا فهد من frame. متى يناسبك نتصل؟")]
    [stored] = db.get_recent_messages(CUSTOMER)
    assert (stored.direction, stored.wa_message_id) == ("owner", "wamid.FAKE_OUT1")
    assert db.get_chat(CUSTOMER).status == "human"  # replying means you're handling it
    text, reply_to = fake_telegram.sent[-1]
    assert text.startswith("✅ انرسلت للعميل 966512341234.")
    assert "البوت ساكت الحين" in text  # it was a bot chat before
    assert reply_to == 9


def test_reply_restarts_the_timeout_clock(make_state, db):
    db.take_over(CUSTOMER)
    old = (datetime.now(UTC) - timedelta(hours=20)).isoformat(timespec="seconds")
    with sqlite3.connect(db.path) as conn:
        conn.execute("UPDATE chats SET last_owner_message_at = ?", (old,))
    db.link_telegram_message(500, CUSTOMER)

    run(make_state(), "Any update?", reply_to=500)

    assert db.get_chat(CUSTOMER).last_owner_message_at > old


def test_reply_to_unknown_message_gives_a_hint(make_state, fake_telegram, fake_wa):
    run(make_state(), "hello", reply_to=12345)
    assert fake_wa.sent == []
    assert "ما عرفت هذي الرسالة لأي عميل" in last_text(fake_telegram)


def test_failed_send_is_explained_and_not_stored(make_state, fake_telegram, fake_wa, db):
    fake_wa.fail_sends = True
    db.link_telegram_message(500, CUSTOMER)
    run(make_state(), "hello", reply_to=500)
    assert "ما وصلت الرسالة" in last_text(fake_telegram)
    assert "24 ساعة" in last_text(fake_telegram)
    assert db.get_recent_messages(CUSTOMER) == []
    assert db.get_chat(CUSTOMER) is None or db.get_chat(CUSTOMER).status == "bot"


def test_unsupported_media_reply_is_refused(make_state, fake_telegram, fake_wa, db):
    db.link_telegram_message(500, CUSTOMER)
    upd = update("", reply_to=500)
    upd["message"].pop("text")
    upd["message"]["voice"] = {"file_id": "x", "duration": 3}  # voice notes aren't relayed (yet)
    asyncio.run(owner_console.handle_update(upd, make_state()))
    assert fake_wa.sent == [] and fake_wa.sent_media == []
    assert last_text(fake_telegram) == owner_console.ONLY_TEXT_AND_FILES


# --- forwards and auto-return ---

def customer_message(text="hi", type="text") -> IncomingMessage:
    return IncomingMessage("wamid.X", CUSTOMER, type, text if type == "text" else None, None, "سارة")


def test_forward_to_owner(make_state, fake_telegram, db):
    asyncio.run(owner_console.forward_to_owner(make_state(), customer_message("متى المعاينة؟"), "human"))
    text = last_text(fake_telegram)
    assert text == "📩 966512341234 (سارة) · استلمت هذي المحادثة\nمتى المعاينة؟"
    assert db.phone_for_telegram_message(fake_telegram.last_message_id) == CUSTOMER


def test_forward_describes_what_cant_be_forwarded(make_state, fake_telegram):
    asyncio.run(owner_console.forward_to_owner(make_state(), customer_message(type="sticker"), "paused"))
    assert "[ملصق: ما يمكن تحويله]" in last_text(fake_telegram)
    assert "البوت موقوف" in last_text(fake_telegram)


def test_forward_without_telegram_only_logs(make_state, caplog):
    import logging

    state = make_state()
    state.telegram = None
    with caplog.at_level(logging.INFO):
        asyncio.run(owner_console.forward_to_owner(state, customer_message(), "human"))
    assert "Telegram not configured" in caplog.text
    assert "966512341234" not in caplog.text  # phone masked in logs


def test_return_expired_chats(make_state, fake_telegram, db):
    db.take_over(CUSTOMER)
    db.take_over("966500000000")
    old = (datetime.now(UTC) - timedelta(hours=30)).isoformat(timespec="seconds")
    with sqlite3.connect(db.path) as conn:
        conn.execute("UPDATE chats SET last_owner_message_at = ? WHERE phone = ?", (old, CUSTOMER))

    returned = asyncio.run(owner_console.return_expired_chats(make_state(human_timeout_hours=24)))

    assert returned == 1
    assert db.get_chat(CUSTOMER).status == "bot"
    assert db.get_chat("966500000000").status == "human"
    assert last_text(fake_telegram) == "↩️ 966512341234 رجع للبوت (ما وصلته رسالة منك خلال 24 ساعة)."


# --- Phase 6: alerts ---

def notify(state, reason="viewing_request", summary="يبي يعاين P-1007 السبت", phone=CUSTOMER):
    return asyncio.run(state.alerts.notify(phone, reason, summary))


def test_alert_format_and_reply_link(make_state, fake_telegram, db):
    db.save_message(CUSTOMER, "in", "text", "ممكن أشوف الشقة السبت؟")
    db.save_lead(CUSTOMER, {"name": "سارة", "budget": "900 ألف", "deal": "sale", "districts": "النرجس"})
    state = make_state()

    assert notify(state) == "sent"

    text = last_text(fake_telegram)
    assert text.splitlines() == [
        "🔔 طلب معاينة · 966512341234 (سارة)",
        "يبي يعاين P-1007 السبت",
        "بيانات العميل: الميزانية: 900 ألف · الطلب: شراء · الأحياء: النرجس",
        "آخر رسالة: «ممكن أشوف الشقة السبت؟»",
        "↩️ للرد عليه: اكتب في موضوعه أو سوّ رد على هذي الرسالة. لاستلام المحادثة: /human 966512341234",
    ]
    # Replying to the alert in Telegram reaches this customer.
    assert db.phone_for_telegram_message(fake_telegram.last_message_id) == CUSTOMER


def test_alert_without_lead_or_messages(make_state, fake_telegram):
    notify(make_state(), reason="wants_human", summary="يبي يكلم موظف")
    assert last_text(fake_telegram).splitlines() == [
        "🔔 يبي يكلم موظف · 966512341234",
        "يبي يكلم موظف",
        "↩️ للرد عليه: اكتب في موضوعه أو سوّ رد على هذي الرسالة. لاستلام المحادثة: /human 966512341234",
    ]


def test_alert_does_not_take_over_the_chat(make_state, db):
    db.get_or_create_chat(CUSTOMER)
    notify(make_state())
    assert db.get_chat(CUSTOMER).status == "bot"  # by design: the bot keeps helping


def test_same_alert_is_not_repeated_within_an_hour(make_state, fake_telegram):
    state = make_state()
    assert notify(state) == "sent"
    assert notify(state) == "duplicate"
    assert notify(state, reason="negotiation") == "sent"  # a different reason is a new alert
    assert notify(state, phone="966500000000") == "sent"  # so is another customer
    assert len(fake_telegram.sent) == 3


def test_alert_is_sent_again_after_the_cooldown(make_state, fake_telegram):
    from app.owner_console import ALERT_COOLDOWN

    state = make_state()
    notify(state)
    key = (CUSTOMER, "viewing_request")
    state.alerts._last_sent[key] = datetime.now(UTC) - ALERT_COOLDOWN
    assert notify(state) == "sent"


def test_failed_telegram_send_is_retried_next_time(make_state, fake_telegram):
    state = make_state()

    async def broken_send(text, reply_to=None, thread_id=None):
        return None

    real_send = fake_telegram.send_message
    fake_telegram.send_message = broken_send
    assert notify(state) == "failed"
    fake_telegram.send_message = real_send
    assert notify(state) == "sent"  # not blocked by the anti-spam


def test_alert_without_telegram_is_logged(make_state, caplog):
    state = make_state()
    state.telegram = None
    assert notify(state) == "not_configured"
    assert "Telegram not configured" in caplog.text
    assert CUSTOMER not in caplog.text  # masked


def test_bot_error_alert(make_state, fake_telegram, db):
    state = make_state()
    assert asyncio.run(state.alerts.bot_error(CUSTOMER, "timed out")) == "sent"
    assert asyncio.run(state.alerts.bot_error(CUSTOMER, "timed out")) == "duplicate"
    text = last_text(fake_telegram)
    assert text.startswith("⚠️ البوت ما قدر يرد على 966512341234 (انتهت المهلة).")
    assert db.phone_for_telegram_message(fake_telegram.last_message_id) == CUSTOMER


def test_gemini_limit_is_explained_in_the_alert(make_state, fake_telegram):
    asyncio.run(make_state().alerts.bot_error(CUSTOMER, "Gemini error 429"))
    assert "انتهى الحد المسموح في Gemini" in last_text(fake_telegram)
    assert "فعّل الفوترة" in last_text(fake_telegram)


# --- Phase 6: /leads and /lead ---

def test_leads_empty(make_state, fake_telegram):
    run(make_state(), "/leads")
    assert last_text(fake_telegram) == "ما فيه عملاء محتملين محفوظين للحين."


def test_leads_list(make_state, fake_telegram, db):
    db.save_lead(CUSTOMER, {"name": "سارة", "budget": "900 ألف"})
    db.save_lead("966500000000", {"deal": "rent"})
    run(make_state(), "/leads")
    text = last_text(fake_telegram)
    assert "آخر العملاء المحتملين (2)" in text
    assert "• 966512341234 سارة: الميزانية: 900 ألف (قبل شوي)" in text
    assert "• 966500000000: الطلب: إيجار (قبل شوي)" in text


def test_lead_details(make_state, fake_telegram, db):
    db.save_lead(CUSTOMER, {"name": "سارة", "type": "villa", "timeline": "خلال 3 شهور"})
    run(make_state(), "/lead 0512341234")
    text = last_text(fake_telegram)
    assert text.startswith("📋 بيانات العميل 966512341234 (آخر تحديث قبل شوي)")
    assert "الاسم: سارة" in text and "نوع العقار: فيلا" in text and "الموعد المتوقع: خلال 3 شهور" in text
    assert db.phone_for_telegram_message(fake_telegram.last_message_id) == CUSTOMER


def test_lead_missing_or_bad_number(make_state, fake_telegram):
    run(make_state(), f"/lead {CUSTOMER}")
    assert last_text(fake_telegram) == "ما فيه بيانات محفوظة للعميل 966512341234."
    run(make_state(), "/lead")
    assert "اكتب رقم العميل" in last_text(fake_telegram)


# --- Phase 6: /forget ---

def _store_customer(db, phone=CUSTOMER):
    db.get_or_create_chat(phone)
    db.save_message(phone, "in", "text", "hello")
    db.save_message(phone, "out", "text", "hi")
    db.save_lead(phone, {"name": "سارة"})


def test_forget_asks_for_confirmation_first(make_state, fake_telegram, db):
    _store_customer(db)
    run(make_state(), f"/forget {CUSTOMER}")
    text = last_text(fake_telegram)
    assert "يحذف نهائياً" in text and "2 رسالة" in text
    assert f"/forget {CUSTOMER} تأكيد" in text
    assert db.customer_data_counts(CUSTOMER)["messages"] == 2  # nothing deleted yet


def test_forget_confirm_deletes_only_that_customer(make_state, fake_telegram, db):
    _store_customer(db)
    _store_customer(db, "966500000000")
    run(make_state(), "/forget 0512341234 تأكيد")

    assert not any(db.customer_data_counts(CUSTOMER).values())
    assert db.customer_data_counts("966500000000")["messages"] == 2
    text = last_text(fake_telegram)
    assert "انحذف كل شي محفوظ عن 966512341234 (2 رسالة)" in text
    assert "احذف رسائله هنا يدوياً" in text


def test_forget_still_accepts_the_english_confirm_word(make_state, db):
    _store_customer(db)
    run(make_state(), f"/forget {CUSTOMER} confirm")
    assert not any(db.customer_data_counts(CUSTOMER).values())


def test_forget_nothing_stored_or_bad_number(make_state, fake_telegram):
    run(make_state(), f"/forget {CUSTOMER} تأكيد")
    assert last_text(fake_telegram) == "ما فيه شي محفوظ للعميل 966512341234."
    run(make_state(), "/forget تأكيد")
    assert "اكتب رقم العميل" in last_text(fake_telegram)


def test_help_lists_new_commands(make_state, fake_telegram):
    run(make_state(), "/help")
    text = last_text(fake_telegram)
    assert "/leads" in text and "/lead 9665" in text and "/forget 9665" in text and "/chatid" in text


# --- no English in anything the console sends you ---

# Latin words that may appear: names of things, file types, commands' placeholder.
ALLOWED_LATIN = {"frame", "PDF", "Word", "Excel", "PowerPoint", "TXT", "JPG", "PNG", "Gemini", "xlsx",
                 "BotFather", "XXXXXXXX", "TELEGRAM_GROUP_ID", "P"}


def english_words(text: str) -> list[str]:
    text = re.sub(r"/[a-z_]+", "", text)  # commands like /status
    return [w for w in re.findall(r"[A-Za-z][A-Za-z_]*", text) if w not in ALLOWED_LATIN]


def test_everything_the_console_sends_is_arabic(make_state, fake_telegram, fake_wa, db):
    db.save_message(CUSTOMER, "in", "text", "أبي شقة")
    db.save_message(CUSTOMER, "in", "image", None)
    db.save_lead(CUSTOMER, {"name": "سارة", "budget": "900 ألف", "deal": "sale", "type": "apartment"})
    state = make_state()
    for command in ("/help", "/status", "/leads", f"/lead {CUSTOMER}", f"/human {CUSTOMER}", f"/bot {CUSTOMER}",
                    "/pause", "/resume", f"/forget {CUSTOMER}", "/dance", "/human", "hello?", "/chatid"):
        run(state, command)
    run(state, "hello", reply_to=12345)
    db.link_telegram_message(500, CUSTOMER)
    run(state, "رد", reply_to=500)
    notify(state)
    for reason in ("timed out", "GEMINI_API_KEY is not set", "Gemini error 429", "Gemini error 403",
                   "Gemini error 503", None):
        state.alerts._last_sent.clear()
        asyncio.run(state.alerts.bot_error(CUSTOMER, reason))
    asyncio.run(owner_console.forward_to_owner(state, customer_message(type="sticker"), "work_hours"))
    run(make_state(availability_mode="schedule"), "/status")
    run(state, f"/forget {CUSTOMER} تأكيد")

    assert len(fake_telegram.sent) > 20
    for text, _ in fake_telegram.sent:
        assert english_words(text) == [], text
