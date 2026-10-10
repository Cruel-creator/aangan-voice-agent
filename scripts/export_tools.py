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
import sys
from pathlib import Path

from app.config import PROMPT_PATH, ROOT, get_rules, get_settings
from app.tools.schemas import greeting, tool_definitions


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
