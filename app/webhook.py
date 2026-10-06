"""Turn Meta's nested webhook JSON into simple, flat events.

Meta's payload looks like:
    {"object": "whatsapp_business_account",
     "entry": [{"changes": [{"field": "messages",
                             "value": {"metadata": {"phone_number_id": ...},
                                       "contacts": [...],
                                       "messages": [...],     # customer messages
                                       "statuses": [...]}}]}]}  # sent/delivered/read/failed
Docs: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages

Also handled (dormant until the number is onboarded with "coexistence"): field
"smb_message_echoes", sent when YOU write from the WhatsApp Business app. It never
fires for messages the bot sends through the API.
Docs: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/smb_message_echoes
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Message types whose file we can fetch from Meta and pass on to you.
MEDIA_TYPES = ("image", "video", "audio", "document")


@dataclass(frozen=True)
class Media:
    """A photo, video, voice note or file (the file itself is fetched from Meta by its id)."""

    id: str
    mime_type: str | None = None
    caption: str | None = None
    filename: str | None = None  # documents only
    voice: bool = False  # audio recorded as a voice note


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None

    @property
    def label(self) -> str | None:
        """"name · address", or None if WhatsApp gave neither."""
        return " · ".join(part for part in (self.name, self.address) if part) or None


@dataclass(frozen=True)
class IncomingMessage:
    """A message a customer sent to us."""

    wa_message_id: str
    from_phone: str
    type: str  # "text", "image", "audio", "location", "sticker", ...
    text: str | None = None  # only set for type == "text"
    timestamp: str | None = None
    profile_name: str | None = None  # the customer's WhatsApp display name, if given
    media: Media | None = None  # image, video, audio, document
    location: Location | None = None

    @property
    def note(self) -> str | None:
        """What we store as the message text: the text, a caption, or a place name (never coordinates)."""
        if self.type == "text":
            return self.text
        if self.media:
            return self.media.caption
        if self.location:
            return self.location.label
        return None


@dataclass(frozen=True)
class StatusUpdate:
    """Delivery status of a message WE sent (sent / delivered / read / failed)."""

    wa_message_id: str
    status: str
    recipient_phone: str | None = None
    errors: list = field(default_factory=list)


@dataclass(frozen=True)
class OwnerEcho:
    """A message YOU sent from the WhatsApp Business app (coexistence only)."""

    wa_message_id: str
    to_phone: str
    type: str
    text: str | None = None


def extract_events(
    payload: dict, our_phone_number_id: str
) -> list[IncomingMessage | StatusUpdate | OwnerEcho]:
    """Return all messages, status updates and owner echoes in a webhook payload.

    Anything that isn't for our phone number, or is another webhook field, is skipped.
    """
    events: list[IncomingMessage | StatusUpdate | OwnerEcho] = []

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") not in ("messages", "smb_message_echoes"):
                logger.info("Ignoring webhook field %r", change.get("field"))
                continue

            value = change.get("value") or {}
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            if phone_number_id != our_phone_number_id:
                logger.warning("Ignoring webhook for a different phone_number_id")
                continue

            # Display names are sent separately in "contacts", keyed by wa_id.
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts") or []
            }

            for msg in value.get("messages") or []:
                msg_id, sender = msg.get("id"), msg.get("from")
                if not msg_id or not sender:
                    logger.warning("Skipping message without id or sender")
                    continue
                msg_type = msg.get("type", "unknown")
                text = (msg.get("text") or {}).get("body") if msg_type == "text" else None
                events.append(
                    IncomingMessage(
                        wa_message_id=msg_id,
                        from_phone=sender,
                        type=msg_type,
                        text=text,
                        timestamp=msg.get("timestamp"),
                        profile_name=names.get(sender),
                        media=_media(msg, msg_type),
                        location=_location(msg, msg_type),
                    )
                )

            for echo in value.get("message_echoes") or []:
                echo_id, customer = echo.get("id"), echo.get("to")
                if not echo_id or not customer:
                    continue
                echo_type = echo.get("type", "unknown")
                events.append(
                    OwnerEcho(
                        wa_message_id=echo_id,
                        to_phone=customer,
                        type=echo_type,
                        text=(echo.get("text") or {}).get("body") if echo_type == "text" else None,
                    )
                )

            for st in value.get("statuses") or []:
                if not st.get("id"):
                    continue
                events.append(
                    StatusUpdate(
                        wa_message_id=st["id"],
                        status=st.get("status", "unknown"),
                        recipient_phone=st.get("recipient_id"),
                        errors=st.get("errors") or [],
                    )
                )

    return events


def _media(msg: dict, msg_type: str) -> Media | None:
    if msg_type not in MEDIA_TYPES:
        return None
    info = msg.get(msg_type) or {}
    if not info.get("id"):
        return None
    return Media(
        id=info["id"],
        mime_type=info.get("mime_type"),
        caption=(info.get("caption") or "").strip() or None,
        filename=info.get("filename"),
        voice=bool(info.get("voice")),
    )


def _location(msg: dict, msg_type: str) -> Location | None:
    if msg_type != "location":
        return None
    info = msg.get("location") or {}
    try:
        return Location(
            latitude=float(info["latitude"]),
            longitude=float(info["longitude"]),
            name=(info.get("name") or "").strip() or None,
            address=(info.get("address") or "").strip() or None,
        )
    except (KeyError, TypeError, ValueError):
        return None
