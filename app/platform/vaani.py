"""Vaani (app.vaanivoice.ai) custom-tool adapter.

Vaani's custom tools POST the parameters the agent extracted, and its public docs don't
pin down the exact body or which call metadata comes with it. So this adapter is
tolerant: tool arguments are taken from the body by the tool's own field names (or from
an "arguments" object if one is sent), and the call id / caller number are looked up
under the usual names in the query string, headers and body. Everything else goes
through the same neutral ToolRequest as any other platform.
"""

from __future__ import annotations

import typing
from datetime import datetime
from typing import Any

from app.platform.webhooks import ToolRequest

CALL_ID_KEYS = ("call_id", "callId", "room_name", "roomName", "session_id", "sessionId", "conversation_id")
CALLER_KEYS = (
    "caller_number",
    "callerNumber",
    "phone_number",
    "phoneNumber",
    "from_number",
    "from",
    "caller",
    "customer_number",
    "user_number",
)
NESTED_KEYS = ("call", "metadata", "context", "session", "call_details")
EMPTY = (None, "", "null", "None", "undefined")


def unfilled(value: Any) -> bool:
    """Blank, or a {{variable}} Vaani left unsubstituted."""
    return value in EMPTY or (isinstance(value, str) and "{{" in value)


def variable_report(query: dict[str, str]) -> dict[str, str]:
    """For logs: whether each query parameter was filled in, without its value."""
    return {k: ("literal" if "{{" in v else "empty" if v in EMPTY else "filled") for k, v in query.items()}


def _find(keys: tuple[str, ...], *sources: dict[str, Any]) -> str | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if not unfilled(value) and not isinstance(value, dict | list):
                return str(value)
        for nest in NESTED_KEYS:
            if isinstance(source.get(nest), dict) and (found := _find(keys, source[nest])):
                return found
    return None


def _is_list(annotation: Any) -> bool:
    return typing.get_origin(annotation) is list or any(
        typing.get_origin(a) is list for a in typing.get_args(annotation)
    )


def arguments(model, body: dict[str, Any]) -> dict[str, Any]:
    """The tool's own fields from the body. Blank values are dropped (Vaani may send ""
    for parameters the caller never mentioned) and a comma list becomes a real list."""
    source = body["arguments"] if isinstance(body.get("arguments"), dict) else body
    out = {}
    for name, field in model.model_fields.items():
        value = source.get(name)
        if unfilled(value) or value == []:
            continue
        if _is_list(field.annotation) and isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        out[name] = value
    return out


def to_tool_request(
    model, body: dict[str, Any], query: dict[str, str], headers: dict[str, str], now: datetime
) -> ToolRequest:
    header_ids = {k.lower().removeprefix("x-").replace("-", "_"): v for k, v in headers.items()}
    caller = _find(CALLER_KEYS, query, body, header_ids)
    call_id = _find(CALL_ID_KEYS, query, body, header_ids)
    if call_id is None:
        # No id from Vaani: group this caller's tool calls within the hour. Better than one
        # record per tool call; replaced as soon as Vaani sends a real id.
        call_id = f"vaani-{caller or 'unknown'}-{now:%Y%m%d%H}"
    return ToolRequest(call_id=call_id, caller_number=caller, arguments=arguments(model, body))
