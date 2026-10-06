"""Small client for the WhatsApp Cloud API (Meta Graph API).

Only uses Meta's official HTTP API. Docs:
- Send messages:   https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages
- Typing + read:   https://developers.facebook.com/documentation/business-messaging/whatsapp/typing-indicators
- Media (download, upload):
                   https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media
"""

import logging

import httpx

from app.config import Settings
from app.logging_setup import mask_phone
from app.media import MAX_MEDIA_BYTES, MediaFile, MediaTooBig, base_mime, filename_for, read_capped

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.facebook.com"

# Meta error code when the 24-hour customer-service window has closed.
# We must not retry these.
REENGAGEMENT_ERROR_CODE = 131047

# Downloads and uploads of files can take longer than a text message.
MEDIA_TIMEOUT_SECONDS = 60.0


class WhatsAppClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self._base = f"{GRAPH_BASE_URL}/{settings.whatsapp_api_version}"
        self._phone_number_id = settings.whatsapp_phone_number_id
        self._url = f"{self._base}/{self._phone_number_id}/messages"
        token = settings.whatsapp_access_token.get_secret_value()
        # `http` can be passed in by tests (with a fake transport); normally we create our own.
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._headers = {"Authorization": f"Bearer {token}"}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_text(self, to: str, text: str) -> str | None:
        """Send a plain text message. Returns Meta's message ID, or None if it failed."""
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        return _message_id(await self._post(body, phone=to))

    async def send_media(
        self, to: str, kind: str, media_id: str, caption: str | None = None, filename: str | None = None
    ) -> str | None:
        """Send an uploaded photo ("image") or file ("document"). Returns Meta's message ID, or None."""
        media: dict = {"id": media_id}
        if caption:
            media["caption"] = caption[:1024]
        if kind == "document" and filename:
            media["filename"] = filename
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": kind,
            kind: media,
        }
        return _message_id(await self._post(body, phone=to))

    async def mark_read_with_typing(self, message_id: str) -> bool:
        """Show blue ticks and a "typing…" bubble to the customer.

        The bubble disappears when we reply, or after 25 seconds.
        """
        body = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        }
        return await self._post(body) is not None

    async def download_media(self, media_id: str, filename: str | None = None) -> MediaFile | None:
        """Fetch a file a customer sent. Raises MediaTooBig over the limit; None if it failed.

        Meta first gives a download link (valid for 5 minutes), which also needs our token.
        Never log that link.
        """
        try:
            resp = await self._http.get(
                f"{self._base}/{media_id}",
                params={"phone_number_id": self._phone_number_id},
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            logger.error("WhatsApp media lookup failed: %s", type(exc).__name__)
            return None
        info = self._check(resp)
        if not info:
            return None
        url = info.get("url") or ""
        if not url.startswith("https://"):
            logger.error("WhatsApp media lookup gave no usable download link")
            return None
        try:
            size = int(info.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MAX_MEDIA_BYTES:
            raise MediaTooBig(size)

        try:
            async with self._http.stream(
                "GET", url, headers=self._headers, timeout=MEDIA_TIMEOUT_SECONDS
            ) as download:
                if not download.is_success:
                    logger.error("WhatsApp media download failed: HTTP %s", download.status_code)
                    return None
                content = await read_capped(download)
        except httpx.HTTPError as exc:
            logger.error("WhatsApp media download failed: %s", type(exc).__name__)
            return None
        mime = info.get("mime_type") or "application/octet-stream"
        return MediaFile(content, mime, filename_for(mime, filename, stem="whatsapp"))

    async def upload_media(self, file: MediaFile) -> str | None:
        """Upload a file to Meta so it can be sent. Returns its media ID, or None if refused."""
        mime = base_mime(file.mime_type)
        try:
            resp = await self._http.post(
                f"{self._base}/{self._phone_number_id}/media",
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (file.filename, file.content, mime)},
                headers=self._headers,
                timeout=MEDIA_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            logger.error("WhatsApp media upload failed: %s", type(exc).__name__)
            return None
        return (self._check(resp) or {}).get("id")

    async def _post(self, body: dict, phone: str | None = None) -> dict | None:
        """POST to the messages endpoint. Logs errors and returns None instead of raising."""
        try:
            resp = await self._http.post(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            logger.error("WhatsApp API request failed (to %s): %s", mask_phone(phone), type(exc).__name__)
            return None
        return self._check(resp, phone)

    def _check(self, resp: httpx.Response, phone: str | None = None) -> dict | None:
        """Meta's JSON answer, or None (with a log line) if Meta refused the request."""
        if resp.is_success:
            try:
                return resp.json()
            except ValueError:
                logger.error("WhatsApp API gave an unreadable answer (HTTP %s)", resp.status_code)
                return None

        try:
            error = resp.json().get("error", {})
        except ValueError:
            error = {}
        code = error.get("code")
        if code == REENGAGEMENT_ERROR_CODE:
            logger.warning(
                "Cannot message %s: 24-hour window has closed (error %s). Not retrying.",
                mask_phone(phone), code,
            )
        else:
            logger.error(
                "WhatsApp API error (to %s): HTTP %s, code=%s, message=%s",
                mask_phone(phone), resp.status_code, code, error.get("message"),
            )
        return None


def _message_id(data: dict | None) -> str | None:
    if not data:
        return None
    messages = data.get("messages") or [{}]
    return messages[0].get("id")
