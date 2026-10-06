"""Telegram client, using httpx's built-in fake transport (no network)."""

import asyncio
import json
import logging

import httpx

from app.telegram import TelegramClient, split_text

TOKEN = "123456:SECRET-TOKEN"
OWNER = 4242


def make_client(handler) -> TelegramClient:
    return TelegramClient(TOKEN, OWNER, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def ok(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def test_send_message_payload():
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        return ok({"message_id": 77})

    message_id = asyncio.run(make_client(handler).send_message("hello owner", reply_to=55))

    assert message_id == 77
    url, body = seen[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == OWNER
    assert body["text"] == "hello owner"
    assert body["reply_parameters"] == {"message_id": 55, "allow_sending_without_reply": True}


def test_send_message_without_reply():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return ok({"message_id": 1})

    asyncio.run(make_client(handler).send_message("hi"))
    assert "reply_parameters" not in seen[0]


def test_long_text_is_split_and_last_id_returned():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return ok({"message_id": len(seen)})

    text = "\n".join(f"line {i} " + "x" * 90 for i in range(100))  # ~10,000 chars
    message_id = asyncio.run(make_client(handler).send_message(text, reply_to=9))

    assert len(seen) == 3
    assert all(len(body["text"]) <= 4096 for body in seen)
    assert "reply_parameters" in seen[0] and "reply_parameters" not in seen[1]
    assert message_id == 3


def test_split_text_prefers_line_breaks():
    pieces = split_text("aaaa\nbbbb\ncccc", limit=10)
    assert pieces == ["aaaa\nbbbb", "cccc"]


def test_get_updates():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return ok([{"update_id": 10, "message": {"text": "/status"}}])

    updates = asyncio.run(make_client(handler).get_updates(offset=10))
    assert updates[0]["update_id"] == 10
    assert seen[0]["offset"] == 10
    assert seen[0]["allowed_updates"] == ["message"]


def test_errors_return_none_and_never_log_the_token(caplog):
    def telegram_error(request):
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    def network_error(request):
        raise httpx.ConnectError("boom")

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(make_client(telegram_error).send_message("x")) is None
        assert asyncio.run(make_client(network_error).get_updates(None)) is None

    assert "Unauthorized" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


def test_http_library_never_logs_request_urls_with_the_token():
    """httpx logs full URLs at INFO; for Telegram the token is in the URL. setup_logging must silence it."""
    import logging

    from app.logging_setup import setup_logging

    root = logging.getLogger()
    old_root_level = root.level
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)  # as if nothing had configured them
    try:
        root.setLevel(logging.INFO)
        setup_logging("INFO")
        # Their OWN level must be WARNING (not just inherited), or httpx would log URLs at INFO.
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    finally:
        root.setLevel(old_root_level)
