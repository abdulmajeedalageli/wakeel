"""The AI agent: turns a chat's recent history into a reply, using Gemini + the property tools.

How one reply is made:
    1. The chat's last 20 messages become the conversation for Gemini.
    2. Gemini either answers, or asks to run a tool (search / details).
    3. We run the tool, send the result back, and repeat (max 5 tool rounds).
    4. The answer is tidied for WhatsApp and returned.
If anything goes wrong (error, timeout, empty answer, too many rounds, no API key),
the customer gets a polite fallback reply and the problem is logged.

We use Gemini's stateless `generateContent` API: Google keeps no conversation state;
our own database is the only memory. All Gemini-specific code lives in this file.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from google import genai
from google.genai import errors, types

from app import replies
from app.agent_tools import TOOL_DECLARATIONS, ChatTools, PropertyTools
from app.config import Settings
from app.db import Database, Message
from app.logging_setup import mask_phone
from app.media import TYPE_LABELS
from app.normalize import remove_phone_numbers
from app.properties import PropertyStore

logger = logging.getLogger(__name__)

PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "system.md"

HISTORY_LIMIT = 20  # messages of the chat sent to the model
MAX_TOOL_ROUNDS = 5  # tool-call rounds per incoming message
CALL_TIMEOUT_SECONDS = 30  # one Gemini request
TOTAL_TIMEOUT_SECONDS = 60  # the whole reply, including tool rounds
# Includes the model's "thinking" tokens, so it must not be too low.
MAX_OUTPUT_TOKENS = 2048
WHATSAPP_MAX_CHARS = 4000  # WhatsApp's limit is 4096
# Tools whose arguments contain customer details: only their field names are logged.
PERSONAL_DATA_TOOLS = {"save_lead", "notify_owner"}

class AgentError(Exception):
    """A problem that means we should send the fallback reply."""


@dataclass
class AgentReply:
    text: str
    ok: bool  # False = this is the fallback reply
    tool_calls: list[str] = field(default_factory=list)  # e.g. ["search_properties(...)"], for logs
    error: str | None = None  # why the fallback was used (for the alert to you)


def load_system_prompt(business_name: str) -> str:
    """prompts/system.md with {business_name} filled in. Restart the app after editing it."""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    return text.replace("{business_name}", business_name)


def history_to_contents(history: list[Message]) -> list[types.Content]:
    """Turn stored messages (oldest first) into Gemini's user/model turns."""
    contents: list[types.Content] = []
    for msg in history:
        role = "user" if msg.direction == "in" else "model"  # "out" (bot) and "owner" (you)
        text = msg.text if msg.type == "text" and msg.text else describe_non_text(msg)
        if contents and contents[-1].role == role:
            contents[-1].parts.append(types.Part(text=text))  # merge back-to-back messages
        else:
            contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
    # The conversation must start with the customer (older messages may have been cut off).
    while contents and contents[0].role == "model":
        contents.pop(0)
    return contents


def describe_non_text(msg: Message) -> str:
    """How a photo, voice note, location... appears in the conversation the model sees."""
    label = TYPE_LABELS.get(msg.type, f"{msg.type} message")
    if msg.direction != "in":
        text = f"[The team sent the customer a {label}]"
    elif msg.type == "location":
        # Only the place name / address is stored, never the coordinates.
        return f"[The customer shared a location: {msg.text}]" if msg.text else "[The customer shared a location]"
    else:
        text = f"[The customer sent a {label}, which the assistant cannot see or open]"
    return f'{text} Caption: "{msg.text}"' if msg.text else text


def clean_reply(text: str) -> str:
    """Make the model's text WhatsApp-friendly, whatever it produced.

    Also removes any phone number: the bot never shares numbers (a business rule).
    """
    text = (remove_phone_numbers(text) or "").strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)  # **bold** -> *bold* (WhatsApp style)
    text = re.sub(r"^#{1,6}[ \t]*", "", text, flags=re.MULTILINE)  # markdown headers
    # Table separator / horizontal rule lines like "|---|---|" or "---".
    text = re.sub(r"^[ \t]*\|?[ \t]*:?-{3,}[-:| \t]*$\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > WHATSAPP_MAX_CHARS:
        text = text[:WHATSAPP_MAX_CHARS].rsplit(" ", 1)[0] + " …"
    return text


class Agent:
    def __init__(
        self,
        settings: Settings,
        store: PropertyStore,
        db: Database | None = None,
        alerts=None,
        client=None,
    ):
        """`db` stores leads; `alerts` (owner_console.OwnerAlerts) sends you Telegram alerts.

        `client` is only passed by tests (a fake). Normally it's created from GEMINI_API_KEY.
        """
        if client is None and settings.gemini_api_key:
            client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        self._client = client
        self.model = settings.gemini_model
        self.property_tools = PropertyTools(store)
        self.db = db
        self.alerts = alerts
        # Built once and identical for every request, so Gemini can cache it
        # (automatic caching applies when this shared beginning is big enough).
        self.config = types.GenerateContentConfig(
            system_instruction=load_system_prompt(settings.business_name),
            tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
            # We run the tools ourselves (the SDK must not call Python functions on its own).
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_level=settings.gemini_thinking_level.upper()),
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def reply(self, history: list[Message]) -> AgentReply:
        """Reply to the last customer message in `history` (one chat, oldest first). Never raises."""
        phone = history[-1].phone if history else None
        tool_calls: list[str] = []
        try:
            if not self.enabled:
                raise AgentError("GEMINI_API_KEY is not set")
            # The tools act on THIS chat only: the phone number comes from the chat itself.
            tools = ChatTools(phone, self.property_tools, self.db, self.alerts)
            text = await asyncio.wait_for(self._run(history, tool_calls, tools), TOTAL_TIMEOUT_SECONDS)
            return AgentReply(text=text, ok=True, tool_calls=tool_calls)
        except asyncio.TimeoutError:
            reason = "timed out"
        except AgentError as exc:
            reason = str(exc)
        except errors.APIError as exc:
            # E.g. 429 = limit reached (the free tier allows few requests a day). Your alert
            # explains the code in plain words (owner_console._error_text).
            logger.error("Gemini error %s for %s: %s", exc.code, mask_phone(phone), str(exc)[:300])
            reason = f"Gemini error {exc.code}"
        except Exception as exc:
            logger.exception("Agent error for %s", mask_phone(phone))
            reason = f"{type(exc).__name__}"
        # main.py then alerts you on Telegram (owner_console.OwnerAlerts.bot_error).
        logger.error("Agent used the fallback reply for %s: %s", mask_phone(phone), reason)
        return AgentReply(text=replies.FALLBACK_REPLY, ok=False, tool_calls=tool_calls, error=reason)

    async def _run(self, history: list[Message], tool_calls: list[str], tools: ChatTools) -> str:
        contents = history_to_contents(history[-HISTORY_LIMIT:])
        if not contents:
            raise AgentError("no customer message to answer")

        for round_number in range(MAX_TOOL_ROUNDS + 1):
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self.model, contents=contents, config=self.config
                ),
                CALL_TIMEOUT_SECONDS,
            )
            self._log_usage(response)

            calls = response.function_calls
            if not calls:
                text = clean_reply(response.text or "")
                if not text:
                    raise AgentError("the model returned an empty answer")
                return text
            if round_number == MAX_TOOL_ROUNDS:
                raise AgentError(f"more than {MAX_TOOL_ROUNDS} tool rounds")

            # Keep the model's turn exactly as received: it carries "thought signatures"
            # that Gemini needs back to continue its reasoning.
            contents.append(response.candidates[0].content)
            results = []
            for call in calls:
                tool_calls.append(f"{call.name}({call.args or {}})")
                if call.name in PERSONAL_DATA_TOOLS:
                    logger.info("Tool call: %s(fields: %s)", call.name, ", ".join(call.args or {}))
                else:
                    logger.info("Tool call: %s(%s)", call.name, call.args or {})
                result = await tools.run(call.name, call.args)
                results.append(
                    types.Part(
                        function_response=types.FunctionResponse(id=call.id, name=call.name, response=result)
                    )
                )
            contents.append(types.Content(role="user", parts=results))

        raise AgentError("unreachable")  # the loop always returns or raises

    @staticmethod
    def _log_usage(response: types.GenerateContentResponse) -> None:
        usage = response.usage_metadata
        if usage:
            logger.info(
                "Gemini tokens: prompt=%s (cached=%s) output=%s thinking=%s",
                usage.prompt_token_count,
                usage.cached_content_token_count or 0,
                usage.candidates_token_count,
                usage.thoughts_token_count or 0,
            )
