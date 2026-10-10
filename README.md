# Aangan Studio — voice enquiry agent backend

The backend for an AI phone agent: qualification engine, tool endpoints, booking,
Telegram handoffs, dashboard. It does not depend on any one voice platform. The
platform calls it over HTTPS as described in [docs/PLATFORM_API.md](docs/PLATFORM_API.md).
Database: Neon Postgres. Languages: English and Hindi.

`CLAUDE.md` has the non-negotiable rules.

## Run locally

```bash
uv sync
uv run pytest
uv run uvicorn app.main:app --reload
uv run python -m scripts.export_tools --stdout
```

Run the real-Postgres tests against a local Postgres or a Neon **test branch**
(use the direct, non-pooler string):

```bash
TEST_DATABASE_URL=postgresql://... uv run pytest tests/test_postgres.py
```

Windows note: if the project lives under a very long path, DLL imports fail with
"filename or extension is too long". Put the venv somewhere short:
`$env:UV_PROJECT_ENVIRONMENT="$env:TEMP\aangan-venv"`.

## What is verified vs not

| Verified | How |
|---|---|
| Qualification engine: all 24 fixture cases | `tests/test_transcripts.py` |
| No price floor, "lakh", "₹" or "per sq ft" in the prompt, exported tool schemas, response schemas or any real tool response | `tests/test_price_leak.py` |
| Tool contract (401/404/422/503), booking (retry, slot taken, race), handoff + buttons, re-route with a shortened timer, follow-ups, end-of-call logging, front-desk alert, English/Hindi price audit | `tests/test_api.py` (fakes for Google/Telegram/SMS) |
| Migration runner, every CRM query, and the full HTTP flow on **real Postgres 16** | `tests/test_postgres.py` |
| Dashboard numbers = hand calculation = `docs/dashboard_check.sql` | `tests/test_dashboard.py` |
| `render.yaml` passes Render's JSON schema | manual run |

**Not verified (needs your accounts):**
- Neon itself: the pooler and scale-to-zero behaviour
- Docker build
- Real Google Calendar, Telegram and Twilio calls
- A real voice platform and a real phone call
- Simulated-call pass rate (`tests/sim/run_sim.py` costs Claude API usage)

## Go-live steps

1. **Neon:** create a project in the region closest to Render Singapore. From Connect, copy the
   **pooled** string into `DATABASE_URL` and the **direct** string into `DATABASE_URL_UNPOOLED` (`neon link` does both, into `.env.local`).
   Edit `db/seed.sql` with real designers (Telegram chat ids, calendar ids, areas), then:
   `uv run python -m scripts.migrate --seed` (later deploys run migrations automatically via
   `preDeployCommand`).
2. **Google:** create a service account. Each designer shares their calendar with its email
   ("Make changes to events"). Put the JSON in `GOOGLE_SERVICE_ACCOUNT_JSON`.
3. **Telegram:** create a bot with @BotFather. Every designer and the studio head must open it
   and press Start. Set the webhook:
   `https://api.telegram.org/bot<TOKEN>/setWebhook?url=<PUBLIC_BASE_URL>/webhooks/telegram&secret_token=<TELEGRAM_WEBHOOK_SECRET>`
4. **Render:** deploy with `render.yaml` and fill every `sync: false` value.
5. **Voice platform:** run `uv run python -m scripts.export_tools`, load `agent_config.json` into the
   platform, point tools at `/tools/{name}` and the end-of-call hook at `/webhooks/end-of-call`,
   with the Bearer secret (see `docs/PLATFORM_API.md`). Use an English + Hindi speech model.
6. **Telephony:** at the studio's carrier, ring the front desk during office hours with overflow to
   the AI after 20 s, and send after-hours calls straight to the AI.
7. **Simulation:** run `uv run --group sim python -m tests.sim.run_sim --runs 3` and reach ≥95%
   before the pilot.
8. **Pilot:** audit 10 transcripts a week from `/dashboard/calls/<id>`.
