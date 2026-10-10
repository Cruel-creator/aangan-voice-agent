"""Platform-neutral webhook contract. See docs/PLATFORM_API.md.

Whatever voice platform answers the phone calls these endpoints with plain JSON.
A platform-specific adapter (if the platform can't send this shape directly) is a
thin translation layer in front of this module; nothing else in the app changes.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings

SECRET_HEADER = "x-webhook-secret"


def verify_secret(request: Request) -> None:
    """`Authorization: Bearer <secret>` or `X-Webhook-Secret: <secret>`; constant-time compare."""
    expected = get_settings().tool_webhook_secret
    if not expected:
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    candidates = []
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        candidates.append(auth[7:].strip())
    if header := request.headers.get(SECRET_HEADER):
        candidates.append(header.strip())
    ok = False
    for c in candidates:
        ok |= hmac.compare_digest(c.encode(), expected.encode())
    if not ok:
        raise HTTPException(status_code=401, detail="unauthorized")


class _CallIds(BaseModel):
    call_id: str = Field(min_length=1, description="The platform's id for this phone call")
    caller_number: str | None = Field(None, description="Caller's number, E.164 if possible")


class CallContext(_CallIds):
    """Internal context passed to tool handlers. `demo` is set only by the website demo
    (never from a request body): demo calls skip calendars, Telegram and SMS."""

    demo: bool = False


class ToolRequest(_CallIds):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)


class TranscriptMessage(BaseModel):
    role: Literal["agent", "caller"]
    text: str


class EndOfCallReport(_CallIds):
    model_config = ConfigDict(extra="ignore")

    started_at: datetime | None = None
    ended_at: datetime | None = None
    ended_reason: str | None = Field(None, description="e.g. caller_hangup, agent_hangup, transferred")
    transferred: bool = False
    pickup_seconds: float | None = Field(None, description="Ring time before the call was answered")
    platform_cost_usd: float | None = None
    platform_cost_inr: float | None = None
    summary: str | None = None
    transcript: str | None = None
    messages: list[TranscriptMessage] = Field(default_factory=list)
