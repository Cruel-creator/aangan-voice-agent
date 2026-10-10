"""Export everything a voice platform needs to run the agent, platform-neutral.

    uv run python -m scripts.export_tools                 # writes agent_config.json
    uv run python -m scripts.export_tools --stdout

The file holds the system prompt, the first message, the max call length, and
each tool as a JSON-Schema function definition (the common function-calling
format) with the HTTPS endpoint to call. Paste or import these into whichever
voice platform you choose; the request/response contract is docs/PLATFORM_API.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from app.config import PROMPT_PATH, ROOT, get_rules, get_settings
from app.tools import models as m

TOOL_DESCRIPTIONS = {
    "lookup_caller": "Call FIRST on every call. Tells you if the caller is an existing client, "
    "has enquired before, had a call drop in the last few minutes, and whether "
    "the studio is open now (for transfers).",
    "check_scope": "Check whether the project is one the studio takes on. Pass the caller's own "
    "words plus property type, and service if clear.",
    "check_service_area": "Check whether the site locality is in the studio's service area.",
    "check_timeline": "Check the completion deadline. Pass when the project must be FINISHED, "
    "not the possession date. flexible=true if there is no fixed deadline.",
    "check_budget": "Only if the caller volunteered a budget. Never ask for one.",
    "get_slots": "Get 2-3 consultation slots. Read the label to the caller, keep start and "
    "designer_id for booking.",
    "book_consultation": "Book the slot the caller chose. Only tell the caller it is booked if "
    "confirmed is true; otherwise offer another slot.",
    "send_handoff": "Before ending any qualified call: send the full lead to the designer. "
    "Put anything uncertain in notes.",
    "escalate_urgent": "Existing client with a problem or complaint: escalate immediately. Do not qualify.",
    "route_human_review": "When something stays unclear after one clarifying question, or the "
    "caller wants a person after hours.",
    "log_call": "Call at the end of EVERY call with everything you learned.",
}

INPUTS = {
    "lookup_caller": m.LookupCallerIn,
    "check_scope": m.CheckScopeIn,
    "check_service_area": m.CheckServiceAreaIn,
    "check_timeline": m.CheckTimelineIn,
    "check_budget": m.CheckBudgetIn,
    "get_slots": m.GetSlotsIn,
    "book_consultation": m.BookConsultationIn,
    "send_handoff": m.SendHandoffIn,
    "escalate_urgent": m.EscalateUrgentIn,
    "route_human_review": m.RouteHumanReviewIn,
    "log_call": m.LogCallIn,
}


def simple_schema(model) -> dict:
    """Pydantic JSON schema → plain JSON Schema (no $defs, no anyOf-with-null, no titles)."""

    def clean(node):
        if isinstance(node, dict):
            if "anyOf" in node:
                options = [o for o in node["anyOf"] if o.get("type") != "null"]
                if len(options) == 1:
                    merged = {
                        **options[0],
                        **{k: v for k, v in node.items() if k not in ("anyOf", "default")},
                    }
                    return clean(merged)
            return {k: clean(v) for k, v in node.items() if k not in ("title", "default")}
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    schema = clean(model.model_json_schema())
    return {
        "type": "object",
        "properties": schema.get("properties", {}),
        "required": schema.get("required", []),
    }


def greeting() -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    match = re.search(r'GREETING\s*\n"(.+?)"', text, re.S)
    return " ".join(match.group(1).split()) if match else "Hello, thank you for calling Aangan Studio."


def tool_definitions(base_url: str) -> list[dict]:
    return [
        {
            "name": name,
            "description": TOOL_DESCRIPTIONS[name],
            "parameters": simple_schema(model),
            "url": f"{base_url}/tools/{name}",
        }
        for name, model in INPUTS.items()
    ]


def build_config(base_url: str) -> dict:
    return {
        "system_prompt": PROMPT_PATH.read_text(encoding="utf-8"),
        "first_message": greeting(),
        "languages": ["en-IN", "hi-IN"],
        "max_call_seconds": get_rules().routing.max_call_minutes * 60,
        "auth": "Send header 'Authorization: Bearer <TOOL_WEBHOOK_SECRET>' on every request",
        "tool_request_shape": {"call_id": "<platform call id>", "caller_number": "+91...", "arguments": {}},
        "tools": tool_definitions(base_url),
        "end_of_call_webhook": f"{base_url}/webhooks/end-of-call",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "agent_config.json"))
    a = ap.parse_args()
    config = build_config(get_settings().public_base_url.rstrip("/"))
    text = json.dumps(config, indent=2, ensure_ascii=False)
    if a.stdout:
        print(text)
    else:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out} ({len(config['tools'])} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
