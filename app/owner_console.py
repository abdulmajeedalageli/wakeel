"""Your Telegram console: commands, replies to customers, alerts, and forwarded messages.

Everything the app says to you on Telegram is in this file (in Arabic), so it's easy to reword.

`state` is the app's shared state (app.state in main.py): db, wa, telegram, settings,
chat_locks, topic_locks, alerts. `state.telegram` is None when Telegram isn't configured;
then everything meant for you is only logged.

Where it all goes (the "console chat"):
  - TELEGRAM_GROUP_ID set: a Telegram group with Topics. Each customer gets their own topic
    (title like "🤖 خالد · 9665…"); commands go in the group's General topic.
  - otherwise: your private chat with the bot, with one thread per customer if topics are
    switched on for the bot in @BotFather.
  Without topics, everything arrives in the one chat and you answer with Reply.
Whatever you type inside a customer's topic is sent to that customer. Only messages from YOU
(TELEGRAM_OWNER_ID) and your team (employees you added with /staff), inside the console chat,
are ever acted on. Employees can do everything you can, except adding or removing employees.

Photos, voice notes, files and locations from customers are passed on to you too, and
photos or files you send in a customer's topic (or as a Reply) go to that customer.

Your listings: send your Excel file (.xlsx) in the General topic (not in a customer's topic)
and the bot loads it right away (app/excel_listings.py).
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app import takeover
from app.db import LEAD_FIELDS
from app.excel_listings import ExcelFormatError
from app.logging_setup import mask_phone
from app.media import MAX_MEDIA_BYTES, WHATSAPP_MAX_IMAGE_BYTES, MediaTooBig, base_mime
from app.properties import listings_file_for
from app.webhook import IncomingMessage

logger = logging.getLogger(__name__)

HELP = """🛠 لوحة تحكم بوت frame

/status: حالة البوت والمحادثات اللي استلمتها
/human 9665XXXXXXXX: استلم محادثة (البوت يسكت فيها)
/bot 9665XXXXXXXX: رجّع المحادثة للبوت
/pause: أوقف البوت عن الرد على الكل (الرسائل توصلك هنا)
/resume: شغّل البوت من جديد
/leads: آخر 10 عملاء محتملين
/lead 9665XXXXXXXX: بيانات عميل واحد
/forget 9665XXXXXXXX: احذف كل بيانات العميل (يطلب تأكيد)
/chatid: رقم هذي المحادثة (للإعداد)
/staff: الفريق (الموظفين اللي يقدرون يردون على العملاء)
/staff إضافة أحمد: كرد على رسالة الموظف في المجموعة (للمالك بس)
/staff حذف أحمد: يشيله من الفريق (للمالك بس)

لتحديث العقارات: أرسل ملف الإكسل (xlsx) هنا في الموضوع العام، مو داخل موضوع عميل.

للرد على عميل: اكتب داخل الموضوع الخاص فيه، أو سوّ رد على وحدة من رسائله.
الصور والملفات (PDF…) اللي ترسلها هناك توصل للعميل بعد.
داخل موضوع العميل، الأوامر /human و /bot و /lead و /forget ما تحتاج رقم.
تقدر تكتب الرقم بأي شكل: 9665… أو 05…"""

FORWARD_REASONS = {
    "human": "استلمت هذي المحادثة",
    "paused": "البوت موقوف",
    "work_hours": "وقت دوامك",
    None: "لك: البوت ما يقدر يفتح الصور والملفات",  # the bot answers this chat
}

ONLY_TEXT_AND_FILES = "أقدر أرسل للعميل نص أو صور أو ملفات (PDF و Word و Excel…) بس."

MESSAGE_LABELS = {"in": "👤 العميل", "out": "🤖 البوت", "owner": "🧑 الفريق"}  # "owner" = you or an employee

# WhatsApp message types, as shown to you.
TYPE_NAMES = {
    "text": "نص",
    "image": "صورة",
    "video": "فيديو",
    "audio": "رسالة صوتية",
    "document": "ملف",
    "location": "موقع",
    "sticker": "ملصق",
    "contacts": "جهة اتصال",
    "reaction": "تفاعل",
}

# Lead details, as shown to you. The values of deal and type are translated too.
LEAD_LABELS = {
    "name": "الاسم",
    "budget": "الميزانية",
    "deal": "الطلب",
    "type": "نوع العقار",
    "districts": "الأحياء",
    "bedrooms": "الغرف",
    "timeline": "الموعد المتوقع",
    "notes": "ملاحظات",
}
LEAD_VALUES = {
    "deal": {"sale": "شراء", "rent": "إيجار"},
    "type": {"apartment": "شقة", "villa": "فيلا", "land": "أرض", "floor": "دور",
             "office": "مكتب", "shop": "محل"},
}

# Why the agent alerts you (the notify_owner tool). The labels are shown to you.
ALERT_REASONS = {
    "viewing_request": "طلب معاينة",
    "negotiation": "تفاوض على السعر",
    "ready_to_proceed": "جاهز للشراء/الإيجار",
    "question_not_in_data": "سؤال ما عندنا جوابه",
    "wants_human": "يبي يكلم موظف",
    "upset": "عميل منزعج",
    "price_request": "يسأل عن السعر",  # the bot never tells prices: the team does
}
# The same alert for the same chat isn't repeated within this time.
ALERT_COOLDOWN = timedelta(minutes=60)

# Words that confirm /forget.
CONFIRM_WORDS = ("confirm", "تأكيد")

# Your team: employees who can answer customers (Telegram user ID -> name), kept in app_state.
TEAM_STATE_KEY = "telegram_staff"
ADD_WORDS = ("إضافة", "اضافة", "add")
REMOVE_WORDS = ("حذف", "remove")

# Keys of a Telegram message that are something YOU wrote or sent. Anything else (a topic
# being created or renamed, someone joining the group...) is a service message: ignored.
OWNER_CONTENT_KEYS = ("text", "caption", "photo", "document", "voice", "video", "video_note",
                      "audio", "sticker", "animation", "location", "contact", "poll")


def _display(phone: str) -> str:
    # Digits only: a leading "+" would jump to the wrong side inside Arabic text.
    return phone


def _duration(delta: timedelta) -> str:
    """timedelta -> "2س 5د" / "35د" / "الآن"."""
    minutes = int(delta.total_seconds() // 60)
    if minutes <= 0:
        return "الآن"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}س {minutes}د" if hours else f"{minutes}د"


def _size(size: int) -> str:
    """12582912 -> "12 ميجا", 3000 -> "2 كيلو"."""
    if size < 1024 * 1024:
        return f"{size // 1024 or 1} كيلو"
    return f"{size / (1024 * 1024):.1f}".removesuffix(".0") + " ميجا"


def type_name(msg_type: str) -> str:
    """A WhatsApp message type in Arabic, e.g. "image" -> "صورة"."""
    return TYPE_NAMES.get(msg_type, "رسالة")


def _error_text(error: str | None) -> str:
    """Why the AI failed, in plain Arabic (the technical reason comes from app/agent.py)."""
    if not error:
        return "خطأ غير معروف"
    if error == "timed out":
        return "انتهت المهلة"
    if error == "GEMINI_API_KEY is not set":
        return "مفتاح Gemini غير موجود"
    if error == "Gemini error 429":
        return "انتهى الحد المسموح في Gemini، غالباً الحد المجاني اليومي. فعّل الفوترة"
    if error in ("Gemini error 401", "Gemini error 403"):
        return "مفتاح Gemini مرفوض"
    if error.startswith("Gemini error 5"):
        return "مشكلة مؤقتة عند Gemini"
    return f"خطأ تقني: {error}"


def _threads_on(state) -> bool:
    return state.telegram is not None and getattr(state.telegram, "topics_enabled", False)


def _in_group(state) -> bool:
    telegram = state.telegram
    return telegram is not None and getattr(telegram, "chat_id", telegram.owner_id) != telegram.owner_id


def _thread_title(state, phone: str) -> str:
    """"🤖 خالد · 966512345678": who's handling the chat (bot or you), name if known, number."""
    chat = state.db.get_chat(phone)
    icon = "🙋" if chat and chat.status == "human" else "🤖"
    lead = state.db.get_lead(phone)
    name = f"{lead['name']} · " if lead and lead.get("name") else ""
    return f"{icon} {name}{_display(phone)}"


async def start_console(state) -> None:
    """At startup: the command menu, whether topics are on, and a clean start in a new console chat.

    Telegram's message and topic IDs belong to one chat. If the console moved (for example
    from your private chat to a group), the old topic and reply links are forgotten, so
    customers get fresh topics in the new chat.
    """
    telegram, db = state.telegram, state.db
    await telegram.set_my_commands()
    await telegram.refresh_topics_enabled()
    logger.info("Telegram console: %s; one topic per customer: %s",
                "group" if _in_group(state) else "private chat", "on" if telegram.topics_enabled else "off")
    previous = db.get_state("telegram_console_chat") or str(telegram.owner_id)
    if previous != str(telegram.chat_id):
        db.reset_telegram_mapping()
        logger.info("The Telegram console moved to another chat: old topic and reply links cleared")
    db.set_state("telegram_console_chat", str(telegram.chat_id))


def _team(state) -> dict[int, str]:
    """Your employees: Telegram user ID -> name (added with /staff إضافة)."""
    raw = state.db.get_state(TEAM_STATE_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return {int(user_id): name for user_id, name in data.items()}


def _save_team(state, team: dict[int, str]) -> None:
    state.db.set_state(TEAM_STATE_KEY, json.dumps({str(k): v for k, v in team.items()}, ensure_ascii=False))


def _is_team(state, user_id) -> bool:
    """You (the owner) or one of your employees."""
    return user_id == state.telegram.owner_id or user_id in _team(state)


async def customer_thread(state, phone: str) -> int | None:
    """This customer's topic ID (created on first use), or None if topics aren't available."""
    if not _threads_on(state):
        return None
    existing = state.db.get_thread(phone)
    if existing:
        return existing
    # A separate lock from chat_locks (which may already be held by the caller), so two
    # messages arriving together can't create two topics for the same customer.
    async with state.topic_locks[phone]:
        existing = state.db.get_thread(phone)
        if existing:
            return existing
        thread_id = await state.telegram.create_forum_topic(_thread_title(state, phone))
        if thread_id:
            state.db.set_thread(phone, thread_id)
            logger.info("Created a Telegram topic for %s", mask_phone(phone))
        return thread_id


async def refresh_thread_title(state, phone: str) -> None:
    """Update the topic's icon (🤖/🙋) and name. Does nothing if the customer has no topic."""
    if not _threads_on(state):
        return
    thread_id = state.db.get_thread(phone)
    if thread_id:
        await state.telegram.edit_forum_topic(thread_id, _thread_title(state, phone))


async def _deliver(state, send, phone: str | None = None, thread_id: int | None = None) -> int | None:
    """Send you something with `send(thread_id)` (a text, photo, map pin...).

    About a customer (`phone`): it goes into their topic (if topics are on), and the
    message is linked to them so a Reply to it reaches them. `thread_id` forces a topic
    (used to answer a command inside the topic it was typed in).
    """
    if state.telegram is None:
        return None
    if thread_id is None and phone:
        thread_id = await customer_thread(state, phone)
    message_id = await send(thread_id)
    if message_id is None and thread_id and phone and state.db.get_thread(phone) == thread_id:
        # The topic is gone (e.g. deleted by hand): forget it and use the main chat this time.
        state.db.clear_thread(phone)
        message_id = await send(None)
    if message_id and phone:
        state.db.link_telegram_message(message_id, phone)
    return message_id


async def _say(
    state,
    text: str,
    reply_to: int | None = None,
    phone: str | None = None,
    thread_id: int | None = None,
) -> int | None:
    """Send you a text message (see _deliver for where it goes)."""
    if state.telegram is None:
        return None

    async def send(tid):
        return await state.telegram.send_message(text, reply_to=reply_to, thread_id=tid)

    return await _deliver(state, send, phone, thread_id)


async def mirror_exchange(
    state, phone: str, customer_text: str | None, reply_text: str, reply_to: int | None = None
) -> None:
    """Live copy of one exchange in the customer's topic: 👤 what they wrote, 🤖 the bot's reply.

    `customer_text` None: only the 🤖 line (their photo/file was already forwarded; `reply_to` it).
    """
    if not _threads_on(state) or not getattr(state.settings, "telegram_live_mirror", True):
        return
    text = f"🤖 {reply_text}" if customer_text is None else f"👤 {customer_text}\n🤖 {reply_text}"
    try:
        await _say(state, text, reply_to=reply_to, phone=phone)
    except Exception:
        logger.exception("Couldn't copy the conversation to Telegram for %s", mask_phone(phone))


def _lead_value(key: str, value: str) -> str:
    return LEAD_VALUES.get(key, {}).get(value, value)


def _lead_line(lead: dict | None) -> str | None:
    """'الميزانية: 900 ألف · الطلب: شراء · …' (without the name), or None if nothing saved."""
    if not lead:
        return None
    parts = [f"{LEAD_LABELS[key]}: {_lead_value(key, lead[key])}"
             for key in LEAD_FIELDS if key != "name" and lead.get(key)]
    return " · ".join(parts) or None


class OwnerAlerts:
    """Alerts to you on Telegram, with protection against repeats.

    The "already sent" memory lives only in the app's memory (no customer data is
    stored for it); after a restart an alert may be sent once more.
    """

    def __init__(self, state):
        self.state = state
        self._last_sent: dict[tuple[str, str], datetime] = {}

    def _recently_sent(self, phone: str, reason: str) -> bool:
        last = self._last_sent.get((phone, reason))
        return last is not None and datetime.now(UTC) - last < ALERT_COOLDOWN

    async def notify(self, phone: str, reason: str, summary: str) -> str:
        """The agent's notify_owner tool. Returns "sent", "duplicate", "not_configured" or "failed"."""
        if self._recently_sent(phone, reason):
            return "duplicate"
        lead = self.state.db.get_lead(phone)
        name = f" ({lead['name']})" if lead and lead.get("name") else ""
        lines = [f"🔔 {ALERT_REASONS.get(reason, reason)} · {_display(phone)}{name}", summary.strip()]
        lead_line = _lead_line(lead)
        if lead_line:
            lines.append(f"بيانات العميل: {lead_line}")
        last_text = next(
            (m.text for m in reversed(self.state.db.get_recent_messages(phone, limit=10))
             if m.direction == "in" and m.text),
            None,
        )
        if last_text:
            lines.append(f"آخر رسالة: «{last_text}»")
        lines.append(f"↩️ للرد عليه: اكتب في موضوعه أو سوّ رد على هذي الرسالة. لاستلام المحادثة: /human {phone}")
        return await self._send(phone, reason, "\n".join(lines))

    async def lead_updated(self, phone: str, changed_fields) -> None:
        """The agent saved lead details. A new name is shown in the customer's topic title."""
        if "name" in changed_fields:
            await refresh_thread_title(self.state, phone)

    async def bot_error(self, phone: str, error: str | None) -> str:
        """The bot couldn't answer and sent its fallback reply."""
        if self._recently_sent(phone, "bot_error"):
            return "duplicate"
        text = (
            f"⚠️ البوت ما قدر يرد على {_display(phone)} ({_error_text(error)}).\n"
            "وصلته الرسالة المعتادة «الفريق بيتواصل معك».\n"
            "↩️ رد عليه أنت متى ما قدرت."
        )
        return await self._send(phone, "bot_error", text)

    async def _send(self, phone: str, reason: str, text: str) -> str:
        if self.state.telegram is None:
            logger.warning("Alert for you (%s, %s); Telegram not configured", reason, mask_phone(phone))
            self._last_sent[(phone, reason)] = datetime.now(UTC)  # don't flood the log either
            return "not_configured"
        if await _say(self.state, text, phone=phone) is None:
            return "failed"  # not remembered, so the next attempt can still get through
        self._last_sent[(phone, reason)] = datetime.now(UTC)
        logger.info("Alert sent to you (%s, %s)", reason, mask_phone(phone))
        return "sent"


# --- messages the app sends you on its own ---

async def forward_to_owner(state, msg: IncomingMessage, reason: str) -> None:
    """A customer wrote in a chat that you handle: show it to you on Telegram."""
    if state.telegram is None:
        logger.info("Message from %s is for you (%s); Telegram not configured", mask_phone(msg.from_phone), reason)
        return
    if msg.media or msg.location:
        await forward_media(state, msg, reason)
        return
    name = f" ({msg.profile_name})" if msg.profile_name else ""
    body = msg.text if msg.type == "text" and msg.text else f"[{type_name(msg.type)}: ما يمكن تحويله]"
    if _threads_on(state) and await customer_thread(state, msg.from_phone):
        # In their own topic the number is already in the title.
        note = "" if reason == "human" else f"\n({FORWARD_REASONS.get(reason, reason)})"
        await _say(state, f"👤 {body}{note}", phone=msg.from_phone)
        return
    header = f"📩 {_display(msg.from_phone)}{name} · {FORWARD_REASONS.get(reason, reason)}"
    await _say(state, f"{header}\n{body}", phone=msg.from_phone)


def _telegram_kind(msg: IncomingMessage) -> str:
    """How Telegram should show a customer's file: photo, video, voice (plays inline), audio or document."""
    if msg.type == "image":
        return "photo"
    if msg.type == "video":
        return "video"
    if msg.type == "audio":
        return "voice" if msg.media.voice or base_mime(msg.media.mime_type) == "audio/ogg" else "audio"
    return "document"


async def forward_media(state, msg: IncomingMessage, reason: str | None = None) -> int | None:
    """Pass a customer's photo, video, voice note, file or location on to you.

    `reason` is why you handle the chat ("human", "paused", "work_hours"), or None when the
    bot answers it. Returns the Telegram message ID, or None if it couldn't be passed on
    (you then get a note saying so, if Telegram works at all).
    """
    if state.telegram is None or not (msg.media or msg.location):
        return None
    phone = msg.from_phone
    if await customer_thread(state, phone):
        # In their own topic the number is already in the title.
        head = "👤 "
        note = "" if reason in ("human", None) else f"\n({FORWARD_REASONS.get(reason, reason)})"
    else:
        name = f" ({msg.profile_name})" if msg.profile_name else ""
        head = f"📩 {_display(phone)}{name} · {FORWARD_REASONS.get(reason, reason)}\n"
        note = ""

    if msg.location:
        place = msg.location.label or "موقع"
        text_id = await _say(state, f"{head}📍 {place}{note}", phone=phone)
        latitude, longitude = msg.location.latitude, msg.location.longitude

        async def send_pin(tid):
            return await state.telegram.send_location(latitude, longitude, reply_to=text_id, thread_id=tid)

        pin_id = await _deliver(state, send_pin, phone)
        return pin_id or text_id

    try:
        file = await state.wa.download_media(msg.media.id, msg.media.filename)
        problem = None if file else "ما قدرت أنزّله من واتساب"
    except MediaTooBig as exc:
        file = None
        problem = f"حجمه كبير ({_size(exc.size)}؛ الحد الأقصى {_size(MAX_MEDIA_BYTES)})"
    caption_text = msg.media.caption or ""
    if file is None:
        extra = f" التعليق: {caption_text}" if caption_text else ""
        await _say(state, f"{head}📎 ما قدرت أحوّل {type_name(msg.type)}: {problem}.{extra}{note}", phone=phone)
        logger.warning("Couldn't forward a %s from %s", msg.type, mask_phone(phone))
        return None

    caption = (head + caption_text).rstrip() + note
    kind = _telegram_kind(msg)

    async def send_file(tid):
        return await state.telegram.send_file(kind, file, caption=caption, thread_id=tid)

    message_id = await _deliver(state, send_file, phone)
    if message_id is None:
        logger.warning("Couldn't forward a %s from %s to Telegram", msg.type, mask_phone(phone))
    return message_id


async def notify_returned_to_bot(state, phone: str) -> None:
    hours = state.settings.human_timeout_hours
    await refresh_thread_title(state, phone)
    await _say(
        state,
        f"↩️ {_display(phone)} رجع للبوت (ما وصلته رسالة منك خلال {hours:g} ساعة).",
        phone=phone,
    )


async def return_expired_chats(state) -> int:
    """Give timed-out human chats back to the bot (run every few minutes). Returns how many."""
    returned = 0
    for chat in state.db.expired_human_chats(state.settings.human_timeout_hours):
        async with state.chat_locks[chat.phone]:
            current = state.db.get_chat(chat.phone)  # re-check: you may have just written
            if current and takeover.timed_out(current, datetime.now(UTC), state.settings.human_timeout_hours):
                state.db.hand_back(chat.phone)
                logger.info("Chat %s returned to the bot after the timeout", mask_phone(chat.phone))
                await notify_returned_to_bot(state, chat.phone)
                returned += 1
    return returned


# --- messages you send to the bot ---

def _command_of(text: str) -> str:
    """"/status@frame_bot extra" -> "/status"."""
    return text.split(maxsplit=1)[0].split("@")[0].lower() if text.startswith("/") else ""


async def handle_update(update: dict, state) -> None:
    """One Telegram update (a message someone sent where the bot can see it)."""
    message = update.get("message") or {}
    sender = (message.get("from") or {}).get("id")
    chat_id = (message.get("chat") or {}).get("id")
    telegram = state.telegram
    if not message:
        return

    if (message.get("from") or {}).get("is_bot"):
        return  # e.g. the bot's own "topic created" notice
    raw_text = (message.get("text") or "").strip()
    if sender == telegram.owner_id and _command_of(raw_text) == "/chatid":
        # Works for you in ANY chat, so you can find a group's ID while setting it up.
        # "TELEGRAM_GROUP_ID=…" is exactly what goes into Railway (and keeps the minus sign in place).
        await telegram.send_message(
            f"رقم هذي المحادثة:\nTELEGRAM_GROUP_ID={chat_id}",
            reply_to=message.get("message_id"),
            thread_id=message.get("message_thread_id") if message.get("is_topic_message") else None,
            chat_id=chat_id,
        )
        return

    console_chat = getattr(telegram, "chat_id", telegram.owner_id)
    if chat_id != console_chat or not _is_team(state, sender):
        # This log line is also how you find your own ID when setting up (docs/TELEGRAM_SETUP.md).
        logger.warning(
            "Ignored Telegram message from user id %s in chat %s (only you and your team, in the console chat)",
            sender, chat_id,
        )
        return
    if not any(key in message for key in OWNER_CONTENT_KEYS):
        return  # a service message (topic created or renamed, someone joined...)

    message_id = message.get("message_id")
    # A photo or file for a customer? Its caption is sent with it.
    file = _owner_file(message)
    text = ((message.get("caption") if file else message.get("text")) or "").strip()
    # Typed inside a topic? Then it's about that topic's customer.
    thread_id = message.get("message_thread_id") if message.get("is_topic_message") else None
    thread_phone = state.db.phone_for_thread(thread_id) if thread_id else None

    if _is_listings_file(message) and not thread_phone and not message.get("reply_to_message"):
        # Your listings file, sent in General. (In a customer's topic, files go to the customer.)
        await _load_listings(state, message, thread_id)
        return

    if not file and text.startswith("/"):
        await _handle_command(text, state, message_id, thread_id, thread_phone, sender, message)
    elif thread_phone:
        # (Inside a topic every message technically "replies" to the topic's first message,
        # so the topic decides who it's for, not the Reply.)
        await _relay_to_customer(state, thread_phone, text, message_id, thread_id, file, sender)
    elif message.get("reply_to_message"):
        await _relay_reply(message, text, state, thread_id, file, sender)
    elif thread_id:
        await _say(state, "هذا الموضوع مو مربوط بعميل. اكتب داخل موضوع العميل عشان ترد عليه. /help للأوامر.",
                   reply_to=message_id, thread_id=thread_id)
    else:
        await _say(state, "للرد على عميل: اكتب داخل الموضوع الخاص فيه أو سوّ رد على رسالته. /help للأوامر.",
                   reply_to=message_id)


def _is_listings_file(message: dict) -> bool:
    document = message.get("document") or {}
    name = (document.get("file_name") or "").lower()
    mime = document.get("mime_type") or ""
    return name.endswith(".xlsx") or mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _load_listings(state, message: dict, thread_id: int | None) -> None:
    """Your Excel file from Telegram replaces the listings (only if it can be read)."""
    message_id, document = message.get("message_id"), message.get("document") or {}

    async def answer(text: str) -> None:
        await _say(state, text, reply_to=message_id, thread_id=thread_id)

    store = getattr(state, "properties", None)
    if store is None:
        await answer("❌ تحميل العقارات غير متاح.")
        return
    try:
        media = await state.telegram.download_file(document.get("file_id"), document.get("mime_type"),
                                                   document.get("file_name"))
    except MediaTooBig as exc:
        await answer(f"❌ الملف كبير ({_size(exc.size)}؛ الحد الأقصى {_size(MAX_MEDIA_BYTES)}). القائمة القديمة باقية.")
        return
    if media is None:
        await answer("❌ ما قدرت آخذ الملف من تيليجرام. جرّب مرة ثانية. القائمة القديمة باقية.")
        return
    path = listings_file_for(state.settings.db_path)
    try:
        # Reading and saving take a moment: done off the main loop.
        result = await asyncio.to_thread(store.replace_with_excel, media.content, path)
    except ExcelFormatError:
        tabs = "، ".join(tab.name for tab in store.excel_config.tabs)
        await answer(f"❌ ما قدرت أقرأ الملف. لازم يكون ملف إكسل فيه تبويبات العروض: {tabs}. القائمة القديمة باقية.")
        return
    except ValueError:
        await answer("❌ الملف ما فيه عقارات صالحة، فخليت القائمة القديمة.")
        return

    available = sum(p.is_available for p in result.properties)
    lines = [f"✅ تم تحميل العقارات: {len(result.properties)} ({available} متاح)"]
    per_tab: dict[str, int] = {}
    for listing in result.properties:
        per_tab[listing.source_tab] = per_tab.get(listing.source_tab, 0) + 1
    lines += [f"• {tab}: {count}" for tab, count in per_tab.items()]
    lines += [f"⚠️ ما لقيت تبويب: {w.removeprefix('Tab not found: ')}" for w in result.warnings
              if w.startswith("Tab not found: ")]
    if result.skipped:
        lines.append(f"صفوف متجاهلة: {len(result.skipped)} (فاضية أو بدون نوع وحي)")
    logger.info("Listings replaced from an Excel file: %s properties (%s available)", len(result.properties), available)
    await answer("\n".join(lines))


async def _handle_command(
    text: str,
    state,
    message_id: int | None,
    thread_id: int | None = None,
    thread_phone: str | None = None,
    sender_id: int | None = None,
    message: dict | None = None,
) -> None:
    command, _, arg = text.partition(" ")
    command = command.split("@")[0].lower()  # "/status@frame_bot" -> "/status"
    db = state.db

    async def answer(reply: str, **kwargs) -> None:
        # General answers go back where the command was typed (a topic or the main chat).
        await _say(state, reply, thread_id=thread_id, **kwargs)

    if command in ("/human", "/bot"):
        phone = takeover.normalize_phone(arg) or thread_phone
        if not phone:
            await answer(f"اكتب رقم العميل، مثلاً: {command} 966512345678", reply_to=message_id)
            return
        async with state.chat_locks[phone]:  # wait if the bot is replying to them right now
            if command == "/human":
                db.take_over(phone)
                await refresh_thread_title(state, phone)
                await _say(state, _takeover_text(state, phone), phone=phone)
            else:
                db.hand_back(phone)
                await refresh_thread_title(state, phone)
                await _say(state, f"🤖 البوت رجع يرد على {_display(phone)}.", phone=phone)
    elif command == "/status":
        # Re-check whether topics were switched on (no restart needed).
        if state.telegram is not None and hasattr(state.telegram, "refresh_topics_enabled"):
            await state.telegram.refresh_topics_enabled()
        await answer(_status_text(state))
    elif command == "/pause":
        db.set_paused(True)
        await answer("⏸ تم الإيقاف: البوت ما يرد على أحد، والرسائل الجديدة توصلك هنا. /resume للتشغيل.")
    elif command == "/resume":
        db.set_paused(False)
        await answer("▶️ تم التشغيل: البوت يرد من جديد (إلا المحادثات اللي استلمتها).")
    elif command == "/leads":
        await answer(_leads_text(state))
    elif command == "/lead":
        phone = takeover.normalize_phone(arg) or thread_phone
        if not phone:
            await answer("اكتب رقم العميل، مثلاً: /lead 966512345678", reply_to=message_id)
            return
        await _say(state, _lead_text(state, phone), phone=phone)
    elif command == "/forget":
        await _forget(arg, state, message_id, thread_id, thread_phone)
    elif command == "/staff":
        await answer(_staff_command(arg, state, sender_id, message or {}), reply_to=message_id)
    elif command in ("/help", "/start"):
        await answer(HELP)
    else:
        await answer(f"أمر غير معروف: {command}\n\n{HELP}", reply_to=message_id)


def _staff_command(arg: str, state, sender_id: int | None, message: dict) -> str:
    """/staff: list the team. /staff إضافة <name> (as a Reply to the employee's message) and
    /staff حذف <name or ID>: only you, the owner."""
    words = arg.split(maxsplit=1)
    action = words[0].lower() if words else ""
    rest = words[1].strip() if len(words) > 1 else ""
    team = _team(state)
    owner_id = state.telegram.owner_id

    if action in ADD_WORDS or action in REMOVE_WORDS:
        if sender_id != owner_id:
            return "إضافة وحذف الموظفين للمالك بس."
        if action in ADD_WORDS:
            employee = (message.get("reply_to_message") or {}).get("from") or {}
            if not employee.get("id") or employee.get("is_bot") or employee["id"] == owner_id:
                return ("لإضافة موظف: خلّه يرسل أي رسالة في المجموعة، "
                        "ثم سوّ رد على رسالته واكتب: /staff إضافة <الاسم>")
            name = rest or employee.get("first_name") or str(employee["id"])
            team[employee["id"]] = name
            _save_team(state, team)
            logger.info("Team member added (Telegram user id %s)", employee["id"])
            return f"✅ {name} صار من الفريق. يقدر يرد على العملاء ويستخدم كل الأوامر."
        match = next((uid for uid, name in team.items() if rest in (name, str(uid))), None)
        if match is None:
            return f"ما لقيت «{rest}» في الفريق. /staff لعرض الفريق."
        name = team.pop(match)
        _save_team(state, team)
        logger.info("Team member removed (Telegram user id %s)", match)
        return f"🗑 {name} ما عاد من الفريق."

    if not team:
        return ("ما فيه موظفين في الفريق للحين.\n"
                "لإضافة موظف: خلّه يرسل أي رسالة في المجموعة، ثم سوّ رد على رسالته واكتب: /staff إضافة <الاسم>")
    lines = [f"👥 الفريق ({len(team)}):"]
    lines += [f"• {name} ({uid})" for uid, name in team.items()]
    lines.append("لحذف موظف: /staff حذف <الاسم>")
    return "\n".join(lines)


def _ago(iso_time: str | None) -> str:
    """"منذ 2س 5د" / "قبل شوي"."""
    if not iso_time:
        return "?"
    text = _duration(datetime.now(UTC) - datetime.fromisoformat(iso_time))
    return "قبل شوي" if text == "الآن" else f"منذ {text}"


def _leads_text(state) -> str:
    leads = state.db.recent_leads(limit=10)
    if not leads:
        return "ما فيه عملاء محتملين محفوظين للحين."
    lines = [f"📋 آخر العملاء المحتملين ({len(leads)}):"]
    for lead in leads:
        name = f" {lead['name']}" if lead.get("name") else ""
        details = _lead_line(lead) or "ما فيه تفاصيل للحين"
        lines.append(f"• {_display(lead['phone'])}{name}: {details} ({_ago(lead['updated_at'])})")
    lines.append("\nالتفاصيل كاملة: /lead <الرقم>")
    return "\n".join(lines)


def _lead_text(state, phone: str) -> str:
    lead = state.db.get_lead(phone)
    if not lead:
        return f"ما فيه بيانات محفوظة للعميل {_display(phone)}."
    lines = [f"📋 بيانات العميل {_display(phone)} (آخر تحديث {_ago(lead['updated_at'])})"]
    lines += [f"{LEAD_LABELS[key]}: {_lead_value(key, lead[key])}" for key in LEAD_FIELDS if lead.get(key)]
    lines.append("↩️ رد على هذي الرسالة (أو اكتب في موضوعه) للرد على العميل.")
    return "\n".join(lines)


async def _forget(
    arg: str, state, message_id: int | None, thread_id: int | None = None, thread_phone: str | None = None
) -> None:
    """/forget <phone> shows what will be deleted; /forget <phone> تأكيد (or confirm) deletes it.

    Inside a customer's topic the number can be left out: /forget, then /forget تأكيد.
    """
    words = arg.split()
    confirmed = bool(words) and words[-1].lower() in CONFIRM_WORDS
    number_words = words[:-1] if confirmed else words
    phone = takeover.normalize_phone(" ".join(number_words)) if number_words else thread_phone
    if not phone:
        await _say(state, "اكتب رقم العميل، مثلاً: /forget 966512345678", reply_to=message_id, thread_id=thread_id)
        return

    db = state.db
    counts = db.customer_data_counts(phone)
    if not any(counts.values()):
        await _say(state, f"ما فيه شي محفوظ للعميل {_display(phone)}.", reply_to=message_id, thread_id=thread_id)
        return
    summary = f"{counts['messages']} رسالة، {'بيانات العميل، ' if counts['lead'] else ''}المحادثة وروابط الردود"
    if counts.get("telegram_thread"):
        summary += "، وموضوعه في تيليجرام بكل رسائله"
    if not confirmed:
        again = "/forget تأكيد" if thread_phone == phone and not number_words else f"/forget {phone} تأكيد"
        await _say(
            state,
            f"⚠️ هذا يحذف نهائياً كل شي محفوظ عن {_display(phone)}: {summary}.\nللمتابعة أرسل:\n{again}",
            reply_to=message_id,
            thread_id=thread_id,
        )
        return

    customer_thread_id = db.get_thread(phone)
    async with state.chat_locks[phone]:
        deleted = db.forget_customer(phone)
    thread_deleted = False
    if customer_thread_id and _threads_on(state):
        thread_deleted = await state.telegram.delete_forum_topic(customer_thread_id)
    logger.info("Deleted all stored data for %s (%s messages)", mask_phone(phone), deleted["messages"])
    telegram_note = (
        "وانحذف موضوعه في تيليجرام بعد."
        if thread_deleted
        else "التطبيق ما يقدر يحذف الرسائل الموجودة في تيليجرام: احذف رسائله هنا يدوياً (اضغط مطولاً ← حذف)."
    )
    # If we were inside the deleted topic, answer in the main chat.
    answer_thread = None if thread_id == customer_thread_id else thread_id
    await _say(
        state,
        f"🗑 انحذف كل شي محفوظ عن {_display(phone)} ({deleted['messages']} رسالة).\n"
        f"{telegram_note} إذا راسل مرة ثانية تبدأ محادثة جديدة.",
        reply_to=None if answer_thread is None and thread_id else message_id,
        thread_id=answer_thread,
    )


def _takeover_text(state, phone: str) -> str:
    hours = state.settings.human_timeout_hours
    lines = [
        f"✋ استلمت محادثة {_display(phone)}. البوت ساكت فيها.",
        f"ترجع للبوت بعد {hours:g} ساعة بدون رسالة منك، أو بالأمر /bot {phone}.",
    ]
    recent = state.db.get_recent_messages(phone, limit=5)
    if recent:
        lines.append("\nآخر الرسائل:")
        for m in recent:
            content = m.text if m.type == "text" and m.text else f"[{type_name(m.type)}] {m.text or ''}".rstrip()
            lines.append(f"{MESSAGE_LABELS.get(m.direction, m.direction)}: {content}")
    lines.append("\n↩️ اكتب في هذا الموضوع (أو سوّ رد على هذي الرسالة) للرد على العميل.")
    return "\n".join(lines)


def _status_text(state) -> str:
    db, settings = state.db, state.settings
    now = datetime.now(UTC)
    lines = ["⏸ البوت موقوف (/resume)" if db.is_paused() else "▶️ البوت شغّال"]
    store = getattr(state, "properties", None)
    if store is not None and store.properties:
        available = sum(p.is_available for p in store.properties)
        source = "ملف الإكسل" if store.source == "excel" else "جوجل شيت"
        lines.append(f"🏠 العقارات: {len(store.properties)} ({available} متاح) من {source}، "
                     f"آخر تحميل {_ago(store.last_refresh_at)}")
    elif store is not None:
        lines.append("🏠 العقارات: ما فيه قائمة. أرسل ملف الإكسل هنا في الموضوع العام.")
    where = "في المجموعة" if _in_group(state) else "في محادثتك مع البوت"
    if _threads_on(state):
        mirror = "مفعّلة" if getattr(settings, "telegram_live_mirror", True) else "موقفة"
        lines.append(f"🧵 موضوع لكل عميل: مفعّل ({where}؛ النسخة المباشرة من المحادثات: {mirror})")
    elif _in_group(state):
        lines.append("🧵 موضوع لكل عميل: غير مفعّل (فعّل المواضيع في إعدادات المجموعة، وخلّ البوت مشرف)")
    else:
        lines.append("🧵 موضوع لكل عميل: غير مفعّل (فعّل المواضيع للبوت من @BotFather)")
    if settings.availability_mode == "schedule":
        busy = takeover.in_work_hours(now, settings.work_hours)
        lines.append(
            f"🕘 وضع الدوام: أنت ترد خلال {settings.work_hours} (بتوقيت الرياض). "
            f"الحين: {'أنت' if busy else 'البوت'}."
        )
    else:
        lines.append("🕘 الوضع: دائماً (البوت يرد في أي وقت)")

    human = db.list_chats_by_status("human")
    if not human:
        lines.append("\nما فيه محادثات مستلمة.")
    else:
        lines.append(f"\nالمحادثات اللي استلمتها ({len(human)}):")
        timeout = timedelta(hours=settings.human_timeout_hours)
        for chat in human:
            if chat.last_owner_message_at:
                since = now - datetime.fromisoformat(chat.last_owner_message_at)
                left = _duration(timeout - since)
                lines.append(f"• {_display(chat.phone)}: ترجع للبوت بعد {left}")
            else:
                lines.append(f"• {_display(chat.phone)}: ترجع للبوت في الفحص الجاي")
    return "\n".join(lines)


@dataclass(frozen=True)
class OwnerFile:
    """A photo or file you sent to the bot, to pass on to a customer."""

    file_id: str
    kind: str  # how WhatsApp sends it: "image" or "document"
    mime_type: str
    filename: str | None = None


def _owner_file(message: dict) -> OwnerFile | None:
    """The photo or file (document) in your Telegram message, if any."""
    photos = [p for p in message.get("photo") or [] if p.get("file_id")]
    if photos:
        # Telegram offers several sizes: take the biggest one WhatsApp accepts as a photo.
        fitting = [p for p in photos if (p.get("file_size") or 0) <= WHATSAPP_MAX_IMAGE_BYTES] or photos
        best = max(fitting, key=lambda p: (p.get("width") or 0) * (p.get("height") or 0))
        return OwnerFile(best["file_id"], "image", "image/jpeg")
    document = message.get("document") or {}
    if document.get("file_id"):
        mime = base_mime(document.get("mime_type"))
        small_image = mime in ("image/jpeg", "image/png") and (
            (document.get("file_size") or 0) <= WHATSAPP_MAX_IMAGE_BYTES
        )
        kind = "image" if small_image else "document"
        return OwnerFile(document["file_id"], kind, mime, document.get("file_name"))
    return None


async def _upload_owner_file(state, phone: str, file: OwnerFile, message_id: int | None) -> tuple[str, str] | None:
    """Fetch your photo/file from Telegram and upload it to WhatsApp.

    Returns (WhatsApp media ID, file name), or None after telling you why it failed.
    """
    try:
        media = await state.telegram.download_file(file.file_id, file.mime_type, file.filename)
        problem = None if media else "❌ ما قدرت آخذ الملف من تيليجرام، وما انرسل شي. جرّب مرة ثانية."
    except MediaTooBig as exc:
        media = None
        problem = f"❌ الملف كبير ({_size(exc.size)}؛ الحد الأقصى {_size(MAX_MEDIA_BYTES)})، وما انرسل شي."
    if media is not None:
        media_id = await state.wa.upload_media(media)
        if media_id:
            return media_id, media.filename
        problem = ("❌ واتساب ما قبل هذا الملف، وما انرسل شي. الصور: JPG أو PNG لين 5 ميجا. "
                   "الملفات: PDF أو Word أو Excel أو PowerPoint أو TXT لين 20 ميجا.")
    await _say(state, problem, reply_to=message_id, phone=phone)
    return None


async def _relay_reply(
    message: dict,
    text: str,
    state,
    thread_id: int | None = None,
    file: OwnerFile | None = None,
    sender_id: int | None = None,
) -> None:
    """You replied to a forwarded message: send your text (or photo/file) to that customer on WhatsApp."""
    message_id = message.get("message_id")
    replied_to = (message.get("reply_to_message") or {}).get("message_id")
    phone = state.db.phone_for_telegram_message(replied_to) if replied_to else None
    if not phone:
        await _say(
            state,
            "ما عرفت هذي الرسالة لأي عميل. سوّ رد على رسالة عميل محوّلة، أو استخدم /human <الرقم> أول.",
            reply_to=message_id,
            thread_id=thread_id,
        )
        return
    await _relay_to_customer(state, phone, text, message_id, thread_id, file, sender_id)


async def _relay_to_customer(
    state,
    phone: str,
    text: str,
    message_id: int | None,
    thread_id: int | None = None,
    file: OwnerFile | None = None,
    sender_id: int | None = None,
) -> None:
    """Send your (or an employee's) text, or photo/file with `text` as its caption, to the
    customer on WhatsApp, exactly as written, and take the chat over."""
    if not text and not file:
        await _say(state, ONLY_TEXT_AND_FILES, reply_to=message_id, phone=phone)
        return
    uploaded = None
    if file:
        # Fetched and uploaded before taking the chat lock: this can take a few seconds.
        uploaded = await _upload_owner_file(state, phone, file, message_id)
        if uploaded is None:
            return

    db = state.db
    async with state.chat_locks[phone]:
        chat = db.get_chat(phone)
        was_human = bool(chat and chat.status == "human")
        if uploaded:
            media_id, filename = uploaded
            wa_message_id = await state.wa.send_media(phone, file.kind, media_id, caption=text, filename=filename)
        else:
            wa_message_id = await state.wa.send_text(phone, text)
        if wa_message_id is None:
            await _say(
                state,
                f"❌ ما وصلت الرسالة للعميل {_display(phone)}. غالباً آخر رسالة منه قبل أكثر من 24 ساعة: "
                "واتساب يسمح بالرد خلال 24 ساعة من آخر رسالة للعميل بس. التفاصيل في سجل التطبيق.",
                reply_to=message_id,
                phone=phone,
            )
            return
        db.save_message(phone, "owner", file.kind if file else "text", text or None, wa_message_id)
        db.take_over(phone)  # you're talking now: bot silent, timeout clock restarts
    logger.info("Team %s (user id %s) relayed to %s", file.kind if file else "reply", sender_id, mask_phone(phone))
    if not was_human:
        await refresh_thread_title(state, phone)
    note = "" if was_human else f"\nالبوت ساكت الحين في هذي المحادثة (/bot {phone} لإرجاعها له)."
    # Only the team sees who answered; the customer just gets the message.
    employee = _team(state).get(sender_id) if sender_id is not None else None
    who = f" ({employee})" if employee else ""
    await _say(state, f"✅ انرسلت للعميل {_display(phone)}{who}.{note}", reply_to=message_id, phone=phone)
