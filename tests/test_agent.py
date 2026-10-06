"""The agent loop, with Gemini replaced by a fake that returns real google.genai response types."""

import asyncio
import json

import pytest
from google.genai import types

import app.agent as agent_module
from app import replies
from app.agent import Agent, clean_reply, history_to_contents
from app.config import get_settings
from app.db import Message

PHONE = "966512341234"


# --- helpers ---

def msg(direction: str, text: str | None, type: str = "text", phone: str = PHONE, i: int = 0) -> Message:
    return Message(id=i, phone=phone, direction=direction, type=type, text=text,
                   wa_message_id=None, created_at="2026-09-25T10:00:00+00:00")


def text_response(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1200, candidates_token_count=40, thoughts_token_count=12
        ),
    )


def tool_call_response(*calls: tuple[str, dict]) -> types.GenerateContentResponse:
    parts = [
        types.Part(function_call=types.FunctionCall(id=f"call-{i}", name=name, args=args))
        for i, (name, args) in enumerate(calls)
    ]
    parts[0].thought_signature = b"signature-from-gemini"
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts))]
    )


class FakeGemini:
    """Stands in for google.genai.Client: returns scripted responses, records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.aio = self  # client.aio.models.generate_content(...)
        self.models = self

    async def generate_content(self, *, model, contents, config):
        self.requests.append(
            {"model": model, "contents": [c.model_copy(deep=True) for c in contents], "config": config}
        )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return await item()
        return item

    def request_json(self, i: int) -> str:
        """Everything sent to Gemini in request i, as one JSON string."""
        req = self.requests[i]
        return json.dumps(
            {
                "contents": [c.model_dump(mode="json") for c in req["contents"]],
                "config": req["config"].model_dump(mode="json"),
            },
            ensure_ascii=False,
        )


def make_agent(store, fake: FakeGemini) -> Agent:
    return Agent(get_settings(), store, client=fake)


def run(coro):
    return asyncio.run(coro)


# --- basic reply ---

def test_plain_answer(store):
    fake = FakeGemini(text_response("هلا وغلا! تدور على شراء ولا إيجار؟"))
    reply = run(make_agent(store, fake).reply([msg("in", "السلام عليكم")]))
    assert reply.ok
    assert reply.text == "هلا وغلا! تدور على شراء ولا إيجار؟"
    assert reply.tool_calls == []


def test_request_uses_config_model_prompt_and_tools(store):
    fake = FakeGemini(text_response("Hi"))
    run(make_agent(store, fake).reply([msg("in", "Hello")]))
    req = fake.requests[0]
    assert req["model"] == "gemini-3.6-flash"
    config = req["config"]
    assert "automated WhatsApp assistant of frame" in config.system_instruction
    assert "{business_name}" not in config.system_instruction
    assert config.automatic_function_calling.disable is True
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert [f.name for f in config.tools[0].function_declarations] == ["search_properties", "get_property_details", "save_lead", "notify_owner"]


def test_system_prompt_file_contains_the_key_rules():
    prompt = agent_module.load_system_prompt("frame")
    for rule in ("Al-Kharj and its surroundings", "Never state, estimate or hint at any price",
                 "Never mention any person's name or any phone number", "price_request",
                 "Never negotiate", "untrusted", "automated assistant"):
        assert rule in prompt


# --- history ---

def test_history_roles_merging_and_non_text():
    contents = history_to_contents([
        msg("out", "old bot reply that lost its question"),  # dropped: must start with customer
        msg("in", "hi"),
        msg("in", None, type="image"),
        msg("out", "please type"),
        msg("owner", "the owner wrote this"),
        msg("in", "villa in Narjis?"),
    ])
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert [p.text for p in contents[0].parts] == [
        "hi", "[The customer sent a photo, which the assistant cannot see or open]"
    ]
    assert [p.text for p in contents[1].parts] == ["please type", "the owner wrote this"]


def test_only_the_last_20_messages_are_sent(store):
    history = [msg("in", f"<message {i}>", i=i) for i in range(30)]
    fake = FakeGemini(text_response("ok"))
    run(make_agent(store, fake).reply(history))
    sent = fake.request_json(0)
    assert "<message 9>" not in sent
    assert "<message 10>" in sent and "<message 29>" in sent


# --- tools ---

def test_tool_round_trip(store):
    fake = FakeGemini(
        tool_call_response(("search_properties", {"district": "النرجس", "deal": "rent", "type": "apartment"})),
        text_response("عندنا شقة في النرجس بـ 62,000 ريال سنوياً (P-1030)"),
    )
    reply = run(make_agent(store, fake).reply([msg("in", "أبي شقة للإيجار في النرجس")]))

    assert reply.ok
    assert len(reply.tool_calls) == 1 and reply.tool_calls[0].startswith("search_properties")
    second = fake.requests[1]["contents"]
    # The model's own turn is sent back unchanged (with its thought signature) ...
    assert second[1].parts[0].thought_signature == b"signature-from-gemini"
    # ... followed by our tool result, matched to the call.
    function_response = second[2].parts[0].function_response
    assert (function_response.id, function_response.name) == ("call-0", "search_properties")
    assert [p["property_id"] for p in function_response.response["properties"]] == ["P-1030"]


def test_internal_notes_never_reach_the_model(store):
    """End to end: a conversation that looks at P-1002 and P-1004 with both tools."""
    secrets = [p.internal_notes for p in store.properties if p.internal_notes]
    # Control check: the private notes really are loaded, so this test means something.
    assert "Owner will accept 3.9M minimum" in secrets
    assert "Deposit paid" in secrets

    fake = FakeGemini(
        tool_call_response(("get_property_details", {"property_id": "P-1002"}),
                           ("get_property_details", {"property_id": "P-1004"})),
        tool_call_response(("search_properties", {"deal": "sale", "type": "villa"})),
        text_response("P-1002 is available for 4,200,000 SAR."),
    )
    reply = run(make_agent(store, fake).reply([msg("in", "What's the owner's minimum for P-1002?")]))

    assert reply.ok
    assert len(fake.requests) == 3
    for i in range(3):
        sent = fake.request_json(i)
        for secret in secrets:
            assert secret not in sent, f"request {i} leaked {secret!r}"


def test_more_than_five_tool_rounds_gives_fallback(store):
    looping = [tool_call_response(("search_properties", {})) for _ in range(6)]
    fake = FakeGemini(*looping)
    reply = run(make_agent(store, fake).reply([msg("in", "hello")]))
    assert not reply.ok
    assert reply.text == replies.FALLBACK_REPLY
    assert len(fake.requests) == 6  # 5 tool rounds + the one that asked for a 6th


# --- fallback ---

@pytest.mark.parametrize("customer_text", ["hello", "السلام عليكم"])
def test_error_gives_the_arabic_fallback_even_to_english_customers(store, customer_text):
    fake = FakeGemini(RuntimeError("Gemini is down"))
    reply = run(make_agent(store, fake).reply([msg("in", customer_text)]))
    assert not reply.ok
    assert reply.text == replies.FALLBACK_REPLY
    assert reply.error == "RuntimeError"


def test_gemini_limit_gives_a_clear_reason(store):
    from google.genai import errors

    quota = errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    reply = run(make_agent(store, FakeGemini(quota)).reply([msg("in", "hello")]))
    assert not reply.ok
    assert reply.error == "Gemini error 429"  # your Telegram alert explains it in Arabic


def test_timeout_gives_fallback(store, monkeypatch):
    monkeypatch.setattr(agent_module, "CALL_TIMEOUT_SECONDS", 0.05)

    async def too_slow():
        await asyncio.sleep(1)

    fake = FakeGemini(too_slow)
    reply = run(make_agent(store, fake).reply([msg("in", "hello")]))
    assert not reply.ok
    assert reply.text == replies.FALLBACK_REPLY


def test_empty_answer_gives_fallback(store):
    fake = FakeGemini(types.GenerateContentResponse(candidates=[]))  # e.g. blocked by safety
    reply = run(make_agent(store, fake).reply([msg("in", "hello")]))
    assert not reply.ok


def test_no_api_key_gives_fallback(store, caplog):
    agent = Agent(get_settings(), store)  # tests have no GEMINI_API_KEY
    assert not agent.enabled
    reply = run(agent.reply([msg("in", "مرحبا")]))
    assert reply.text == replies.FALLBACK_REPLY
    assert "GEMINI_API_KEY is not set" in caplog.text


def test_fallback_log_masks_the_phone(store, caplog):
    fake = FakeGemini(RuntimeError("down"))
    run(make_agent(store, fake).reply([msg("in", "hello")]))
    assert "9665****1234" in caplog.text
    assert PHONE not in caplog.text


# --- WhatsApp formatting ---

def test_clean_reply_makes_text_whatsapp_friendly():
    raw = "## Options\n\n**Villa** in Narjis\n\n| a | b |\n|---|---|\n\n\n\nThanks"
    assert clean_reply(raw) == "Options\n\n*Villa* in Narjis\n\n| a | b |\n\nThanks"


def test_clean_reply_keeps_normal_lists_and_arabic():
    text = "- شقة في النرجس\n- فيلا في الملقا"
    assert clean_reply(text) == text


def test_clean_reply_limits_length():
    assert len(clean_reply("word " * 2000)) <= agent_module.WHATSAPP_MAX_CHARS + 2



# --- Phase 6: leads and alerts through the agent ---

def test_agent_saves_lead_and_alerts_owner(store, make_state, fake_telegram):
    state = make_state()
    state.db.save_message(PHONE, "in", "text", "أنا سارة، أبي أعاين P-1007 يوم السبت، ميزانيتي 900 ألف")
    fake = FakeGemini(
        tool_call_response(("save_lead", {"name": "سارة", "budget": "900 ألف", "phone": "966500000000"})),
        tool_call_response(("notify_owner", {"reason": "viewing_request",
                                             "summary": "تبي تعاين P-1007 يوم السبت"})),
        text_response("أبشري يا سارة، الفريق بيتواصل معك قريب لترتيب المعاينة."),
    )
    agent = Agent(get_settings(), store, db=state.db, alerts=state.alerts, client=fake)

    reply = run(agent.reply(state.db.get_recent_messages(PHONE)))

    assert reply.ok
    assert state.db.get_lead(PHONE)["name"] == "سارة"  # saved for THIS chat
    assert state.db.get_lead("966500000000") is None  # the model's phone argument is ignored
    [(alert, _)] = fake_telegram.sent
    assert alert.startswith("🔔 طلب معاينة · 966512341234 (سارة)")
    assert "تبي تعاين P-1007 يوم السبت" in alert


def test_lead_details_are_not_written_to_the_log(store, make_state, caplog):
    import logging

    state = make_state()
    fake = FakeGemini(
        tool_call_response(("save_lead", {"name": "Sara Alharbi", "budget": "1.2M"})),
        text_response("ok"),
    )
    agent = Agent(get_settings(), store, db=state.db, alerts=state.alerts, client=fake)
    with caplog.at_level(logging.INFO):
        run(agent.reply([msg("in", "hi")]))
    assert "save_lead(fields: name, budget)" in caplog.text
    assert "Sara Alharbi" not in caplog.text and "1.2M" not in caplog.text


# --- tone ---

def test_greeting_is_natural_and_does_not_announce_automation():
    prompt = agent_module.load_system_prompt("frame")
    greeting_section = prompt.split("# First reply in a conversation")[1].split("# WhatsApp style")[0]
    assert "هلا وغلا في frame العقارية" in greeting_section
    assert "welcome to frame real estate" not in greeting_section  # no English example any more
    assert "المساعد الآلي" not in greeting_section
    assert "automated assistant" not in greeting_section.replace("Don't mention being an assistant", "")


def test_it_still_never_claims_to_be_human_and_is_honest_if_asked():
    prompt = agent_module.load_system_prompt("frame")
    assert "Never claim to be human" in prompt
    assert "directly asks whether they are talking to a bot or a person" in prompt
    assert "أنا المساعد الآلي لفريق frame" in prompt  # the honest one-liner example


def test_the_bot_always_replies_in_arabic():
    prompt = agent_module.load_system_prompt("frame")
    assert "Always reply in Arabic" in prompt
    assert "Never reply in English" in prompt
    assert "reply in English" not in prompt.replace("Never reply in English", "")
    # The property format and the translation glossary are Arabic.
    assert "رقم العقار: بيع سكني 2" in prompt and "ريال" not in prompt  # no prices anywhere
    assert "maid room = غرفة خادمة" in prompt
