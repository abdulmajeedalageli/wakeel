"""Small client for the Telegram Bot API (https://core.telegram.org/bots/api), using httpx.

The bot talks to ONE "console chat": a Telegram group with Topics (TELEGRAM_GROUP_ID), or
otherwise your private chat with the bot (TELEGRAM_OWNER_ID). With topics on, that chat has
one topic per customer (in a private chat, topics are switched on for the bot in @BotFather).
We receive your commands with "long polling" (getUpdates): the app asks Telegram
"anything new?" and Telegram answers as soon as there is. This needs no public URL,
so it works the same on your PC and on the server.
"""

import json
import logging

import httpx

from app.media import MAX_MEDIA_BYTES, MediaFile, MediaTooBig, filename_for, read_capped

logger = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4096
TELEGRAM_MAX_CAPTION = 1024
# Method and form field for each kind of file. Whatever Telegram refuses is sent again as a file.
FILE_METHODS = {
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "voice": ("sendVoice", "voice"),
    "audio": ("sendAudio", "audio"),
    "document": ("sendDocument", "document"),
}
POLL_TIMEOUT_SECONDS = 50  # how long Telegram may hold a getUpdates request open

# The command menu in your Telegram app. Command names must be Latin letters (a Telegram rule).
COMMANDS = [
    ("status", "حالة البوت والمحادثات اللي استلمتها"),
    ("human", "استلم محادثة: /human 9665XXXXXXXX"),
    ("bot", "رجّع المحادثة للبوت: /bot 9665XXXXXXXX"),
    ("pause", "أوقف البوت عن الرد على الكل"),
    ("resume", "شغّل البوت من جديد"),
    ("leads", "آخر 10 عملاء محتملين"),
    ("lead", "بيانات عميل: /lead 9665XXXXXXXX"),
    ("forget", "احذف بيانات عميل: /forget 9665XXXXXXXX"),
    ("chatid", "رقم هذي المحادثة (للإعداد)"),
    ("staff", "الفريق: عرض وإضافة وحذف الموظفين"),
    ("help", "طريقة استخدام لوحة التحكم"),
]


def split_text(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Split long text into Telegram-sized pieces, preferring to break at line ends."""
    pieces = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        pieces.append(text[:cut])
        text = text[cut:].lstrip("\n")
    pieces.append(text)
    return pieces


class TelegramClient:
    def __init__(
        self, token: str, owner_id: int, http: httpx.AsyncClient | None = None, chat_id: int | None = None
    ):
        # The token is part of the URL, so never log URLs or httpx request details.
        self._base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        self.owner_id = owner_id
        # The console chat: your group (TELEGRAM_GROUP_ID) or your private chat with the bot.
        self.chat_id = chat_id or owner_id
        self._http = http or httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS + 15)
        # Set at startup (and on /status): True if the console chat can have one topic per customer.
        self.topics_enabled = False

    @property
    def is_group(self) -> bool:
        return self.chat_id != self.owner_id

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_message(
        self, text: str, reply_to: int | None = None, thread_id: int | None = None, chat_id: int | None = None
    ) -> int | None:
        """Send text to the console chat (into a customer's topic if thread_id is given).

        `chat_id` sends to another chat instead (only used to answer /chatid).

        Returns the (last) Telegram message ID, or None if it failed.
        """
        message_id = None
        for i, piece in enumerate(split_text(text)):
            body: dict = {
                "chat_id": chat_id or self.chat_id,
                "text": piece,
                "link_preview_options": {"is_disabled": True},
            }
            if thread_id:
                body["message_thread_id"] = thread_id
            if reply_to and i == 0:
                body["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            result = await self._call("sendMessage", body)
            if result is None:
                return None
            message_id = result.get("message_id")
        return message_id

    async def send_file(
        self,
        kind: str,
        file: MediaFile,
        caption: str | None = None,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> int | None:
        """Send you a photo, video, voice note, audio or document. Returns its message ID, or None."""
        form: dict = {"chat_id": self.chat_id}
        if caption:
            form["caption"] = caption[:TELEGRAM_MAX_CAPTION]
        if thread_id:
            form["message_thread_id"] = thread_id
        if reply_to:
            form["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        method, field = FILE_METHODS.get(kind, FILE_METHODS["document"])
        result = await self._call(method, form, files={field: (file.filename, file.content, file.mime_type)})
        if result is None and field != "document":
            # E.g. a format Telegram can't show as a photo or voice note: send it as a plain file.
            result = await self._call(
                "sendDocument", form, files={"document": (file.filename, file.content, file.mime_type)}
            )
        return (result or {}).get("message_id")

    async def send_location(
        self, latitude: float, longitude: float, reply_to: int | None = None, thread_id: int | None = None
    ) -> int | None:
        """Send you a map pin. Returns its message ID, or None."""
        body: dict = {"chat_id": self.chat_id, "latitude": latitude, "longitude": longitude}
        if thread_id:
            body["message_thread_id"] = thread_id
        if reply_to:
            body["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return ((await self._call("sendLocation", body)) or {}).get("message_id")

    async def download_file(
        self, file_id: str, mime_type: str | None = None, filename: str | None = None
    ) -> MediaFile | None:
        """Fetch a photo or file you sent to the bot. Raises MediaTooBig over the limit; None if it failed.

        The download link contains the bot token: never log it.
        """
        info = await self._call("getFile", {"file_id": file_id})
        if not info or not info.get("file_path"):
            return None
        if (info.get("file_size") or 0) > MAX_MEDIA_BYTES:
            raise MediaTooBig(info["file_size"])
        try:
            async with self._http.stream("GET", f"{self._file_base}/{info['file_path']}") as download:
                if not download.is_success:
                    logger.warning("Telegram file download failed: HTTP %s", download.status_code)
                    return None
                content = await read_capped(download)
        except httpx.HTTPError as exc:
            logger.warning("Telegram file download failed: %s", type(exc).__name__)
            return None
        mime = mime_type or "application/octet-stream"
        name = filename or info["file_path"].rsplit("/", 1)[-1]
        return MediaFile(content, mime, filename_for(mime, name, stem="telegram"))

    async def get_updates(self, offset: int | None) -> list[dict] | None:
        """Wait for new messages to the bot. Returns a list (maybe empty), or None on error."""
        body: dict = {"timeout": POLL_TIMEOUT_SECONDS, "allowed_updates": ["message"]}
        if offset is not None:
            body["offset"] = offset
        return await self._call("getUpdates", body)

    async def get_me(self) -> dict | None:
        """The bot's own profile, including has_topics_enabled (threads in private chats)."""
        return await self._call("getMe", {})

    async def get_chat(self) -> dict | None:
        """Details of the console chat, including is_forum (a group with topics)."""
        return await self._call("getChat", {"chat_id": self.chat_id})

    async def refresh_topics_enabled(self) -> bool:
        """Check whether the console chat can have one topic per customer (keeps the old value on errors).

        A group: Topics switched on in the group (is_forum). Your private chat: topics
        switched on for the bot in @BotFather (has_topics_enabled).
        """
        if self.is_group:
            info = await self.get_chat()
            if info is not None:
                self.topics_enabled = bool(info.get("is_forum"))
        else:
            info = await self.get_me()
            if info is not None:
                self.topics_enabled = bool(info.get("has_topics_enabled"))
        return self.topics_enabled

    async def create_forum_topic(self, name: str) -> int | None:
        """Create a thread in your chat with the bot. Returns its thread ID, or None."""
        result = await self._call("createForumTopic", {"chat_id": self.chat_id, "name": name[:128]})
        return (result or {}).get("message_thread_id")

    async def edit_forum_topic(self, thread_id: int, name: str) -> bool:
        """Rename a thread (e.g. to show 🤖/🙋 and the customer's name)."""
        body = {"chat_id": self.chat_id, "message_thread_id": thread_id, "name": name[:128]}
        return await self._call("editForumTopic", body) is not None

    async def delete_forum_topic(self, thread_id: int) -> bool:
        """Delete a thread together with all its messages."""
        body = {"chat_id": self.chat_id, "message_thread_id": thread_id}
        return await self._call("deleteForumTopic", body) is not None

    async def set_my_commands(self) -> None:
        """Show the command menu (/status, /human, ...) in your Telegram app (and in the group)."""
        commands = [{"command": c, "description": d} for c, d in COMMANDS]
        await self._call("setMyCommands", {"commands": commands})
        if self.is_group:
            await self._call(
                "setMyCommands", {"commands": commands, "scope": {"type": "chat", "chat_id": self.chat_id}}
            )

    async def _call(self, method: str, body: dict, files: dict | None = None):
        try:
            if files:
                # A file upload is a form: every value is text, nested ones as JSON.
                form = {k: json.dumps(v) if isinstance(v, dict) else str(v) for k, v in body.items()}
                resp = await self._http.post(f"{self._base}/{method}", data=form, files=files)
            else:
                resp = await self._http.post(f"{self._base}/{method}", json=body)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Telegram %s failed: %s", method, type(exc).__name__)
            return None
        if not data.get("ok"):
            logger.warning(
                "Telegram %s error %s: %s", method, data.get("error_code"), data.get("description")
            )
            return None
        return data.get("result")
