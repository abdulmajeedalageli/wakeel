"""Photos, voice notes and files: webhook parsing, helpers, and the Meta + Telegram clients (no network)."""

import asyncio
import json
import logging

import httpx
import pytest

from app.config import get_settings
from app.media import MAX_MEDIA_BYTES, MediaFile, MediaTooBig, filename_for, size_text
from app.telegram import TelegramClient
from app.webhook import IncomingMessage, extract_events
from app.whatsapp import WhatsAppClient
from tests.conftest import TEST_PHONE_NUMBER_ID, load_fixture

MB = 1024 * 1024
CDN_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123&secret=abc"
TG_TOKEN = "123456:SECRET-TOKEN"


def run(coro):
    return asyncio.run(coro)


# --- webhook parsing ---

def parse(message: dict) -> IncomingMessage:
    payload = load_fixture("image_message.json")
    base = {"from": "966512341234", "id": "wamid.M1", "timestamp": "1749416383"}
    payload["entry"][0]["changes"][0]["value"]["messages"] = [{**base, **message}]
    [event] = extract_events(payload, TEST_PHONE_NUMBER_ID)
    return event


def test_image_with_caption():
    msg = parse({"type": "image", "image": {"id": "M1", "mime_type": "image/jpeg", "caption": " كم سعرها؟ "}})
    assert msg.media.id == "M1" and msg.media.mime_type == "image/jpeg"
    assert msg.media.caption == "كم سعرها؟"
    assert msg.text is None  # text stays text-only
    assert msg.note == "كم سعرها؟"  # what gets stored


def test_voice_note_document_and_video():
    voice = parse({"type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg; codecs=opus", "voice": True}})
    assert voice.media.voice is True and voice.note is None
    doc = parse({"type": "document", "document": {"id": "D1", "mime_type": "application/pdf",
                                                   "filename": "floorplan.pdf", "caption": "the plan"}})
    assert doc.media.filename == "floorplan.pdf" and doc.media.caption == "the plan"
    video = parse({"type": "video", "video": {"id": "V1", "mime_type": "video/mp4"}})
    assert video.media.id == "V1" and video.media.voice is False


def test_location_keeps_the_place_name_but_never_stores_coordinates():
    msg = parse({"type": "location", "location": {"latitude": 24.7113, "longitude": 46.6744,
                                                 "name": "Kingdom Centre", "address": "King Fahd Rd"}})
    assert (msg.location.latitude, msg.location.longitude) == (24.7113, 46.6744)
    assert msg.note == "Kingdom Centre · King Fahd Rd"
    bare = parse({"type": "location", "location": {"latitude": "24.7", "longitude": "46.6"}})
    assert bare.location.latitude == 24.7 and bare.note is None
    broken = parse({"type": "location", "location": {"latitude": "north"}})
    assert broken.location is None


def test_sticker_and_media_without_id_have_no_media():
    assert parse({"type": "sticker", "sticker": {"id": "S1", "mime_type": "image/webp"}}).media is None
    assert parse({"type": "image", "image": {"mime_type": "image/jpeg"}}).media is None


# --- helpers ---

def test_filename_for():
    assert filename_for("application/pdf", "../../etc/plan.pdf") == "plan.pdf"  # no folders
    assert filename_for("application/pdf", "C:\\Users\\x\\plan.pdf") == "plan.pdf"
    assert filename_for("image/jpeg", None, stem="photo") == "photo.jpg"
    assert filename_for("audio/ogg; codecs=opus") == "file.ogg"
    assert filename_for("application/x-unknown-thing") == "file.bin"


def test_size_text():
    assert size_text(34 * MB) == "34 MB"
    assert size_text(int(20.5 * MB)) == "20.5 MB"
    assert size_text(3000) == "2 KB"


# --- WhatsApp client ---

def wa_client(handler) -> WhatsAppClient:
    return WhatsAppClient(get_settings(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def media_handler(seen, content=b"JPEGDATA", info=None, cdn_status=200):
    info = info if info is not None else {"url": CDN_URL, "mime_type": "image/jpeg", "file_size": len(content)}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"id": "M1", **info})
        return httpx.Response(cdn_status, content=content)

    return handler


def test_download_media_asks_meta_then_downloads_with_the_token():
    seen = []
    file = run(wa_client(media_handler(seen)).download_media("M1"))
    assert file == MediaFile(b"JPEGDATA", "image/jpeg", "whatsapp.jpg")
    lookup, download = seen
    assert str(lookup.url) == f"https://graph.facebook.com/v26.0/M1?phone_number_id={TEST_PHONE_NUMBER_ID}"
    assert lookup.headers["Authorization"] == "Bearer test-access-token"
    assert str(download.url) == CDN_URL
    assert download.headers["Authorization"] == "Bearer test-access-token"  # Meta requires it


def test_download_media_keeps_the_customers_file_name():
    seen = []
    info = {"url": CDN_URL, "mime_type": "application/pdf", "file_size": 3}
    file = run(wa_client(media_handler(seen, b"PDF", info)).download_media("M1", "floorplan.pdf"))
    assert file.filename == "floorplan.pdf"


def test_too_big_file_is_refused_before_downloading():
    seen = []
    info = {"url": CDN_URL, "mime_type": "application/pdf", "file_size": 34 * MB}
    with pytest.raises(MediaTooBig) as exc:
        run(wa_client(media_handler(seen, info=info)).download_media("M1"))
    assert exc.value.size == 34 * MB
    assert len(seen) == 1  # never downloaded


def test_too_big_file_is_stopped_while_downloading():
    seen = []
    content = b"x" * (MAX_MEDIA_BYTES + 1)
    info = {"url": CDN_URL, "mime_type": "video/mp4"}  # no size given
    with pytest.raises(MediaTooBig):
        run(wa_client(media_handler(seen, content, info)).download_media("M1"))


def test_expired_link_or_failed_lookup_returns_none():
    assert run(wa_client(media_handler([], cdn_status=404)).download_media("M1")) is None

    def refused(request):
        return httpx.Response(400, json={"error": {"code": 100, "message": "Invalid media id"}})

    assert run(wa_client(refused).download_media("M1")) is None


def test_token_is_never_sent_to_a_non_https_link():
    seen = []
    info = {"url": "http://lookaside.fbsbx.com/x", "mime_type": "image/jpeg", "file_size": 3}
    assert run(wa_client(media_handler(seen, info=info)).download_media("M1")) is None
    assert len(seen) == 1  # only the lookup; the link was never called


def test_download_logs_never_contain_the_link_or_token(caplog):
    def handler(request):
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": CDN_URL, "mime_type": "image/jpeg"})
        raise httpx.ConnectError("boom")

    with caplog.at_level(logging.DEBUG, logger="app"):
        assert run(wa_client(handler).download_media("M1")) is None
    app_logs = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert "download failed" in app_logs
    assert "fbsbx" not in app_logs and "secret=abc" not in app_logs and "test-access-token" not in app_logs


def test_upload_media_sends_a_form_with_the_file():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "UPLOADED1"})

    media_id = run(wa_client(handler).upload_media(MediaFile(b"PDFBYTES", "application/pdf", "plan.pdf")))
    assert media_id == "UPLOADED1"
    request = seen[0]
    assert str(request.url) == f"https://graph.facebook.com/v26.0/{TEST_PHONE_NUMBER_ID}/media"
    assert request.headers["Authorization"] == "Bearer test-access-token"
    body = request.read()
    assert b'name="messaging_product"\r\n\r\nwhatsapp' in body
    assert b'name="type"\r\n\r\napplication/pdf' in body
    assert b'filename="plan.pdf"' in body and b"PDFBYTES" in body


def test_refused_upload_returns_none():
    def handler(request):
        return httpx.Response(400, json={"error": {"code": 131053, "message": "Unsupported file type"}})

    assert run(wa_client(handler).upload_media(MediaFile(b"x", "application/zip", "a.zip"))) is None


def test_send_media_bodies():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.M{len(seen)}"}]})

    client = wa_client(handler)
    assert run(client.send_media("966512341234", "image", "UP1", caption="الصالة")) == "wamid.M1"
    assert run(client.send_media("966512341234", "document", "UP2", caption="x" * 2000,
                                 filename="plan.pdf")) == "wamid.M2"
    assert seen[0]["type"] == "image"
    assert seen[0]["image"] == {"id": "UP1", "caption": "الصالة"}
    assert seen[1]["document"]["filename"] == "plan.pdf"
    assert len(seen[1]["document"]["caption"]) == 1024


def test_send_media_outside_24h_is_not_retried(caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})

    with caplog.at_level(logging.WARNING):
        assert run(wa_client(handler).send_media("966512341234", "image", "UP1")) is None
    assert len(calls) == 1 and "24-hour window" in caplog.text


# --- Telegram client ---

def tg_client(handler) -> TelegramClient:
    return TelegramClient(TG_TOKEN, 4242, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def form_fields(request: httpx.Request) -> bytes:
    return request.read()


def test_send_file_uploads_a_photo_into_a_thread():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 88}})

    file = MediaFile(b"JPEGDATA", "image/jpeg", "whatsapp.jpg")
    message_id = run(tg_client(handler).send_file("photo", file, caption="👤 كم سعرها؟", reply_to=5, thread_id=77))
    assert message_id == 88
    request = seen[0]
    assert str(request.url) == f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
    body = form_fields(request)
    assert b'name="chat_id"\r\n\r\n4242' in body
    assert b'name="message_thread_id"\r\n\r\n77' in body
    assert "👤 كم سعرها؟".encode() in body
    assert b'"message_id": 5' in body  # reply_parameters sent as JSON
    assert b'name="photo"; filename="whatsapp.jpg"' in body and b"JPEGDATA" in body


def test_send_file_falls_back_to_a_plain_file():
    methods = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "sendVoice":
            return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "wrong type"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    file = MediaFile(b"AMR", "audio/amr", "whatsapp.amr")
    assert run(tg_client(handler).send_file("voice", file)) == 9
    assert methods == ["sendVoice", "sendDocument"]


def test_failed_document_is_not_retried_and_caption_is_trimmed():
    seen = []

    def handler(request):
        seen.append(form_fields(request))
        return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "nope"})

    file = MediaFile(b"PDF", "application/pdf", "plan.pdf")
    assert run(tg_client(handler).send_file("document", file, caption="y" * 1500)) is None
    assert len(seen) == 1
    assert b"y" * 1024 in seen[0] and b"y" * 1025 not in seen[0]


def test_send_location():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

    assert run(tg_client(handler).send_location(24.71, 46.67, reply_to=2, thread_id=77)) == 3
    path, body = seen[0]
    assert path.endswith("/sendLocation")
    assert body == {"chat_id": 4242, "latitude": 24.71, "longitude": 46.67, "message_thread_id": 77,
                    "reply_parameters": {"message_id": 2, "allow_sending_without_reply": True}}


def tg_download_handler(seen, file_size=8, content=b"JPEGDATA"):
    def handler(request):
        seen.append(str(request.url))
        if request.url.path.endswith("/getFile"):
            result = {"file_id": "F1", "file_path": "photos/file_7.jpg", "file_size": file_size}
            return httpx.Response(200, json={"ok": True, "result": result})
        return httpx.Response(200, content=content)

    return handler


def test_download_file_gets_the_path_then_the_file():
    seen = []
    file = run(tg_client(tg_download_handler(seen)).download_file("F1", "image/jpeg"))
    assert file == MediaFile(b"JPEGDATA", "image/jpeg", "file_7.jpg")
    assert seen == [f"https://api.telegram.org/bot{TG_TOKEN}/getFile",
                    f"https://api.telegram.org/file/bot{TG_TOKEN}/photos/file_7.jpg"]


def test_download_file_keeps_your_file_name():
    file = run(tg_client(tg_download_handler([])).download_file("F1", "application/pdf", "Floor plan.pdf"))
    assert file.filename == "Floor plan.pdf"


def test_download_file_refuses_big_files_before_downloading():
    seen = []
    with pytest.raises(MediaTooBig):
        run(tg_client(tg_download_handler(seen, file_size=25 * MB)).download_file("F1"))
    assert len(seen) == 1  # only getFile


def test_download_file_logs_never_contain_the_token(caplog):
    def handler(request):
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "docs/a.pdf", "file_size": 3}})
        raise httpx.ConnectError("boom")

    with caplog.at_level(logging.DEBUG, logger="app"):
        assert run(tg_client(handler).download_file("F1")) is None
    app_logs = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert "download failed" in app_logs
    assert TG_TOKEN not in app_logs and "SECRET" not in app_logs
