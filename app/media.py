"""Photos, voice notes and files passing between WhatsApp and your Telegram.

Files are only held in memory while they're passed on; they're never written to disk.
"""

import mimetypes
from dataclasses import dataclass

import httpx

# The biggest file we pass on, in either direction. It covers every WhatsApp photo
# (5 MB), voice note and video (16 MB), and it's the most a Telegram bot may download.
MAX_MEDIA_BYTES = 20 * 1024 * 1024
# WhatsApp's limit for photos (JPG/PNG); bigger images must be sent as documents.
WHATSAPP_MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Owner-facing names for WhatsApp message types.
TYPE_LABELS = {
    "image": "photo",
    "video": "video",
    "audio": "voice note",
    "document": "file",
    "location": "location",
}


@dataclass(frozen=True)
class MediaFile:
    content: bytes
    mime_type: str
    filename: str


class MediaTooBig(Exception):
    """The file is larger than MAX_MEDIA_BYTES."""

    def __init__(self, size: int):
        super().__init__(f"{size} bytes")
        self.size = size


def size_text(size: int) -> str:
    """12582912 -> "12 MB", 21495808 -> "20.5 MB", 3000 -> "2 KB"."""
    if size < 1024 * 1024:
        return f"{size // 1024 or 1} KB"
    return f"{size / (1024 * 1024):.1f}".removesuffix(".0") + " MB"


def base_mime(mime_type: str | None) -> str:
    """"audio/ogg; codecs=opus" -> "audio/ogg"."""
    return (mime_type or "application/octet-stream").split(";")[0].strip().lower()


def filename_for(mime_type: str | None, name: str | None = None, stem: str = "file") -> str:
    """A safe file name: the given one (without folders), or e.g. "photo.jpg" from the type."""
    if name:
        cleaned = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if cleaned:
            return cleaned[:100]
    ext = {"audio/ogg": ".ogg", "image/jpeg": ".jpg"}.get(base_mime(mime_type))
    ext = ext or mimetypes.guess_extension(base_mime(mime_type)) or ".bin"
    return f"{stem}{ext}"


async def read_capped(response: httpx.Response, limit: int = MAX_MEDIA_BYTES) -> bytes:
    """Read a streamed download, stopping as soon as it passes `limit` (raises MediaTooBig)."""
    chunks, total = [], 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise MediaTooBig(total)
        chunks.append(chunk)
    return b"".join(chunks)
