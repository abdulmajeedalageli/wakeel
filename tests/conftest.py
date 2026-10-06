"""Shared test setup.

Test settings are set as environment variables BEFORE the app is imported,
so tests never need a real .env file or real keys. Meta is replaced by FakeWhatsApp.
"""

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path

import pytest

TEST_APP_SECRET = "test-app-secret"
TEST_VERIFY_TOKEN = "test-verify-token"
TEST_PHONE_NUMBER_ID = "123456789"

os.environ.update(
    {
        "WHATSAPP_ACCESS_TOKEN": "test-access-token",
        "WHATSAPP_PHONE_NUMBER_ID": TEST_PHONE_NUMBER_ID,
        "WHATSAPP_APP_SECRET": TEST_APP_SECRET,
        "WHATSAPP_VERIFY_TOKEN": TEST_VERIFY_TOKEN,
        "WHATSAPP_API_VERSION": "v26.0",
        # Keeps the app's startup DB out of the project folder; each test then gets its own DB.
        "DB_PATH": str(Path(tempfile.mkdtemp()) / "startup.db"),
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402

# Ignore your real .env completely, so tests can never reach Meta or Google
# (e.g. once GOOGLE_SHEET_ID is filled in there).
Settings.model_config["env_file"] = None
get_settings.cache_clear()

from app.db import Database  # noqa: E402
from app.main import app  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def sign(body: bytes, secret: str = TEST_APP_SECRET) -> str:
    """Build the X-Hub-Signature-256 header value, the same way Meta does."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeWhatsApp:
    """Records what the app would have sent to Meta."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.read: list[str] = []
        self.fail_sends = False  # set True to simulate Meta rejecting a send
        # Customers' files Meta would give us: media_id -> MediaFile, or an int (a size that's too big).
        self.media: dict = {}
        self.downloads: list[str] = []
        self.uploads: list = []  # MediaFile uploaded (your photos/files for customers)
        self.fail_uploads = False  # set True to simulate Meta refusing a file
        self.sent_media: list[tuple] = []  # (to, kind, media_id, caption, filename)

    async def send_text(self, to: str, text: str) -> str | None:
        self.sent.append((to, text))
        return None if self.fail_sends else f"wamid.FAKE_OUT{len(self.sent)}"

    async def download_media(self, media_id: str, filename: str | None = None):
        from app.media import MediaTooBig

        self.downloads.append(media_id)
        item = self.media.get(media_id)
        if isinstance(item, int):
            raise MediaTooBig(item)
        return item

    async def upload_media(self, file) -> str | None:
        self.uploads.append(file)
        return None if self.fail_uploads else f"media.UP{len(self.uploads)}"

    async def send_media(self, to, kind, media_id, caption=None, filename=None) -> str | None:
        self.sent_media.append((to, kind, media_id, caption, filename))
        return None if self.fail_sends else f"wamid.FAKE_MEDIA{len(self.sent_media)}"

    async def mark_read_with_typing(self, message_id: str) -> bool:
        self.read.append(message_id)
        return True

    async def aclose(self) -> None:
        pass


@pytest.fixture
def sample_rows() -> list[list]:
    """The Properties tab of test_data/sample_properties.xlsx, as Google returns it."""
    return load_fixture("sample_properties.json")


@pytest.fixture
def sheet_config():
    from app.properties import SHEET_CONFIG_FILE
    from app.sheet import SheetConfig

    return SheetConfig.load(SHEET_CONFIG_FILE)


@pytest.fixture
def locations():
    from app.locations import Locations
    from app.properties import LOCATIONS_FILE

    return Locations.load(LOCATIONS_FILE)


@pytest.fixture
def store(sample_rows, sheet_config, locations):
    """A PropertyStore loaded with the sample sheet (no Google involved)."""
    from app.properties import PropertyStore

    s = PropertyStore(lambda: sample_rows, sheet_config, locations)
    assert s.refresh()
    return s


AGENT_REPLY = "reply from the (fake) AI agent"


class FakeAgent:
    """Stands in for app.agent.Agent in webhook tests. Records the histories it's given."""

    enabled = True

    def __init__(self):
        self.histories: list[list] = []
        self.delay = 0.0
        self.running = 0
        self.max_running = 0  # how many replies ran at the same time (must stay 1 per chat)
        self.fail_with: str | None = None  # set to simulate the agent using its fallback

    async def reply(self, history):
        from app.agent import AgentReply

        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.histories.append(history)
        await asyncio.sleep(self.delay)
        self.running -= 1
        if self.fail_with:
            return AgentReply(text="fallback", ok=False, error=self.fail_with)
        return AgentReply(text=AGENT_REPLY, ok=True)


OWNER_TELEGRAM_ID = 4242


class FakeTelegram:
    """Stands in for app.telegram.TelegramClient. Records what the app told the owner."""

    def __init__(self, owner_id: int = OWNER_TELEGRAM_ID):
        self.owner_id = owner_id
        self.chat_id = owner_id  # the console chat: your private chat, or a group ID
        self.sent_chat_ids: list[int] = []  # chat of each sent message
        self.commands_set = 0
        self.sent: list[tuple[str, int | None]] = []  # (text, reply_to)
        self.sent_thread_ids: list[int | None] = []  # thread of each sent message (None = main chat)
        self._next_id = 1000
        # Threads (one per customer): off by default, like a bot without topics in @BotFather.
        self.topics_enabled = False
        self.topics: dict[int, str] = {}  # thread_id -> title
        self.deleted_topics: list[int] = []
        self.broken_threads: set[int] = set()  # sending into these fails (thread deleted by hand)
        self._next_thread = 500
        # Photos, files and map pins sent to the owner: dicts with kind, file, caption, thread_id...
        self.files_sent: list[dict] = []
        self.locations: list[dict] = []
        self.fail_files = False  # set True to simulate Telegram refusing a file
        # Files the owner sent to the bot: file_id -> bytes, or an int (a size that's too big).
        self.downloadable: dict = {}
        self.downloads: list[str] = []

    async def send_message(self, text: str, reply_to: int | None = None, thread_id: int | None = None,
                           chat_id: int | None = None):
        if thread_id in self.broken_threads:
            return None
        self._next_id += 1
        self.sent.append((text, reply_to))
        self.sent_thread_ids.append(thread_id)
        self.sent_chat_ids.append(chat_id or self.chat_id)
        return self._next_id

    async def set_my_commands(self) -> None:
        self.commands_set += 1

    async def refresh_topics_enabled(self) -> bool:
        """Same logic as the real client: a group asks getChat, your private chat asks getMe."""
        if self.chat_id != self.owner_id:
            info, key = await self.get_chat(), "is_forum"
        else:
            info, key = await self.get_me(), "has_topics_enabled"
        if info is not None:
            self.topics_enabled = bool(info.get(key))
        return self.topics_enabled

    async def get_chat(self) -> dict:
        return {"id": self.chat_id, "type": "supergroup", "is_forum": self.topics_enabled}

    async def send_file(self, kind, file, caption=None, reply_to=None, thread_id=None):
        if thread_id in self.broken_threads or self.fail_files:
            return None
        self._next_id += 1
        self.files_sent.append({"kind": kind, "file": file, "caption": caption, "reply_to": reply_to,
                                "thread_id": thread_id, "message_id": self._next_id})
        return self._next_id

    async def send_location(self, latitude, longitude, reply_to=None, thread_id=None):
        if thread_id in self.broken_threads:
            return None
        self._next_id += 1
        self.locations.append({"latitude": latitude, "longitude": longitude, "reply_to": reply_to,
                               "thread_id": thread_id, "message_id": self._next_id})
        return self._next_id

    async def download_file(self, file_id, mime_type=None, filename=None):
        from app.media import MediaFile, MediaTooBig, filename_for

        self.downloads.append(file_id)
        item = self.downloadable.get(file_id)
        if isinstance(item, int):
            raise MediaTooBig(item)
        if item is None:
            return None
        mime = mime_type or "application/octet-stream"
        return MediaFile(item, mime, filename_for(mime, filename, stem="telegram"))

    async def get_me(self) -> dict:
        return {"id": 1, "is_bot": True, "has_topics_enabled": self.topics_enabled}

    async def create_forum_topic(self, name: str) -> int:
        await asyncio.sleep(0)  # let concurrent callers interleave, like a real network call
        self._next_thread += 1
        self.topics[self._next_thread] = name
        return self._next_thread

    async def edit_forum_topic(self, thread_id: int, name: str) -> bool:
        self.topics[thread_id] = name
        return True

    async def delete_forum_topic(self, thread_id: int) -> bool:
        self.topics.pop(thread_id, None)
        self.deleted_topics.append(thread_id)
        return True

    def in_thread(self, thread_id: int) -> list[str]:
        """Texts sent into one thread."""
        return [text for (text, _), tid in zip(self.sent, self.sent_thread_ids) if tid == thread_id]

    @property
    def last_message_id(self) -> int:
        return self._next_id

    async def aclose(self) -> None:
        pass


@pytest.fixture
def fake_wa() -> FakeWhatsApp:
    return FakeWhatsApp()


@pytest.fixture
def fake_telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def make_state(fake_wa, fake_agent, fake_telegram, db):
    """Build an app.state-like object for calling handlers directly (no web server)."""
    from collections import defaultdict
    from types import SimpleNamespace

    def _make(**settings_overrides):
        settings = get_settings().model_copy(update=settings_overrides)
        from app.owner_console import OwnerAlerts

        state = SimpleNamespace(
            wa=fake_wa, db=db, agent=fake_agent, telegram=fake_telegram,
            settings=settings, chat_locks=defaultdict(asyncio.Lock),
            topic_locks=defaultdict(asyncio.Lock),
        )
        state.alerts = OwnerAlerts(state)
        return state

    return _make


@pytest.fixture
def fake_agent() -> FakeAgent:
    return FakeAgent()


@pytest.fixture
def db(tmp_path) -> Database:
    """A fresh, empty database for each test."""
    database = Database(str(tmp_path / "test.db"))
    database.init()
    return database


@pytest.fixture
def client(fake_wa, fake_agent, fake_telegram, db):
    with TestClient(app) as test_client:
        real_wa = app.state.wa
        app.state.wa = fake_wa
        app.state.db = db
        app.state.agent = fake_agent
        app.state.telegram = fake_telegram
        yield test_client
        app.state.wa = real_wa  # so the app's shutdown closes the real client
