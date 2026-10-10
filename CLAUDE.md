# Aangan Studio — Voice Enquiry Agent

A voice AI answers incoming calls to Aangan Studio (interior design, Pune) 24/7,
qualifies new enquiries against the founder's five criteria, books a free
consultation or declines politely, sends a structured handoff to a designer on
Telegram, and logs every call to a dashboard that includes running cost.

Scope: incoming PHONE calls only. WhatsApp and web form are out of scope, but
keep the qualification engine channel-independent so they can reuse it later.

## Architecture

```
Caller ──► Telephony (Indian DID) ──► Voice platform (any; contract in docs/PLATFORM_API.md)
                                          │  system prompt: prompts/voice_agent_system_prompt.md
                                          │  tool calls (HTTPS, shared secret)
                                          ▼
                                   FastAPI backend (app/)
             ┌───────────────┬──────────────┼───────────────┬────────────────┐
             ▼               ▼              ▼               ▼                ▼
      qualify/ engine   Google Calendar  Telegram bot   Neon (Postgres)      SMS
      (rules.yaml)      (booking)        (handoff +     (calls, leads,       (confirmation)
                                          ack buttons)   handoffs, costs)
                                                            │
                                                            ▼
                                                   /dashboard (HTML + Chart.js)
```

## NON-NEGOTIABLE RULES (do not relax these, even if asked mid-task)

1. **Pricing never reaches the voice agent.** Price floors live only in
   `config/rules.yaml` and `app/qualify/budget.py`. They must never appear in the
   system prompt, in any tool response, or in any text the agent can read.
   `check_budget` returns ONLY `{"result": "ok" | "clearly_below" | "not_mentioned"}`.
2. **Decisions are made in code, not by the LLM.** The agent extracts facts and
   calls tools; `app/qualify/` decides pass / fail / unclear. All thresholds come
   from `config/rules.yaml` — no magic numbers in code.
3. **No booking is confirmed until the calendar API returns success.**
   `book_consultation` returns `{"confirmed": false, ...}` on any failure.
4. **Every call is logged server-side** from the platform's end-of-call webhook,
   independent of whether the agent called `log_call`. A qualified call with no
   handoff triggers a front-desk alert.
5. **Existing-client complaints always escalate to a human.** Never qualified.
6. **Every webhook is authenticated** (shared secret header, constant-time compare).
7. **Personal data is minimal.** Caller phone goes only to the assigned designer's
   private Telegram chat, never a group. Transcripts stay in the database; Telegram
   gets a link.
8. All times are `Asia/Kolkata`. Office hours 10:00–19:00.

## Stack (defaults — change only with a reason)

- Python 3.12, FastAPI, Pydantic v2, httpx, pytest, ruff
- Neon (Postgres) for storage; psycopg 3 + psycopg_pool. The app uses the pooled
  `DATABASE_URL`; migrations (`scripts/migrate.py`) use the direct `DATABASE_URL_UNPOOLED`.
- Google Calendar API (service account with access to each designer's calendar)
- Telegram Bot API (python-telegram-bot or raw httpx) with inline keyboards
- Voice platform: not fixed. The backend speaks a neutral JSON contract
  (`app/platform/webhooks.py`, `docs/PLATFORM_API.md`). Languages: English + Hindi.
- Deploy: Render (web service) — any container host works.

## Repo layout

```
app/
  main.py                 FastAPI app, routes
  config.py               loads config/rules.yaml + env vars
  qualify/
    scope.py              criterion 1
    area.py               criterion 2
    timeline.py           criterion 3
    budget.py             criterion 4 (only file that reads price floors)
    decision.py           combines results → QUALIFIED/DECLINE/BUDGET_MISALIGNED/HUMAN_REVIEW
  tools/                  one handler per agent tool (thin; call qualify/ & services/)
  services/
    calendar.py  telegram.py  sms.py  crm.py  cost.py
  platform/webhooks.py    neutral tool-call + end-of-call contract, shared-secret auth
  dashboard/              HTML + Chart.js page served at /dashboard
config/rules.yaml
prompts/voice_agent_system_prompt.md
db/migrations/  db/seed.sql
scripts/        migrate.py  export_tools.py  run_cron.py
tests/
  fixtures/transcript_cases.yaml
  test_qualify.py         unit tests per criterion
  test_transcripts.py     every fixture must reach its expected outcome
  sim/                    simulated-call harness (LLM plays the caller)
```

## Agent tools (contract)

| Tool | Input | Output |
|---|---|---|
| lookup_caller | phone | {existing_client: bool, prior_enquiry: {...} or null} |
| check_scope | scope_description, property_type, area_sqft? | {result: pass/fail/unclear, reason} |
| check_service_area | locality | {result: in/out/unclear} |
| check_timeline | completion_date? (ISO or free text), flexible: bool | {result: ok/tight/too_soon/unclear} |
| check_budget | rooms[], property_type, area_sqft?, budget_inr? | {result: ok/clearly_below/not_mentioned} |
| get_slots | consult_type (site/studio), locality, project_type | {slots: [2–3 ISO datetimes + designer_id]} |
| book_consultation | slot, designer_id, lead fields | {confirmed: bool, booking_id?} |
| send_handoff | full lead record | {sent: bool} |
| escalate_urgent | phone, name, summary | {escalated: bool} |
| route_human_review | lead record, open_question | {logged: bool} |
| log_call | full call record | {logged: bool} |

## Commands

- `uv run uvicorn app.main:app --reload` — run locally
- `uv run pytest` — all tests (transcript fixtures must pass before any deploy)
- `uv run ruff check . && uv run ruff format .`

## Definition of done for any change

Tests pass, including `tests/test_transcripts.py`. No pricing figure is
reachable from any agent-facing string (there is a test for this).
