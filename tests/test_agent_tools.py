"""The agent's tools, and the rule that internal_notes never reaches the model."""

import json

import asyncio

from app.agent_tools import (
    AGENT_FIELDS,
    TOOL_DECLARATIONS,
    ChatTools,
    PropertyTools,
    property_for_agent,
)
from app.owner_console import ALERT_REASONS
from app.properties import PropertyStore


def all_internal_notes(store) -> list[str]:
    notes = [p.internal_notes for p in store.properties if p.internal_notes]
    # Deposit paid, owner's minimum, tenant note + 5 TEST notes (2 TEST rows are skipped).
    assert len(notes) == 8
    return notes


# --- the privacy allowlist ---

def test_internal_notes_never_in_agent_view_of_any_property(store):
    secrets = all_internal_notes(store)
    for p in store.properties:
        view = property_for_agent(p)
        assert "internal_notes" not in view
        dumped = json.dumps(view, ensure_ascii=False)
        for secret in secrets:
            assert secret not in dumped, f"{p.property_id} leaks {secret!r}"


def test_internal_notes_never_in_any_tool_result(store):
    tools = PropertyTools(store)
    secrets = all_internal_notes(store)
    results = [tools.run("search_properties", {})]
    results += [tools.run("search_properties", {"deal": d}) for d in ("sale", "rent")]
    results += [tools.run("get_property_details", {"property_id": p.property_id}) for p in store.properties]
    dumped = json.dumps(results, ensure_ascii=False)
    for secret in secrets:
        assert secret not in dumped


def test_agent_view_only_has_allowlisted_fields(store):
    view = property_for_agent(store.get("P-1001"))
    assert set(view) <= set(AGENT_FIELDS)  # empty fields are left out
    assert "row_number" not in view and "internal_notes" not in view


def test_public_notes_are_shared(store):
    assert property_for_agent(store.get("P-1001"))["notes"] == "New build, facing north"


def test_prices_never_reach_the_model(store):
    """Customers are never told prices, so the model never sees them."""
    assert "price_sar" not in AGENT_FIELDS
    prices = {str(p.price_sar) for p in store.properties}
    tools = PropertyTools(store)
    results = [tools.run("search_properties", {"deal": "sale", "max_price": 5_000_000})]
    results += [tools.run("get_property_details", {"property_id": p.property_id}) for p in store.properties]
    dumped = json.dumps(results, ensure_ascii=False)
    assert "price" not in json.dumps([r.get("properties") or r.get("property") for r in results])
    for price in prices:
        assert f'": {price},' not in dumped and f'": {price}}}' not in dumped
    assert "never state" in results[0]["rule"]


def test_budget_still_filters_without_showing_prices(store):
    cheap = PropertyTools(store).run("search_properties", {"deal": "sale", "type": "apartment", "max_price": 900000})
    assert cheap["total_matches"] > 0
    assert all(store.get(p["property_id"]).price_sar <= 900000 for p in cheap["properties"])


def test_phone_numbers_are_removed_from_what_the_model_sees(store):
    from dataclasses import replace

    listing = replace(store.get("P-1001"), notes="اتصل على 0541234567 أو ٠٥٥٩٨٧٦٥٤٣")
    assert property_for_agent(listing)["notes"] == "اتصل على أو"


# --- tool definitions ---

def test_exactly_four_tools_are_offered():
    assert [t.name for t in TOOL_DECLARATIONS] == ["search_properties", "get_property_details", "save_lead", "notify_owner"]


def test_search_tool_parameters_match_spec():
    params = TOOL_DECLARATIONS[0].parameters_json_schema["properties"]
    assert set(params) == {
        "deal", "type", "city", "district", "min_price", "max_price", "min_bedrooms", "min_area"
    }


# --- search_properties ---

def test_search_returns_counts_and_properties(store):
    result = PropertyTools(store).run("search_properties", {"deal": "rent", "type": "apartment"})
    assert result["total_matches"] == 6
    assert result["shown"] == 5
    assert all(p["deal"] == "rent" for p in result["properties"])


def test_search_converts_number_strings(store):
    tools = PropertyTools(store)
    as_text = tools.run("search_properties", {"deal": "sale", "max_price": "900,000"})
    as_number = tools.run("search_properties", {"deal": "sale", "max_price": 900000.0})
    assert as_text == as_number
    assert as_text["total_matches"] > 0


def test_search_bad_numbers_return_an_error_for_the_model(store):
    tools = PropertyTools(store)
    assert "error" in tools.run("search_properties", {"max_price": "cheap"})
    assert "error" in tools.run("search_properties", {"min_price": -5})
    assert "error" in tools.run("search_properties", {"min_bedrooms": 2.5})


def test_search_ignores_unknown_arguments(store):
    result = PropertyTools(store).run("search_properties", {"deal": "rent", "has_pool": True})
    assert "error" not in result


def test_search_with_no_match_says_not_to_invent(store):
    result = PropertyTools(store).run("search_properties", {"city": "Jeddah"})
    assert result["total_matches"] == 0
    assert "Do not invent" in result["note"]


# --- get_property_details ---

def test_details_of_available_property(store):
    result = PropertyTools(store).run("get_property_details", {"property_id": "p1023"})
    assert result["property"]["property_id"] == "P-1023"
    assert "note" not in result


def test_details_of_reserved_property_says_not_available(store):
    result = PropertyTools(store).run("get_property_details", {"property_id": "P-1004"})
    assert result["property"]["status"] == "reserved"
    assert "NOT available" in result["note"]


def test_details_unknown_or_missing_id(store):
    tools = PropertyTools(store)
    assert "error" in tools.run("get_property_details", {"property_id": "P-9999"})
    assert "error" in tools.run("get_property_details", {})
    assert "error" in tools.run("get_property_details", None)


# --- other ---

def test_unknown_tool_returns_error(store):
    assert "error" in PropertyTools(store).run("send_email", {"to": "x"})


def test_empty_store_says_data_is_unavailable(sheet_config, locations):
    empty = PropertyStore(None, sheet_config, locations)
    result = PropertyTools(empty).run("search_properties", {})
    assert "temporarily unavailable" in result["error"]


# --- save_lead and notify_owner (ChatTools) ---

CUSTOMER = "966512341234"


class FakeAlerts:
    def __init__(self, status="sent"):
        self.calls = []
        self.lead_updates = []
        self.status = status

    async def notify(self, phone, reason, summary):
        self.calls.append((phone, reason, summary))
        return self.status

    async def lead_updated(self, phone, fields):
        self.lead_updates.append((phone, sorted(fields)))


def chat_tools(store, db, alerts=None, phone=CUSTOMER):
    return ChatTools(phone, PropertyTools(store), db, alerts)


def run(tools, name, args):
    return asyncio.run(tools.run(name, args))


def test_lead_tool_parameters_match_spec():
    save_lead = {t.name: t for t in TOOL_DECLARATIONS}["save_lead"]
    assert set(save_lead.parameters_json_schema["properties"]) == {
        "name", "budget", "deal", "type", "districts", "bedrooms", "timeline", "notes"
    }
    notify = {t.name: t for t in TOOL_DECLARATIONS}["notify_owner"]
    assert notify.parameters_json_schema["properties"]["reason"]["enum"] == list(ALERT_REASONS)
    assert notify.parameters_json_schema["required"] == ["reason", "summary"]


def test_save_lead_uses_the_chats_phone_never_the_models(store, db):
    result = run(chat_tools(store, db), "save_lead", {"name": "Sara", "phone": "966500000000"})
    assert result["saved"] is True
    assert db.get_lead(CUSTOMER)["name"] == "Sara"
    assert db.get_lead("966500000000") is None


def test_save_lead_merges_joins_districts_and_caps_length(store, db):
    tools = chat_tools(store, db)
    run(tools, "save_lead", {"budget": "900 ألف", "districts": ["النرجس", " الياسمين ", ""]})
    result = run(tools, "save_lead", {"timeline": "x" * 1000, "budget": ""})
    lead = db.get_lead(CUSTOMER)
    assert lead["budget"] == "900 ألف"  # kept: a blank value never overwrites
    assert lead["districts"] == "النرجس, الياسمين"
    assert len(lead["timeline"]) == 300
    assert result["lead"]["budget"] == "900 ألف"


def test_save_lead_with_nothing_to_save_is_an_error(store, db):
    assert "error" in run(chat_tools(store, db), "save_lead", {"phone": "123", "unknown": "x"})


def test_save_lead_without_database_is_an_error(store):
    assert "error" in run(chat_tools(store, None), "save_lead", {"name": "Sara"})


def test_notify_owner_calls_alerts_with_the_chats_phone(store, db):
    alerts = FakeAlerts()
    result = run(chat_tools(store, db, alerts), "notify_owner",
                 {"reason": "viewing_request", "summary": "يبي يعاين P-1007", "phone": "966500000000"})
    assert alerts.calls == [(CUSTOMER, "viewing_request", "يبي يعاين P-1007")]
    assert result["notified"] is True
    assert "team will get back to them soon" in result["note"]


def test_notify_owner_duplicate_tells_the_model_not_to_repeat(store, db):
    result = run(chat_tools(store, db, FakeAlerts("duplicate")), "notify_owner",
                 {"reason": "negotiation", "summary": "يبي خصم"})
    assert "already told" in result["note"]


def test_notify_owner_bad_arguments(store, db):
    tools = chat_tools(store, db, FakeAlerts())
    assert "error" in run(tools, "notify_owner", {"reason": "bored", "summary": "x"})
    assert "error" in run(tools, "notify_owner", {"reason": "upset", "summary": "  "})
    assert "error" in run(tools, "notify_owner", {})


def test_notify_owner_without_telegram_still_guides_the_model(store, db):
    result = run(chat_tools(store, db, None), "notify_owner", {"reason": "wants_human", "summary": "يبي موظف"})
    assert result["notified"] is False
    assert "team will get back to them soon" in result["note"]


def test_chat_tools_still_run_property_tools_without_leaks(store, db):
    tools = chat_tools(store, db)
    result = run(tools, "get_property_details", {"property_id": "P-1002"})
    assert result["property"]["property_id"] == "P-1002"
    assert "3.9M" not in str(result)


def test_chat_tools_unknown_tool(store, db):
    assert "error" in run(chat_tools(store, db), "delete_everything", {})


def test_save_lead_tells_alerts_which_fields_changed(store, db):
    alerts = FakeAlerts()
    run(chat_tools(store, db, alerts), "save_lead", {"name": "Sara", "budget": "1M"})
    assert alerts.lead_updates == [(CUSTOMER, ["budget", "name"])]
