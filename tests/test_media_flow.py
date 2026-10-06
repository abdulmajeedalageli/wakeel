"""Photos, voice notes, files and locations flowing between customers and your Telegram (Meta + Telegram faked)."""

import asyncio

from app import owner_console, replies
from app.agent import describe_non_text
from app.db import Message
from app.media import MediaFile
from tests.conftest import OWNER_TELEGRAM_ID, TEST_PHONE_NUMBER_ID, load_fixture

CUSTOMER = "966512341234"
OTHER = "966500000000"
MB = 1024 * 1024
PHOTO = MediaFile(b"JPEGDATA", "image/jpeg", "whatsapp.jpg")


def run(coro):
    return asyncio.run(coro)


def customer_sends(state, message: dict, msg_id: str = "wamid.M1") -> None:
    """A webhook with one customer message (e.g. {"type": "image", "image": {...}})."""
    from app.main import process_payload

    payload = load_fixture("image_message.json")
    base = {"from": CUSTOMER, "id": msg_id, "timestamp": "1749416383"}
    payload["entry"][0]["changes"][0]["value"]["messages"] = [{**base, **message}]
    run(process_payload(payload, state, TEST_PHONE_NUMBER_ID))


def photo(caption: str | None = None, media_id: str = "M1") -> dict:
    image = {"id": media_id, "mime_type": "image/jpeg"}
    if caption:
        image["caption"] = caption
    return {"type": "image", "image": image}


def threads_state(make_state, fake_telegram, **settings):
    fake_telegram.topics_enabled = True
    return make_state(**settings)


# --- a customer sends a photo, voice note, file or location (the bot answers the chat) ---

def test_photo_goes_into_the_thread_and_the_customer_is_told(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    fake_wa.media["M1"] = PHOTO
    customer_sends(state, photo("كم سعر هذي الشقة؟"))

    thread = db.get_thread(CUSTOMER)
    [sent] = fake_telegram.files_sent
    assert sent["kind"] == "photo" and sent["file"] == PHOTO
    assert sent["caption"] == "👤 كم سعر هذي الشقة؟" and sent["thread_id"] == thread
    assert db.phone_for_telegram_message(sent["message_id"]) == CUSTOMER  # a Reply to it reaches them

    reply = replies.MEDIA_RECEIVED["image"]
    assert fake_wa.sent == [(CUSTOMER, reply)]
    # The live copy: only the bot's line, as a reply to the forwarded photo.
    assert fake_telegram.in_thread(thread) == [f"🤖 {reply}"]
    assert fake_telegram.sent[-1][1] == sent["message_id"]

    stored = db.get_recent_messages(CUSTOMER)
    assert [(m.direction, m.type, m.text) for m in stored] == [
        ("in", "image", "كم سعر هذي الشقة؟"), ("out", "text", reply)]


def test_media_reply_is_always_arabic(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    fake_wa.media.update({"M1": PHOTO, "M2": PHOTO})
    customer_sends(state, photo())
    assert fake_wa.sent[-1][1] == replies.media_received("image")
    assert "وصلتني الصورة" in fake_wa.sent[-1][1]

    db.save_message(CUSTOMER, "in", "text", "Hi, looking for a villa")  # an English customer
    customer_sends(state, photo(media_id="M2"), msg_id="wamid.M2")
    assert fake_wa.sent[-1][1] == replies.media_received("image")  # still Arabic


def test_voice_note_video_and_document_use_the_right_telegram_kind(make_state, fake_telegram, fake_wa):
    state = threads_state(make_state, fake_telegram)
    fake_wa.media.update({
        "A1": MediaFile(b"OGG", "audio/ogg; codecs=opus", "whatsapp.ogg"),
        "A2": MediaFile(b"MP3", "audio/mpeg", "whatsapp.mp3"),
        "V1": MediaFile(b"MP4", "video/mp4", "whatsapp.mp4"),
        "D1": MediaFile(b"PDF", "application/pdf", "floorplan.pdf"),
    })
    customer_sends(state, {"type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg; codecs=opus",
                                                      "voice": True}}, "wamid.1")
    customer_sends(state, {"type": "audio", "audio": {"id": "A2", "mime_type": "audio/mpeg"}}, "wamid.2")
    customer_sends(state, {"type": "video", "video": {"id": "V1", "mime_type": "video/mp4"}}, "wamid.3")
    customer_sends(state, {"type": "document", "document": {"id": "D1", "mime_type": "application/pdf",
                                                            "filename": "floorplan.pdf"}}, "wamid.4")
    assert [f["kind"] for f in fake_telegram.files_sent] == ["voice", "audio", "video", "document"]
    assert fake_wa.downloads == ["A1", "A2", "V1", "D1"]
    assert fake_wa.sent[0][1] == replies.media_received("audio")


def test_media_reaches_you_even_with_the_live_copy_off(make_state, fake_telegram, fake_wa):
    state = threads_state(make_state, fake_telegram, telegram_live_mirror=False)
    fake_wa.media["M1"] = PHOTO
    customer_sends(state, photo())
    assert len(fake_telegram.files_sent) == 1
    assert fake_telegram.sent == []  # no 🤖 copy
    assert fake_wa.sent == [(CUSTOMER, replies.media_received("image"))]


def test_too_big_file_gives_you_a_note_and_the_customer_is_asked_to_type(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    fake_wa.media["D1"] = 34 * MB
    customer_sends(state, {"type": "document", "document": {"id": "D1", "mime_type": "application/pdf",
                                                            "caption": "العقد"}})
    note, bot_line = fake_telegram.in_thread(db.get_thread(CUSTOMER))
    assert note == "👤 📎 ما قدرت أحوّل ملف: حجمه كبير (34 ميجا؛ الحد الأقصى 20 ميجا). التعليق: العقد"
    assert fake_telegram.files_sent == []
    assert fake_wa.sent == [(CUSTOMER, replies.NON_TEXT_REPLY)]  # never "the team will look"
    assert bot_line == f"🤖 {replies.NON_TEXT_REPLY}"


def test_telegram_refusing_the_file_means_the_customer_is_asked_to_type(make_state, fake_telegram, fake_wa):
    state = threads_state(make_state, fake_telegram)
    fake_wa.media["M1"] = PHOTO
    fake_telegram.fail_files = True
    customer_sends(state, photo())
    assert fake_wa.sent == [(CUSTOMER, replies.NON_TEXT_REPLY)]


def test_without_telegram_nothing_is_downloaded(make_state, fake_wa):
    state = make_state()
    state.telegram = None
    fake_wa.media["M1"] = PHOTO
    customer_sends(state, photo())
    assert fake_wa.downloads == []
    assert fake_wa.sent == [(CUSTOMER, replies.NON_TEXT_REPLY)]


def test_without_threads_it_arrives_in_the_main_chat_with_a_header(make_state, fake_telegram, fake_wa, db):
    state = make_state()  # topics off
    fake_wa.media["M1"] = PHOTO
    customer_sends(state, photo("this one"))
    [sent] = fake_telegram.files_sent
    assert sent["thread_id"] is None
    assert sent["caption"] == "📩 966512341234 (Test Customer) · لك: البوت ما يقدر يفتح الصور والملفات\nthis one"
    assert db.phone_for_telegram_message(sent["message_id"]) == CUSTOMER
    assert fake_telegram.sent == []  # no live copy without threads
    assert fake_wa.sent == [(CUSTOMER, replies.media_received("image"))]


def test_stale_thread_falls_back_to_the_main_chat(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    db.set_thread(CUSTOMER, 999)
    fake_telegram.broken_threads.add(999)  # deleted by hand
    fake_wa.media["M1"] = PHOTO
    customer_sends(state, photo())
    assert fake_telegram.files_sent[0]["thread_id"] is None
    # The dead thread is forgotten; the bot's 🤖 line right after already opens a new one.
    assert db.get_thread(CUSTOMER) not in (None, 999)


def test_location_becomes_a_map_pin_and_coordinates_are_not_stored(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    customer_sends(state, {"type": "location", "location": {
        "latitude": 24.7113, "longitude": 46.6744, "name": "Kingdom Centre", "address": "King Fahd Rd"}})
    thread = db.get_thread(CUSTOMER)
    label = fake_telegram.in_thread(thread)[0]
    assert label == "👤 📍 Kingdom Centre · King Fahd Rd"
    [pin] = fake_telegram.locations
    assert (pin["latitude"], pin["longitude"], pin["thread_id"]) == (24.7113, 46.6744, thread)
    assert pin["reply_to"] == fake_telegram.last_message_id - 2  # the label (then the pin, then 🤖)
    assert fake_wa.sent == [(CUSTOMER, replies.media_received("location"))]

    stored = db.get_recent_messages(CUSTOMER)
    assert stored[0].text == "Kingdom Centre · King Fahd Rd"
    assert not any("24.71" in (m.text or "") or "46.67" in (m.text or "") for m in stored)


def test_location_without_a_name(make_state, fake_telegram, db):
    state = threads_state(make_state, fake_telegram)
    customer_sends(state, {"type": "location", "location": {"latitude": 24.7, "longitude": 46.6}})
    assert fake_telegram.in_thread(db.get_thread(CUSTOMER))[0] == "👤 📍 موقع"
    assert db.get_recent_messages(CUSTOMER)[0].text is None


def test_sticker_is_unchanged(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    customer_sends(state, {"type": "sticker", "sticker": {"id": "S1", "mime_type": "image/webp"}})
    assert fake_wa.downloads == [] and fake_telegram.files_sent == []
    assert fake_wa.sent == [(CUSTOMER, replies.NON_TEXT_REPLY)]
    assert fake_telegram.in_thread(db.get_thread(CUSTOMER))[0].startswith("👤 [ملصق]\n🤖 ")


# --- chats you handle: forwarded, no reply to the customer ---

def test_photo_in_a_chat_you_took_over(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    db.get_or_create_chat(CUSTOMER)
    db.take_over(CUSTOMER)
    fake_wa.media["M1"] = PHOTO
    customer_sends(state, photo("شف هذي"))
    assert fake_telegram.files_sent[0]["caption"] == "👤 شف هذي"
    assert fake_wa.sent == [] and fake_wa.read == []  # silent, not marked as read


def test_photo_while_paused_without_threads(make_state, fake_telegram, fake_wa, db):
    state = make_state()
    db.set_paused(True)
    fake_wa.media["M1"] = PHOTO
    customer_sends(state, photo())
    assert fake_telegram.files_sent[0]["caption"] == "📩 966512341234 (Test Customer) · البوت موقوف"
    assert fake_wa.sent == []


def test_photo_while_paused_with_threads_notes_why(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    db.set_paused(True)
    fake_wa.media["M1"] = PHOTO
    customer_sends(state, photo("hi"))
    assert fake_telegram.files_sent[0]["caption"] == "👤 hi\n(البوت موقوف)"


# --- you send a photo or file to a customer ---

def owner_file_message(thread_id=None, reply_to=None, sender=OWNER_TELEGRAM_ID, caption=None, **content):
    message = {"message_id": 7, "from": {"id": sender}, "chat": {"id": sender}, **content}
    if caption is not None:
        message["caption"] = caption
    if thread_id is not None:
        message.update({"message_thread_id": thread_id, "is_topic_message": True})
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": message}


PHOTO_SIZES = [
    {"file_id": "small", "width": 90, "height": 67, "file_size": 1_200},
    {"file_id": "big", "width": 1280, "height": 960, "file_size": 4 * MB},
    {"file_id": "huge", "width": 2560, "height": 1920, "file_size": 6 * MB},  # over WhatsApp's 5 MB
]


def customer_thread(state, db, phone=CUSTOMER) -> int:
    db.get_or_create_chat(phone)
    return run(owner_console.customer_thread(state, phone))


def test_your_photo_in_a_thread_reaches_that_customer(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    customer_thread(state, db, OTHER)
    thread = customer_thread(state, db)
    fake_telegram.downloadable["big"] = b"OWNERJPEG"
    run(owner_console.handle_update(owner_file_message(thread, photo=PHOTO_SIZES, caption="الصالة"), state))

    assert fake_telegram.downloads == ["big"]  # the biggest size WhatsApp accepts as a photo
    [upload] = fake_wa.uploads
    assert upload.content == b"OWNERJPEG" and upload.mime_type == "image/jpeg"
    assert fake_wa.sent_media == [(CUSTOMER, "image", "media.UP1", "الصالة", upload.filename)]
    assert fake_wa.sent == []

    [stored] = db.get_recent_messages(CUSTOMER)
    assert (stored.direction, stored.type, stored.text) == ("owner", "image", "الصالة")
    assert db.get_chat(CUSTOMER).status == "human"
    assert fake_telegram.topics[thread].startswith("🙋")
    assert fake_telegram.in_thread(thread)[-1].startswith("✅ انرسلت للعميل 966512341234.")
    assert db.get_recent_messages(OTHER) == []


def test_your_pdf_is_sent_as_a_document_with_its_name(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    thread = customer_thread(state, db)
    fake_telegram.downloadable["DOC"] = b"%PDF"
    document = {"file_id": "DOC", "file_name": "مخطط الفيلا.pdf", "mime_type": "application/pdf", "file_size": 4}
    run(owner_console.handle_update(owner_file_message(thread, document=document), state))
    assert fake_wa.uploads[0].filename == "مخطط الفيلا.pdf"
    assert fake_wa.sent_media == [(CUSTOMER, "document", "media.UP1", "", "مخطط الفيلا.pdf")]
    assert db.get_recent_messages(CUSTOMER)[0].type == "document"


def test_image_sent_as_a_file_becomes_a_photo(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    thread = customer_thread(state, db)
    fake_telegram.downloadable["IMG"] = b"PNG"
    document = {"file_id": "IMG", "file_name": "view.png", "mime_type": "image/png", "file_size": 2 * MB}
    run(owner_console.handle_update(owner_file_message(thread, document=document), state))
    assert fake_wa.sent_media[0][1] == "image"


def test_too_big_file_is_explained_and_nothing_is_sent(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    thread = customer_thread(state, db)
    fake_telegram.downloadable["DOC"] = 25 * MB
    document = {"file_id": "DOC", "file_name": "big.pdf", "mime_type": "application/pdf"}
    run(owner_console.handle_update(owner_file_message(thread, document=document), state))
    assert fake_telegram.in_thread(thread)[-1] == "❌ الملف كبير (25 ميجا؛ الحد الأقصى 20 ميجا)، وما انرسل شي."
    assert fake_wa.uploads == [] and fake_wa.sent_media == []
    assert db.get_chat(CUSTOMER).status == "bot"  # not taken over


def test_file_whatsapp_refuses_is_explained(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    thread = customer_thread(state, db)
    fake_telegram.downloadable["ZIP"] = b"PK"
    fake_wa.fail_uploads = True
    document = {"file_id": "ZIP", "file_name": "photos.zip", "mime_type": "application/zip", "file_size": 2}
    run(owner_console.handle_update(owner_file_message(thread, document=document), state))
    assert fake_telegram.in_thread(thread)[-1].startswith("❌ واتساب ما قبل هذا الملف")
    assert fake_wa.sent_media == [] and db.get_recent_messages(CUSTOMER) == []
    assert db.get_chat(CUSTOMER).status == "bot"


def test_photo_outside_the_24h_window_is_explained_and_not_stored(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    thread = customer_thread(state, db)
    fake_telegram.downloadable["big"] = b"JPEG"
    fake_wa.fail_sends = True
    run(owner_console.handle_update(owner_file_message(thread, photo=PHOTO_SIZES), state))
    assert fake_telegram.in_thread(thread)[-1].startswith("❌ ما وصلت الرسالة")
    assert db.get_recent_messages(CUSTOMER) == []


def test_photo_as_a_reply_without_threads(make_state, fake_telegram, fake_wa, db):
    state = make_state()
    db.link_telegram_message(500, CUSTOMER)
    fake_telegram.downloadable["big"] = b"JPEG"
    run(owner_console.handle_update(owner_file_message(reply_to=500, photo=PHOTO_SIZES, caption="/bot"), state))
    # A caption is never a command: it goes to the customer with the photo.
    assert fake_wa.sent_media == [(CUSTOMER, "image", "media.UP1", "/bot", fake_wa.uploads[0].filename)]
    assert db.get_chat(CUSTOMER).status == "human"


def test_photo_in_an_unlinked_thread_or_without_reply_gives_a_hint(make_state, fake_telegram, fake_wa):
    state = threads_state(make_state, fake_telegram)
    run(owner_console.handle_update(owner_file_message(thread_id=12345, photo=PHOTO_SIZES), state))
    assert "مو مربوط بعميل" in fake_telegram.sent[-1][0]
    run(owner_console.handle_update(owner_file_message(photo=PHOTO_SIZES), state))
    assert "للرد على عميل" in fake_telegram.sent[-1][0]
    assert fake_telegram.downloads == [] and fake_wa.sent_media == []


def test_photo_from_anyone_else_is_ignored(make_state, fake_telegram, fake_wa, db):
    state = threads_state(make_state, fake_telegram)
    thread = customer_thread(state, db)
    fake_telegram.downloadable["big"] = b"JPEG"
    run(owner_console.handle_update(owner_file_message(thread, sender=999, photo=PHOTO_SIZES), state))
    assert fake_telegram.downloads == [] and fake_wa.sent_media == []
    assert fake_telegram.sent == []


# --- what the AI sees ---

def message(direction, type, text=None) -> Message:
    return Message(1, CUSTOMER, direction, type, text, None, "2026-09-27T10:00:00+00:00")


def test_the_ai_is_told_about_media_but_never_sees_it():
    assert describe_non_text(message("in", "image", "كم سعرها؟")) == (
        '[The customer sent a photo, which the assistant cannot see or open] Caption: "كم سعرها؟"')
    assert describe_non_text(message("in", "audio")) == (
        "[The customer sent a voice note, which the assistant cannot see or open]")
    assert describe_non_text(message("in", "location", "Kingdom Centre")) == (
        "[The customer shared a location: Kingdom Centre]")
    assert describe_non_text(message("in", "sticker")) == (
        "[The customer sent a sticker message, which the assistant cannot see or open]")
    # Your photo is described as sent by the team, not by the customer.
    assert describe_non_text(message("owner", "image", "الصالة")) == (
        '[The team sent the customer a photo] Caption: "الصالة"')

