"""The tools the AI agent may use, and the ONLY path by which property data reaches the model.

Privacy rules: the model only ever sees what `property_for_agent()` returns. That dict is
built from AGENT_FIELDS, an explicit allowlist. `internal_notes`, and any column added to
the sheet in the future, are never sent unless someone adds them to the list on purpose.
Never pass `Property.to_dict()` (or the Property itself) to the model.
Business rule: customers are NEVER told a price, a person's name or a phone
number. So the price is not in the allowlist (it's only used to filter by budget), owner
names/phones/notes are never loaded from the Excel file, and phone numbers are removed from
every text that goes to the model.

The agent has exactly four tools:
    search_properties, get_property_details  -> PropertyTools (the sheet)
    save_lead, notify_owner                  -> ChatTools (this chat's lead, alerts to you)
The customer's phone number always comes from the chat itself, NEVER from the model.
"""

import logging

from google.genai import types

from app.db import LEAD_FIELDS, Database
from app.logging_setup import mask_phone
from app.normalize import parse_number, remove_phone_numbers
from app.owner_console import ALERT_REASONS
from app.properties import PropertyStore
from app.sheet import Property

logger = logging.getLogger(__name__)

# The ONLY property fields the model may see. No price (business rule) and no
# internal_notes: both are deliberately NOT here.
AGENT_FIELDS = (
    "property_id",
    "type",
    "type_text",
    "category",
    "deal",
    "city",
    "district",
    "facade",
    "area_sqm",
    "area_text",
    "age",
    "terms",
    "bedrooms",
    "bathrooms",
    "features",
    "status",
    "ad_license_no",
    "photos_link",
    "notes",  # PUBLIC notes of the Google Sheet only (the Excel notes are never loaded)
)


def property_for_agent(p: Property) -> dict:
    """What the model is allowed to know about one property (empty fields left out)."""
    data = {}
    for name in AGENT_FIELDS:
        value = getattr(p, name)
        if isinstance(value, str):
            value = remove_phone_numbers(value)  # belt and braces: never a phone number
        if value not in (None, ""):
            data[name] = value
    return data


# Added to every search/details result, so the model is reminded where it matters.
PRICE_RULE = (
    "Prices are private: never state, estimate or hint at a price or rent. If the customer asks, "
    "say the team will share it and call notify_owner with reason price_request."
)


# Standard property types (the Excel file's words are mapped to these in config/excel.json).
PROPERTY_TYPES = {
    "apartment": "شقة", "villa": "فيلا", "house": "بيت", "floor": "دور", "land": "أرض",
    "farm": "مزرعة", "rest_house": "استراحة", "building": "عمارة", "labor_housing": "سكن عمال",
    "workshop": "ورشة", "warehouse": "مستودع", "office": "مكتب", "shop": "محل",
}


SEARCH_PROPERTIES = types.FunctionDeclaration(
    name="search_properties",
    description=(
        "Search the company's current listings. Returns up to 5 AVAILABLE properties, "
        "closest matches first, plus the total number of matches. Leave out any filter "
        "the customer hasn't specified. Results never include prices: a budget is only used "
        "to filter (rent budgets are per year, in SAR)."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "deal": {"type": "string", "enum": ["sale", "rent"], "description": "sale = buy; rent = annual rental"},
            "type": {
                "type": "string",
                "enum": list(PROPERTY_TYPES),
                "description": "Property type: " + ", ".join(f"{k} = {v}" for k, v in PROPERTY_TYPES.items()),
            },
            "city": {"type": "string", "description": "City. The company covers Al-Kharj and its surroundings."},
            "district": {
                "type": "string",
                "description": "One district name, in Arabic or English as the customer wrote it, e.g. 'Al-Narjis' or 'النرجس'",
            },
            "min_price": {"type": "number", "description": "Customer's minimum budget in SAR (rent: per year). Filter only."},
            "max_price": {"type": "number", "description": "Customer's maximum budget in SAR (rent: per year). Filter only."},
            "min_bedrooms": {"type": "integer", "description": "Minimum number of bedrooms"},
            "min_area": {"type": "number", "description": "Minimum area in square metres"},
        },
    },
)

GET_PROPERTY_DETAILS = types.FunctionDeclaration(
    name="get_property_details",
    description=(
        "Get full details of one property by its ID (e.g. 'بيع سكني 12' or 'P-1023'), whatever its status. "
        "Use it when the customer mentions a specific property."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {"property_id": {"type": "string", "description": "The property ID, e.g. 'P-1023'"}},
        "required": ["property_id"],
    },
)

SAVE_LEAD = types.FunctionDeclaration(
    name="save_lead",
    description=(
        "Save details the customer has told you about themselves and what they want. "
        "Call it whenever they share something new. Include ONLY details they actually said "
        "(never guess). Earlier saved details are kept."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The customer's name, as they gave it"},
            "budget": {"type": "string", "description": "Budget as they said it, e.g. '900 ألف' or '1.2M'"},
            "deal": {"type": "string", "enum": ["sale", "rent"], "description": "sale = buy; rent"},
            "type": {"type": "string", "enum": list(PROPERTY_TYPES)},
            "districts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Preferred districts",
            },
            "bedrooms": {"type": "string", "description": "Bedrooms wanted, e.g. '3' or '3-4'"},
            "timeline": {"type": "string", "description": "When they want to buy/move, e.g. 'within 3 months'"},
            "notes": {"type": "string", "description": "Other useful needs, e.g. 'needs parking'"},
        },
    },
)

NOTIFY_OWNER = types.FunctionDeclaration(
    name="notify_owner",
    description=(
        "Alert the team that this customer needs a person. Use it when the customer wants a "
        "viewing, wants to negotiate the price, is ready to buy or rent, asks something the "
        "listings can't answer, asks for a human, or is upset. Afterwards, tell the customer "
        "the team will get back to them soon."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "enum": list(ALERT_REASONS)},
            "summary": {
                "type": "string",
                "description": "1-3 short lines IN ARABIC for the team: what the customer wants, "
                               "which property, and any key details.",
            },
        },
        "required": ["reason", "summary"],
    },
)

TOOL_DECLARATIONS = [SEARCH_PROPERTIES, GET_PROPERTY_DETAILS, SAVE_LEAD, NOTIFY_OWNER]

MAX_LEAD_VALUE_CHARS = 300
MAX_SUMMARY_CHARS = 1000

_TEXT_ARGS = ("deal", "type", "city", "district")
_NUMBER_ARGS = ("min_price", "max_price", "min_area")

UNAVAILABLE = {
    "error": "Property data is temporarily unavailable. Tell the customer the team will follow up shortly."
}


class ToolArgumentError(ValueError):
    """The model sent an argument we can't use. The message goes back to the model."""


class PropertyTools:
    """Runs the model's tool calls against the property store."""

    def __init__(self, store: PropertyStore):
        self.store = store

    def run(self, name: str, args: dict | None) -> dict:
        """Run one tool call. Always returns a dict for the model; never raises for bad input."""
        if not self.store.properties:
            return UNAVAILABLE
        try:
            if name == "search_properties":
                return self._search(args or {})
            if name == "get_property_details":
                return self._details(args or {})
            return {"error": f"Unknown tool {name!r}. Only search_properties and get_property_details exist."}
        except ToolArgumentError as exc:
            return {"error": str(exc)}

    def _search(self, args: dict) -> dict:
        filters: dict = {}
        for key in _TEXT_ARGS:
            value = args.get(key)
            if value not in (None, ""):
                filters[key] = str(value)
        for key in (*_NUMBER_ARGS, "min_bedrooms"):
            value = args.get(key)
            if value in (None, ""):
                continue
            number = parse_number(value)
            if number is None:
                raise ToolArgumentError(f"{key} must be a positive number, got {value!r}")
            if key == "min_bedrooms" and not isinstance(number, int):
                raise ToolArgumentError(f"min_bedrooms must be a whole number, got {value!r}")
            filters[key] = number
        # Any other argument the model invents is ignored.

        result = self.store.search(**filters)
        response = {
            "total_matches": result.total_matches,
            "shown": len(result.properties),
            "properties": [property_for_agent(p) for p in result.properties],
            "rule": PRICE_RULE,
        }
        if result.total_matches == 0:
            response["note"] = (
                "No available property matches. Do not invent any. Offer to change one "
                "criterion, or say the team can follow up."
            )
        return response

    def _details(self, args: dict) -> dict:
        property_id = args.get("property_id")
        if not isinstance(property_id, str) or not property_id.strip():
            raise ToolArgumentError("property_id is required, e.g. 'P-1023'")
        prop = self.store.get(property_id)
        if prop is None:
            return {"error": f"No property with ID {property_id!r} in the current listings."}
        response = {"property": property_for_agent(prop), "rule": PRICE_RULE}
        if not prop.is_available:
            response["note"] = (
                f"This property is NOT available (status: {prop.status}). Say so honestly "
                "and offer to search for similar available properties."
            )
        return response


class ChatTools:
    """All four tools for ONE chat. Created for every reply, so the phone number is fixed."""

    def __init__(self, phone: str, property_tools: PropertyTools, db: Database | None, alerts=None):
        self.phone = phone  # from the chat, never from the model
        self.property_tools = property_tools
        self.db = db
        self.alerts = alerts  # owner_console.OwnerAlerts, or None

    async def run(self, name: str, args: dict | None) -> dict:
        """Run one tool call. Always returns a dict for the model; never raises for bad input."""
        args = args or {}
        if name in ("search_properties", "get_property_details"):
            return self.property_tools.run(name, args)
        try:
            if name == "save_lead":
                return await self._save_lead(args)
            if name == "notify_owner":
                return await self._notify_owner(args)
        except ToolArgumentError as exc:
            return {"error": str(exc)}
        return {"error": f"Unknown tool {name!r}."}

    async def _save_lead(self, args: dict) -> dict:
        if self.db is None:
            return {"error": "Details can't be saved right now. Continue helping the customer."}
        fields = {}
        for key in LEAD_FIELDS:  # anything else (like a "phone" argument) is ignored
            value = args.get(key)
            if isinstance(value, list):
                value = ", ".join(str(v).strip() for v in value if str(v).strip())
            text = str(value).strip() if value is not None else ""
            if text:
                fields[key] = text[:MAX_LEAD_VALUE_CHARS]
        if not fields:
            raise ToolArgumentError("Nothing to save: include at least one detail the customer gave.")
        lead = self.db.save_lead(self.phone, fields)
        # Log which fields changed, never their values (personal data).
        logger.info("Lead saved for %s (%s)", mask_phone(self.phone), ", ".join(fields))
        if self.alerts is not None and hasattr(self.alerts, "lead_updated"):
            await self.alerts.lead_updated(self.phone, fields)  # e.g. show the name in the thread title
        return {"saved": True, "lead": {k: lead[k] for k in LEAD_FIELDS if lead.get(k)}}

    async def _notify_owner(self, args: dict) -> dict:
        reason = args.get("reason")
        if reason not in ALERT_REASONS:
            raise ToolArgumentError(f"reason must be one of {list(ALERT_REASONS)}")
        summary = str(args.get("summary") or "").strip()
        if not summary:
            raise ToolArgumentError("summary is required: 1-3 short lines in Arabic for the team.")

        if self.alerts is None:
            status = "not_configured"
        else:
            status = await self.alerts.notify(self.phone, reason, summary[:MAX_SUMMARY_CHARS])
        if status == "duplicate":
            return {
                "notified": True,
                "note": "The team was already told about this recently. Don't repeat it; just "
                        "reassure the customer the team will get back to them soon.",
            }
        return {
            "notified": status == "sent",
            "note": "Tell the customer the team will get back to them soon. Don't promise a time.",
        }
