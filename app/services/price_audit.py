"""Post-call audit: did the agent SAY anything that looks like a price?

Most voice platforms cannot block a sentence before it is spoken, so this
after-the-fact audit is the guaranteed check. Flagged calls show on the dashboard
and alert the front desk.
"""

from __future__ import annotations

import re

PRICE_PATTERNS = [
    re.compile(r"₹"),
    re.compile(r"\brs\.?\s*\d", re.I),
    re.compile(r"\b(rupees?|rupaye|rupaiye|lakhs?|lacs?|crores?|karod)\b", re.I),
    re.compile(r"\bper\s*(sq(uare)?\.?\s*(ft|feet|foot)|square)", re.I),
    re.compile(r"(लाख|करोड़|रुपये|रुपए|रुपया)"),  # Hindi currency words
    # A digit within a few words of a cost word, either order
    re.compile(r"\b(cost|price|budget|rate|charge|fees?)\b\W+(?:\w+\W+){0,4}\d", re.I),
    re.compile(r"\d[\d,.]*\W+(?:\w+\W+){0,4}(cost|price|rate|charges?)\b", re.I),
]

AGENT_ROLES = {"agent", "assistant", "bot", "ai"}


def agent_lines(messages: list[dict], transcript: str | None) -> list[str]:
    lines = [
        str(m.get("text") or m.get("message") or m.get("content") or "")
        for m in messages
        if str(m.get("role", "")).lower() in AGENT_ROLES
    ]
    if not lines and transcript:
        # Fallback for plain transcripts formatted "AI: ..." / "User: ...".
        lines = [
            ln.split(":", 1)[1]
            for ln in transcript.splitlines()
            if ln.lower().startswith(("ai:", "assistant:", "bot:"))
        ]
    return lines


def find_price_mentions(lines: list[str]) -> list[str]:
    return [ln for ln in lines if any(p.search(ln) for p in PRICE_PATTERNS)]
