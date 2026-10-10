"""Simulated-call evaluation (Phase 7). Costs real API money — run deliberately.

    uv run --group sim python -m tests.sim.run_sim --runs 3            # all cases
    uv run --group sim python -m tests.sim.run_sim --runs 1 --cases T02,T09

One Claude instance plays the caller from `caller_brief`; another plays the studio
using prompts/voice_agent_system_prompt.md and the SAME tool definitions that
scripts/export_tools.py exports for the voice platform. Tools execute against the
real FastAPI endpoints in-process (docs/PLATFORM_API.md contract), with the
database, Google Calendar, Telegram and SMS replaced by the test fakes.

Caveat: production runs on whatever LLM your voice platform uses. This harness
tests the prompt + tools with SIM_STUDIO_MODEL; results only transfer if both
use the same model.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
from datetime import datetime, time, timedelta
from pathlib import Path

import yaml
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]
CASES = yaml.safe_load((ROOT / "tests/fixtures/transcript_cases.yaml").read_text(encoding="utf-8"))
SECRET = secrets.token_hex(16)
os.environ.update(
    {
        "TOOL_WEBHOOK_SECRET": SECRET,
        "PHONE_HASH_KEY": secrets.token_hex(16),
        "PHONE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "TELEGRAM_STUDIO_HEAD_CHAT_ID": "999",
        "TELEGRAM_FRONT_DESK_CHAT_ID": "-100",
        "PUBLIC_BASE_URL": "https://sim.test",
        "STUDIO_ADDRESS": "the studio",
    }
)

import anthropic  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import PROMPT_PATH, get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import calendar, clock, sms, telegram  # noqa: E402
from app.services.crm import (  # noqa: E402
    CRM,
    CallRecord,
    Designer,
    Lead,
    MemoryBackend,
    encrypt_phone,
    hash_phone,
    set_crm,
)
from app.services.price_audit import find_price_mentions  # noqa: E402
from app.tools.schemas import greeting, tool_definitions  # noqa: E402
from tests.conftest import IST, FakeCalendar, FakeTelegram  # noqa: E402

STUDIO_MODEL = os.environ.get("SIM_STUDIO_MODEL", "claude-opus-5-5")
CALLER_MODEL = os.environ.get("SIM_CALLER_MODEL", "claude-opus-5-5")
MAX_TURNS = 30
QUALIFY_TOOLS = {"check_scope", "check_service_area", "check_timeline", "check_budget"}
ENGINE_AND_CALL_FLAGS = {"TIGHT", "VIP", "RECOVERY", "DECISION_NOTE", "PRICING_ASKED", "RESUMED_CALL"}
CONFIRM_WORDS = re.compile(r"\b(you're booked|is booked|booking is confirmed|confirmed for)\b", re.I)

client = anthropic.Anthropic()


def create(**kw):
    """Claude call with server-side refusal fallback (opted in by default)."""
    return client.beta.messages.create(
        max_tokens=16000, betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw
    )


def text_of(resp) -> str:
    return " ".join(b.text for b in resp.content if b.type == "text").strip()


def anthropic_tools() -> list[dict]:
    out = []
    for t in tool_definitions("https://sim.test"):
        out.append({"name": t["name"], "description": t["description"], "input_schema": t["parameters"]})
    return out


def setup_world(case: dict, phone: str):
    get_settings.cache_clear()
    crm = CRM(MemoryBackend())
    set_crm(crm)
    telegram.set_telegram(FakeTelegram())
    cal = FakeCalendar()
    calendar.set_calendar(cal)
    sms.set_sms(sms.ConsoleSms())
    now = datetime.combine(case["call_date"], time(11, 0), tzinfo=IST)
    clock.now = lambda: now  # sim clock
    for d in [
        Designer(
            id="d-west",
            name="West",
            telegram_chat_id="101",
            calendar_id="w",
            areas=["Kothrud", "Baner", "Aundh", "Wakad", "Hinjewadi", "Kalyani Nagar", "Koregaon Park"],
            handles_commercial=True,
        ),
        Designer(
            id="d-east",
            name="East",
            telegram_chat_id="102",
            calendar_id="e",
            areas=[
                "Kharadi",
                "Viman Nagar",
                "Hadapsar",
                "Magarpatta",
                "Undri",
                "NIBM",
                "Kalyani Nagar",
                "Koregaon Park",
            ],
        ),
        Designer(
            id="d-pcmc",
            name="PCMC",
            telegram_chat_id="103",
            calendar_id="p",
            areas=["Pimple Saudagar", "Wakad", "Pimpri", "Chinchwad"],
            handles_commercial=True,
        ),
    ]:
        crm.db.insert("designers", d.model_dump())

    intent = case["facts"].get("intent")
    # History the brief implies (existing client, earlier enquiry, dropped call).
    if (
        intent == "existing_client_complaint"
        or case["facts"].get("prior_enquiry_found")
        or "drops" in case.get("caller_brief", "")
    ):
        prior = crm.upsert_call(
            CallRecord(
                platform_call_id=f"prior-{case['id']}",
                phone_hash=hash_phone(phone),
                phone_encrypted=encrypt_phone(phone),
                started_at=now
                - (timedelta(minutes=3) if "drops" in case["caller_brief"] else timedelta(days=4)),
                ended_at=now
                - (timedelta(minutes=2) if "drops" in case["caller_brief"] else timedelta(days=4)),
            )
        )
        lead = crm.create_lead(
            Lead(call_id=prior.id, locality=case["facts"].get("locality"), created_at=now - timedelta(days=4))
        )
        if intent == "existing_client_complaint":
            crm.upsert_consult_outcome(lead.id, held=True, won=True)
    return crm, now


def run_case(case: dict) -> dict:
    phone = "+9198" + str(abs(hash(case["id"])) % 10**8).zfill(8)
    crm, now = setup_world(case, phone)
    http = TestClient(app)
    call_id = f"sim-{case['id']}-{secrets.token_hex(3)}"
    tools = anthropic_tools()
    studio_system = (
        PROMPT_PATH.read_text(encoding="utf-8")
        + f"\n\n[Simulation: this is a text transcript of a phone call. You already "
        f'greeted the caller with: "{greeting()}". Today is '
        f"{now:%A %d %B %Y}, {now:%I:%M %p} IST.]"
    )
    caller_system = (
        "You are role-playing a person phoning an interior design studio in Pune. Stay in "
        f"character. Your situation: {case['caller_brief']}\n"
        "Speak naturally and briefly, one utterance per turn, like a real phone caller. Only "
        "reveal details when asked or when natural. If asked something your situation does not "
        "cover, give a plausible answer consistent with it; if asked about a deadline that is "
        "not in your situation, say you have no fixed deadline. If your situation says you push "
        "for a price, ask for a price or range at least twice. Accept a consultation slot if "
        "offered and it suits your situation. When the call is naturally over, reply with "
        "exactly [HANGUP]."
    )

    studio_msgs: list[dict] = []
    caller_msgs: list[dict] = [{"role": "user", "content": greeting()}]
    agent_lines: list[str] = [greeting()]
    tool_log: list[tuple[str, dict, dict]] = []
    transcript = [f"AI: {greeting()}"]
    confirmed_seen, premature_confirm, hung_up = False, False, False
    logged_before_hangup = False

    def studio_turn(user_content) -> str:
        nonlocal confirmed_seen, premature_confirm
        studio_msgs.append({"role": "user", "content": user_content})
        while True:
            resp = create(model=STUDIO_MODEL, system=studio_system, tools=tools, messages=studio_msgs)
            studio_msgs.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                return "[agent refused]"
            uses = [b for b in resp.content if b.type == "tool_use"]
            said = text_of(resp)
            if said:
                if CONFIRM_WORDS.search(said) and not confirmed_seen:
                    premature_confirm = True
            if not uses:
                return said
            results = []
            for u in uses:
                resp_ = http.post(
                    f"/tools/{u.name}",
                    json={"call_id": call_id, "caller_number": phone, "arguments": u.input},
                    headers={"Authorization": f"Bearer {SECRET}"},
                )
                payload = resp_.text
                parsed = resp_.json()
                tool_log.append((u.name, u.input, parsed))
                if u.name == "book_consultation" and parsed.get("confirmed"):
                    confirmed_seen = True
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": payload})
            studio_msgs.append({"role": "user", "content": results})

    for _ in range(MAX_TURNS):
        cresp = create(model=CALLER_MODEL, system=caller_system, messages=caller_msgs)
        caller_msgs.append({"role": "assistant", "content": cresp.content})
        utterance = text_of(cresp)
        if "[HANGUP]" in utterance or not utterance:
            hung_up = True
            logged_before_hangup = any(n == "log_call" for n, _, _ in tool_log)
            break
        transcript.append(f"User: {utterance}")
        said = studio_turn(utterance)
        agent_lines.append(said)
        transcript.append(f"AI: {said}")
        caller_msgs.append({"role": "user", "content": said or "..."})
    if not any(n == "log_call" for n, _, _ in tool_log):
        studio_turn("[The caller has hung up. Finish any required tool calls now.]")

    # Same end-of-call path as production.
    started = now
    http.post(
        "/webhooks/end-of-call",
        headers={"Authorization": f"Bearer {SECRET}"},
        json={
            "call_id": call_id,
            "caller_number": phone,
            "started_at": started.isoformat(),
            "ended_at": (started + timedelta(minutes=5)).isoformat(),
            "platform_cost_usd": 0,
            "messages": [
                {"role": "agent" if ln.startswith("AI:") else "caller", "text": ln.split(": ", 1)[1]}
                for ln in transcript
            ],
        },
    )
    call = crm.get_call_by_platform_id(call_id)

    expected = case["expected"]
    got_flags = set(call.flags or []) & ENGINE_AND_CALL_FLAGS if call else set()
    checks = {
        "outcome": bool(call) and call.outcome == expected["outcome"],
        "flags": got_flags == set(expected["flags"]),
        "no_price_spoken": not find_price_mentions(agent_lines),
        "complaint_not_qualified": expected["outcome"] != "ESCALATE_URGENT"
        or not any(n in QUALIFY_TOOLS for n, _, _ in tool_log),
        "booking_confirmed_only_after_tool": not premature_confirm,
    }
    return {
        "id": case["id"],
        "passed": all(checks.values()),
        "checks": checks,
        "outcome": call.outcome if call else None,
        "flags": sorted(got_flags),
        "expected": expected,
        "tools": [n for n, _, _ in tool_log],
        "logged_before_hangup": logged_before_hangup,
        "hung_up": hung_up,
        "transcript": transcript,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--cases", default="")
    ap.add_argument("--out", default="sim_report.md")
    a = ap.parse_args()
    wanted = set(filter(None, a.cases.split(",")))
    cases = [
        c
        for c in CASES["cases"]
        if c.get("caller_brief")
        and c["expected"]["outcome"] != "NOT_APPLICABLE"  # T08 is a routing-only case
        and (not wanted or c["id"] in wanted)
    ]

    rows, all_results = [], []
    for run in range(1, a.runs + 1):
        for case in cases:
            try:
                r = run_case(case)
            except anthropic.APIError as e:
                r = {
                    "id": case["id"],
                    "passed": False,
                    "checks": {"api_error": False},
                    "outcome": None,
                    "flags": [],
                    "expected": case["expected"],
                    "tools": [],
                    "logged_before_hangup": False,
                    "transcript": [str(e)],
                }
            r["run"] = run
            all_results.append(r)
            print(f"run {run} {r['id']}: {'PASS' if r['passed'] else 'FAIL'} {r['checks']}")

    passed = sum(r["passed"] for r in all_results)
    rate = 100 * passed / len(all_results) if all_results else 0
    rows.append(
        f"# Simulated-call report\n\nStudio model: `{STUDIO_MODEL}`, caller model: "
        f"`{CALLER_MODEL}`, runs: {a.runs}\n\n**Pass rate: {passed}/{len(all_results)} "
        f"= {rate:.1f}%** (launch bar: 95%)\n\nlog_call before hang-up: "
        f"{sum(r['logged_before_hangup'] for r in all_results)}/{len(all_results)}\n"
    )
    rows.append("| run | case | result | outcome (expected) | flags (expected) | failed checks |")
    rows.append("|---|---|---|---|---|---|")
    for r in all_results:
        failed = ", ".join(k for k, v in r["checks"].items() if not v) or "-"
        rows.append(
            f"| {r['run']} | {r['id']} | {'PASS' if r['passed'] else 'FAIL'} | "
            f"{r['outcome']} ({r['expected']['outcome']}) | {', '.join(r['flags'])} "
            f"({', '.join(r['expected']['flags'])}) | {failed} |"
        )
    rows.append("\n## Failed transcripts\n")
    for r in all_results:
        if not r["passed"]:
            rows.append(
                f"### run {r['run']} {r['id']}\nTools: {', '.join(r['tools'])}\n\n```\n"
                + "\n".join(r["transcript"])
                + "\n```\n"
            )
    Path(a.out).write_text("\n".join(rows), encoding="utf-8")
    print(f"\nPass rate {rate:.1f}% → {a.out}")


if __name__ == "__main__":
    main()
