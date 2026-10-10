"""Loads config/rules.yaml into typed models and secrets from env vars.

Price floors (rules.budget) are modelled here so the file validates, but only
app/qualify/budget.py may read them (enforced by tests/test_price_leak.py).
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "config" / "rules.yaml"
PROMPT_PATH = ROOT / "prompts" / "voice_agent_system_prompt.md"

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class OfficeHours(BaseModel):
    start: time
    end: time


class CommercialScope(BaseModel):
    max_sqft: int
    min_sqft: int
    below_min_action: Literal["human_review", "decline"]


class ScopeRules(BaseModel):
    min_engagement: str
    decline_project_types: list[str]
    decline_service_types: list[str]
    commercial: CommercialScope
    advice_signals: list[str]


class ServiceAreaRules(BaseModel):
    allow: list[str]
    deny: list[str]
    unknown_action: str
    aliases: dict[str, str] = Field(default_factory=dict)
    fuzzy_match_ratio: float


class TimelineRules(BaseModel):
    too_soon_below_weeks: float
    tight_below_weeks: float
    month_only_dates_resolve_to: Literal["end_of_month"]
    never_decline_late_start: bool


class BudgetFloors(BaseModel):
    residential_per_sqft: int
    commercial_per_sqft: int
    per_room: int


class BudgetRules(BaseModel):
    floors_inr: BudgetFloors
    clearly_below_ratio: float


class DecisionMakerRules(BaseModel):
    never_declines_alone: bool
    note_if: list[str]
    categories: dict[str, str]


class RoutingRules(BaseModel):
    overflow_ring_seconds: int
    dropped_call_resume_minutes: int
    dropped_call_callback_after_minutes: int
    handoff_ack_reroute_hours: float
    human_review_callback: str
    urgent_escalation_callback_minutes: int
    urgent_escalation_contact: str
    max_call_minutes: int


class VipRules(BaseModel):
    signals: list[str]


class BookingRules(BaseModel):
    horizon_days: int
    slots_to_offer: int
    slot_minutes: dict[Literal["studio", "site"], int]
    min_notice_hours: int
    working_days: list[Weekday]
    site_visit_extra_days: list[Weekday]
    insert_retries: int


class Rules(BaseModel):
    timezone: str
    office_hours: OfficeHours
    scope: ScopeRules
    service_area: ServiceAreaRules
    timeline: TimelineRules
    budget: BudgetRules
    decision_maker: DecisionMakerRules
    routing: RoutingRules
    vip: VipRules
    booking: BookingRules


def load_rules(path: Path = RULES_PATH) -> Rules:
    with path.open(encoding="utf-8") as f:
        return Rules.model_validate(yaml.safe_load(f))


@lru_cache
def get_rules() -> Rules:
    return load_rules()


class Settings(BaseSettings):
    """Secrets and deployment settings. Every field is listed in .env.example."""

    # `neon link` writes .env.local; values there win over .env.
    # Blank values (e.g. fields left empty in the hosting dashboard) fall back to the defaults below.
    model_config = SettingsConfigDict(
        env_file=(ROOT / ".env", ROOT / ".env.local"), extra="ignore", env_ignore_empty=True
    )

    app_env: str = "development"
    public_base_url: str = "http://localhost:8000"

    # Voice platform → backend shared secret (tool calls + end-of-call webhook)
    tool_webhook_secret: str = ""

    # Neon Postgres: pooled URL for the app, direct URL for migrations
    database_url: str = ""
    # Neon's own name for the direct (non-pooler) string; DATABASE_URL_DIRECT also accepted.
    database_url_unpooled: str = Field(
        "", validation_alias=AliasChoices("DATABASE_URL_UNPOOLED", "DATABASE_URL_DIRECT")
    )

    # Phone number protection
    phone_hash_key: str = ""
    phone_encryption_key: str = ""  # Fernet key (urlsafe base64, 32 bytes)

    # Google Calendar (service account JSON, as a file path or the raw JSON)
    google_service_account_json: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_studio_head_chat_id: str = ""
    telegram_front_desk_chat_id: str = ""

    # Email (Resend): designer handoffs, alongside or instead of Telegram
    resend_api_key: str = ""
    email_from: str = "Aangan Studio <onboarding@resend.dev>"
    studio_head_email: str = ""

    # Vaani (phone agent): the dashboard pulls its calls through Vaani's API
    vaani_api_key: str = ""
    vaani_agent_id: str = ""  # only this agent's calls are imported (blank: all agents)

    # SMS
    sms_provider: Literal["console", "twilio"] = "console"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # Studio facts used in confirmations
    studio_address: str = ""
    portfolio_url: str = ""

    # Scheduled jobs (the website and dashboard are public by the owner's choice)
    cron_secret: str = ""

    # Cost model
    usd_inr_rate: float = 0.0
    telephony_cost_inr_per_min: float = 0.0
    fixed_monthly_cost_inr: float = 0.0

    # Website receptionist demo: Gemini or Claude. "auto" uses Gemini when its key is set,
    # otherwise Claude. Claude also powers the simulation harness.
    gemini_api_key: str = Field("", validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    anthropic_api_key: str = ""
    demo_provider: Literal["auto", "gemini", "claude"] = "auto"
    demo_model: str = ""  # blank: gemini-3.8-flash on Gemini, claude-opus-5-5 on Claude
    demo_effort: Literal["low", "medium", "high"] = "low"  # low keeps spoken replies quick
    demo_daily_turn_limit: int = 300  # visitor messages per day across the whole site
    demo_session_turn_limit: int = 30  # visitor messages per conversation

    @field_validator("gemini_api_key", "anthropic_api_key", "resend_api_key", "vaani_api_key", mode="before")
    @classmethod
    def _clean_key(cls, v: object) -> object:
        # Keys pasted or piped on Windows can carry a byte-order mark or a newline, which
        # breaks the HTTP auth header.
        return v.replace("﻿", "").strip() if isinstance(v, str) else v


@lru_cache
def get_settings() -> Settings:
    return Settings()
