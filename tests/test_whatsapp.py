"""Tests for the Graph API client, using httpx's built-in fake transport (no network)."""

import asyncio
import json
import logging

import httpx

from app.config import get_settings
from app.whatsapp import WhatsAppClient


def make_client(handler) -> WhatsAppClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WhatsAppClient(get_settings(), http=http)


def test_send_text_posts_correct_request():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT1"}]})

    msg_id = asyncio.run(make_client(handler).send_text("966512341234", "hello"))

    assert msg_id == "wamid.OUT1"
    assert seen["url"] == "https://graph.facebook.com/v26.0/123456789/messages"
    assert seen["auth"] == "Bearer test-access-token"
    assert seen["body"]["to"] == "966512341234"
    assert seen["body"]["text"]["body"] == "hello"


def test_mark_read_with_typing_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True})

    assert asyncio.run(make_client(handler).mark_read_with_typing("wamid.IN1"))
    assert seen["body"] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.IN1",
        "typing_indicator": {"type": "text"},
    }


def test_24h_window_error_is_logged_once_and_not_retried(caplog):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(make_client(handler).send_text("966512341234", "hello"))

    assert result is None
    assert len(calls) == 1  # no retry
    assert "24-hour window" in caplog.text
    assert "966512341234" not in caplog.text  # phone is masked


def test_network_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    assert asyncio.run(make_client(handler).send_text("966512341234", "hello")) is None
