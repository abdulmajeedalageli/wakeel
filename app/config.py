"""App configuration, loaded from environment variables / the .env file.

Every setting in .env maps to a field here (lower-case name), e.g. GEMINI_MODEL -> gemini_model.
Secrets use SecretStr so they never show up in logs or error messages;
call .get_secret_value() only at the exact point where the secret is used.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Treat "KEY=" (empty) in .env as "not set", so defaults apply.
        env_ignore_empty=True,
        extra="ignore",
    )

    # --- WhatsApp Cloud API (required) ---
    whatsapp_access_token: SecretStr
    whatsapp_phone_number_id: str
    whatsapp_app_secret: SecretStr
    whatsapp_verify_token: SecretStr
    whatsapp_api_version: str = "v26.0"

    # --- AI model (Gemini) ---
    gemini_api_key: SecretStr | None = None  # without it, customers get the fallback reply
    gemini_model: str = "gemini-3.6-flash"
    # How much the model "thinks" before answering. Lower = faster and cheaper.
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] = "low"

    # --- Google Sheets (Phase 3) ---
    google_service_account_file: str = "secrets/service-account.json"
    # Alternative for hosts without files (e.g. Railway): the key file's JSON content itself.
    # If set, it's used instead of google_service_account_file.
    google_service_account_json: SecretStr | None = None
    google_sheet_id: str | None = None
    sheet_tab_name: str = "Properties"
    sheet_refresh_minutes: int = 10

    # --- Telegram: your console for taking over chats (see docs/TELEGRAM_SETUP.md) ---
    telegram_bot_token: SecretStr | None = None
    telegram_owner_id: int | None = None  # your numeric Telegram user ID
    # Optional: a Telegram group with Topics as your console (one topic per customer).
    # Its ID starts with -100; send /chatid in the group to see it. Empty = your private chat.
    telegram_group_id: int | None = None
    # With threads on: copy every customer message + bot reply into the customer's thread.
    telegram_live_mirror: bool = True

    # --- Behaviour ---
    business_name: str = "frame"
    # always = the bot answers all "bot" chats; schedule = only OUTSIDE work_hours.
    availability_mode: Literal["always", "schedule"] = "always"
    work_hours: str = "09:00-17:00"  # Riyadh time (UTC+3), every day
    # A chat you took over returns to the bot after this long without a message from you.
    human_timeout_hours: float = Field(default=24, gt=0)
    # Relative to the folder the app runs in: ./data/bot.db locally,
    # /app/data/bot.db inside Docker (working directory /app).
    db_path: str = "data/bot.db"
    # Daily database backups (data/backups/) are kept this many days.
    backup_keep_days: int = Field(default=14, ge=1)

    log_level: str = "INFO"

    @field_validator("work_hours")
    @classmethod
    def _check_work_hours(cls, value: str) -> str:
        from app.takeover import parse_work_hours

        parse_work_hours(value)  # raises a clear error at startup if malformed
        return value

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_owner_id)


@lru_cache
def get_settings() -> Settings:
    """Load settings once and reuse them everywhere."""
    return Settings()
