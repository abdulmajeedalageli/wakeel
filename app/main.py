"""FastAPI app: receives WhatsApp webhooks from Meta.

Flow for POST /webhook:
    1. Check Meta's signature (reject if invalid).
    2. Reply 200 OK straight away, so Meta doesn't retry and send duplicates.
    3. Process the messages in a background task:
         skip if we've seen this message ID before (dedupe)
         save the message, load the chat's status
         a chat you took over goes back to the bot after HUMAN_TIMEOUT_HOURS
         you handle it (human mode, /pause, or your work hours in schedule mode)
                  -> forward it to your Telegram; the bot stays silent
         otherwise -> the AI agent replies (text); photos, voice notes, files and
                      locations are passed on to your Telegram and the customer is told
                      the team will look (stickers etc.: we ask them to type); the reply is saved
Messages from the same customer are handled one at a time (a lock per chat), so
each reply sees the previous one.

Your Telegram console (commands, replies to customers) is in app/owner_console.py.
"""

import asyncio
import hmac
import json
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app import owner_console, replies, takeover
from app.agent import HISTORY_LIMIT, Agent
from app.backup import backup_database, backup_dir_for
from app.config import get_settings
from app.db import Database
from app.logging_setup import mask_phone, setup_logging
from app.properties import PropertyStore, build_property_store
from app.security import verify_signature
from app.telegram import TelegramClient
from app.webhook import IncomingMessage, OwnerEcho, StatusUpdate, extract_events
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

# How often to delete old entries from the dedupe and Telegram-link tables.
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
# How often to give timed-out human chats back to the bot.
TAKEOVER_CHECK_SECONDS = 10 * 60
# How often to check whether today's database backup exists yet.
BACKUP_CHECK_SECONDS = 60 * 60

# Message types we store but never answer (answering a 👍 reaction would be odd).
NO_REPLY_TYPES = {"reaction", "system"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)

    app.state.db = Database(settings.db_path)
    app.state.db.init()
    app.state.wa = WhatsAppClient(settings)
    app.state.properties = build_property_store(settings)
    app.state.chat_locks = defaultdict(asyncio.Lock)  # one lock per customer phone
    app.state.topic_locks = defaultdict(asyncio.Lock)  # creating Telegram threads (never nested in chat_locks)
    app.state.settings = settings
    app.state.telegram = None
    if settings.telegram_configured:
        app.state.telegram = TelegramClient(
            settings.telegram_bot_token.get_secret_value(),
            settings.telegram_owner_id,
            chat_id=settings.telegram_group_id,  # None = your private chat with the bot
        )
    else:
        logger.warning("Telegram is not configured: messages for you are only logged")
    app.state.alerts = owner_console.OwnerAlerts(app.state)
    app.state.agent = Agent(
        settings, app.state.properties, db=app.state.db, alerts=app.state.alerts
    )
    if not app.state.agent.enabled:
        logger.warning("GEMINI_API_KEY is not set: customers will get the fallback reply")

    tasks = [
        asyncio.create_task(cleanup_loop(app)),
        asyncio.create_task(takeover_loop(app)),
        asyncio.create_task(backup_loop(app)),
    ]
    if app.state.properties.configured:
        tasks.append(asyncio.create_task(sheet_refresh_loop(app, settings.sheet_refresh_minutes)))
    if app.state.telegram:
        tasks.append(asyncio.create_task(telegram_loop(app)))

    logger.info("App started (Graph API %s)", settings.whatsapp_api_version)
    yield

    for task in tasks:
        task.cancel()
    await app.state.wa.aclose()
    if app.state.telegram:
        await app.state.telegram.aclose()


async def sheet_refresh_loop(app: FastAPI, minutes: int) -> None:
    """Load the sheet now, then again every `minutes`. Failures keep the last good copy."""
    while True:
        # gspread is not async, so run it in a worker thread to keep the app responsive.
        await asyncio.to_thread(app.state.properties.refresh)
        await asyncio.sleep(max(minutes, 1) * 60)


async def cleanup_loop(app: FastAPI) -> None:
    """Every few hours, remove dedupe entries and Telegram reply links older than 7 days."""
    while True:
        try:
            removed = app.state.db.cleanup_processed_ids(older_than_days=7)
            removed += app.state.db.cleanup_telegram_links(older_than_days=7)
            if removed:
                logger.info("Cleanup removed %s old entries", removed)
        except Exception:
            logger.exception("Cleanup failed")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def takeover_loop(app: FastAPI) -> None:
    """Every few minutes, give chats you've left alone for HUMAN_TIMEOUT_HOURS back to the bot."""
    while True:
        try:
            await owner_console.return_expired_chats(app.state)
        except Exception:
            logger.exception("Takeover check failed")
        await asyncio.sleep(TAKEOVER_CHECK_SECONDS)


async def backup_loop(app: FastAPI) -> None:
    """Make one database backup per day (at startup, then checked every hour)."""
    settings = app.state.settings
    folder = backup_dir_for(settings.db_path)
    while True:
        try:
            created = await asyncio.to_thread(
                backup_database, settings.db_path, folder, settings.backup_keep_days
            )
            if created:
                logger.info("Database backup saved: %s", created.name)
        except Exception:
            logger.exception("Database backup failed")
        await asyncio.sleep(BACKUP_CHECK_SECONDS)


async def telegram_loop(app: FastAPI) -> None:
    """Receive your Telegram commands and replies (long polling), forever."""
    telegram, db = app.state.telegram, app.state.db
    await owner_console.start_console(app.state)  # command menu, topics on/off, console chat
    offset_text = db.get_state("telegram_offset")
    offset = int(offset_text) if offset_text else None
    while True:
        updates = await telegram.get_updates(offset)
        if updates is None:  # network problem or Telegram error: wait a bit, try again
            await asyncio.sleep(5)
            continue
        for update in updates:
            # Save progress BEFORE handling, so a crash can never send your reply twice.
            offset = update["update_id"] + 1
            db.set_state("telegram_offset", str(offset))
            try:
                await owner_console.handle_update(update, app.state)
            except Exception:
                logger.exception("Error while handling a Telegram update")


# No automatic API documentation pages (/docs, /redoc, /openapi.json): they would
# publicly describe the app. Caddy also only lets /webhook and /health through.
app = FastAPI(
    title="frame WhatsApp bot",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


PRIVACY_PAGE = Path(__file__).resolve().parent / "pages" / "privacy.html"


@app.get("/privacy", response_class=HTMLResponse)
async def privacy() -> HTMLResponse:
    """Public privacy policy (Meta needs its URL to publish the app). Text: app/pages/privacy.html."""
    return HTMLResponse(PRIVACY_PAGE.read_text(encoding="utf-8"))


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Basic status for you and for an uptime monitor. Never return secrets here.

    Returns 503 only if the database is down. A sheet problem is reported but
    keeps 200, because the bot still works with the last good copy.
    """
    db_ok = request.app.state.db.is_reachable()
    body = {
        "status": "ok" if db_ok else "error",
        "db": "ok" if db_ok else "error",
        "sheet": sheet_status(request.app.state.properties),
        "ai": "configured" if request.app.state.agent.enabled else "not_configured",
        "telegram": "configured" if request.app.state.telegram else "not_configured",
        "paused": request.app.state.db.is_paused() if db_ok else None,
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)


def sheet_status(store: PropertyStore) -> dict:
    if not store.configured:
        state = "not_configured"
    elif store.last_error:
        state = "error"  # still serving the last good copy, if any
    elif store.last_refresh_at is None:
        state = "loading"
    else:
        state = "ok"
    return {
        "status": state,
        "properties": len(store.properties),
        "skipped_rows": len(store.skipped_rows),
        "last_refresh": store.last_refresh_at,
    }


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    """Meta's one-time handshake when you click "Verify and save" in the dashboard."""
    expected = get_settings().whatsapp_verify_token.get_secret_value()
    if (
        hub_mode == "subscribe"
        and hub_verify_token is not None
        and hmac.compare_digest(hub_verify_token.encode(), expected.encode())
    ):
        logger.info("Webhook verified by Meta")
        return PlainTextResponse(hub_challenge or "")
    logger.warning("Webhook verification failed (wrong mode or verify token)")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    settings = get_settings()
    raw_body = await request.body()

    if not verify_signature(
        raw_body,
        request.headers.get("X-Hub-Signature-256"),
        settings.whatsapp_app_secret.get_secret_value(),
    ):
        logger.warning("Rejected webhook: invalid signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    # Runs after the response has been sent.
    background_tasks.add_task(
        process_payload, payload, request.app.state, settings.whatsapp_phone_number_id
    )
    return {"status": "received"}


async def process_payload(payload: dict, state, phone_number_id: str) -> None:
    """Handle every event in one webhook payload. Never raises.

    `state` is app.state: it holds wa, db, agent, telegram, alerts, settings and chat_locks.
    """
    try:
        events = extract_events(payload, phone_number_id)
    except Exception:
        logger.exception("Could not parse webhook payload")
        return

    for event in events:
        try:
            if isinstance(event, StatusUpdate):
                await handle_status(event)
            elif isinstance(event, IncomingMessage):
                await handle_message(event, state)
            elif isinstance(event, OwnerEcho):
                await handle_echo(event, state)
        except Exception:
            # One bad event must not stop the others or crash the app.
            logger.exception("Error while handling a webhook event")


async def handle_status(status: StatusUpdate) -> None:
    if status.status == "failed":
        logger.warning(
            "Message to %s failed: %s", mask_phone(status.recipient_phone), status.errors
        )
    else:
        logger.debug("Message to %s is %s", mask_phone(status.recipient_phone), status.status)


async def handle_message(msg: IncomingMessage, state) -> None:
    # Note: the DB calls below are quick, blocking SQLite calls. That is fine at
    # this scale (a few messages per minute).
    db: Database = state.db
    phone = mask_phone(msg.from_phone)

    # 1. Dedupe: Meta may deliver the same message more than once.
    if not db.mark_processed(msg.wa_message_id):
        logger.info("Skipping duplicate message from %s", phone)
        return

    # One message per customer at a time: if they send two quickly, the second
    # waits until the first is answered, so its reply sees the first one.
    async with state.chat_locks[msg.from_phone]:
        await _handle_in_order(msg, state)


async def _handle_in_order(msg: IncomingMessage, state) -> None:
    db: Database = state.db
    wa: WhatsAppClient = state.wa
    phone = mask_phone(msg.from_phone)

    # 2. Store the message and update the chat.
    chat, created = db.get_or_create_chat(msg.from_phone)
    # For photos/files that's the caption; for locations the place name (never coordinates).
    db.save_message(msg.from_phone, "in", msg.type, msg.note, msg.wa_message_id)
    db.record_customer_activity(msg.from_phone)
    logger.info("%s %s message from %s", "New chat:" if created else "Received", msg.type, phone)

    # 3. A chat you took over goes back to the bot after HUMAN_TIMEOUT_HOURS without
    #    a message from you (checked here too, not only every few minutes).
    settings = state.settings
    now = datetime.now(UTC)
    if takeover.timed_out(chat, now, settings.human_timeout_hours):
        db.hand_back(msg.from_phone)
        logger.info("Chat %s returned to the bot after the timeout", phone)
        await owner_console.notify_returned_to_bot(state, msg.from_phone)
        chat = db.get_chat(msg.from_phone)

    # 4. You handle this chat right now: forward it to your Telegram. No reply, and it
    #    isn't marked as read.
    reason = takeover.owner_handles(
        chat, db.is_paused(), settings.availability_mode, settings.work_hours, now
    )
    if reason:
        logger.info("Chat %s is yours (%s); not replying", phone, reason)
        if msg.type not in NO_REPLY_TYPES:
            await owner_console.forward_to_owner(state, msg, reason)
        return

    if msg.type in NO_REPLY_TYPES:
        return

    # 5. The bot answers.
    await wa.mark_read_with_typing(msg.wa_message_id)
    forwarded = None
    if msg.type == "text":
        # History = this customer's chat only, including the message just saved.
        history = db.get_recent_messages(msg.from_phone, limit=HISTORY_LIMIT)
        result = await state.agent.reply(history)
        if result.tool_calls:
            logger.info("Agent used %s tool call(s) for %s", len(result.tool_calls), phone)
        if not result.ok:
            # The customer gets the polite fallback; you get an alert so you can answer them.
            await state.alerts.bot_error(msg.from_phone, result.error)
        reply = result.text
    elif msg.media or msg.location:
        # The bot can't see photos or hear voice notes: pass them on to you. The customer is
        # only told "the team will take a look" if that worked.
        forwarded = await owner_console.forward_media(state, msg)
        reply = replies.media_received(msg.type) if forwarded else replies.NON_TEXT_REPLY
    else:
        reply = replies.NON_TEXT_REPLY
    await send_and_store(wa, db, msg.from_phone, reply)
    # Live copy in the customer's Telegram thread (only when threads are on).
    if msg.media or msg.location:
        # Their photo/file/location is already there (or a note that it couldn't be forwarded).
        await owner_console.mirror_exchange(state, msg.from_phone, None, reply, reply_to=forwarded)
    else:
        customer_text = msg.text if msg.type == "text" and msg.text else f"[{owner_console.type_name(msg.type)}]"
        await owner_console.mirror_exchange(state, msg.from_phone, customer_text, reply)


async def handle_echo(echo: OwnerEcho, state) -> None:
    """You wrote from the WhatsApp Business app (coexistence): take that chat over.

    Dormant until the number is onboarded with coexistence (see docs/META_SETUP.md).
    """
    db: Database = state.db
    if not db.mark_processed(echo.wa_message_id):
        return
    async with state.chat_locks[echo.to_phone]:
        db.save_message(echo.to_phone, "owner", echo.type, echo.text, echo.wa_message_id)
        db.take_over(echo.to_phone)
    await owner_console.refresh_thread_title(state, echo.to_phone)
    logger.info("You wrote to %s from the Business app; the bot is silent there", mask_phone(echo.to_phone))


async def send_and_store(wa: WhatsAppClient, db: Database, to: str, text: str) -> None:
    """Send a bot reply and save it. Failed sends are not saved (the customer never saw them)."""
    wa_message_id = await wa.send_text(to, text)
    if wa_message_id is None:
        logger.warning("Reply to %s was not sent", mask_phone(to))
        return
    db.save_message(to, "out", "text", text, wa_message_id)
