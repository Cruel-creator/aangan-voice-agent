> **Superseded in part (2026-10-07):** Vapi and Supabase were dropped. The voice platform is not fixed (see docs/PLATFORM_API.md), the database is Neon Postgres, and languages are English + Hindi. Phases 2, 3, 6 and 9 below describe the original plan.

# Build plan — paste one phase at a time into Claude Code

Start each phase in plan mode (Shift+Tab) so Claude Code proposes before it
writes. Do not move on until that phase's "Done when" is true. The order
builds the decision logic first and the voice last: if the brain is wrong, a
nice voice only makes it wrong faster.

---

## Phase 0 — Scaffold (30 min)

> Read CLAUDE.md, config/rules.yaml, prompts/voice_agent_system_prompt.md and
> tests/fixtures/transcript_cases.yaml. Scaffold the repo layout described in
> CLAUDE.md using uv, Python 3.12, FastAPI, Pydantic v2, pytest and ruff. Add
> app/config.py that loads rules.yaml into typed Pydantic models and reads
> secrets from env vars (.env.example with every variable, no real values).
> Add a /health route. Do not write any business logic yet.

**Done when:** `uv run pytest` runs (zero tests is fine), `/health` returns 200,
`.env.example` lists every secret.

---

## Phase 1 — Qualification engine (the core)

> Build app/qualify/ exactly as CLAUDE.md describes: scope.py, area.py,
> timeline.py, budget.py, decision.py. Every threshold comes from rules.yaml.
> Rules:
> - area: case-insensitive match against allow/deny, tolerate spelling variants
>   ("Pimple Saudagar" vs "Pimple Saudaagar", "KP" vs "Koregaon Park"), return unclear for unknown places.
> - timeline: parse ISO dates, month names ("March" → next occurrence, end of
>   month), relative phrases ("in 3 weeks"); compute weeks from an injected
>   `today` (never datetime.now() inside the logic). site_available is NOT a deadline.
> - budget: implement the floor selection and clearly_below ratio from rules.yaml.
>   The public function returns only "ok" | "clearly_below" | "not_mentioned".
> - decision: follow the decision logic in CLAUDE.md and produce outcome + flags
>   (TIGHT, VIP, RECOVERY, DECISION_NOTE).
> Write tests/test_qualify.py (unit tests per criterion, including edge cases at
> exactly 10 and 14 weeks) and tests/test_transcripts.py that loads every case in
> transcript_cases.yaml and asserts outcome and flags. Also add a test that
> greps every agent-facing string (prompt file, tool response models) and fails
> if it contains any number from budget.floors_inr or the words "per sq ft",
> "lakh" or "₹".

**Done when:** all fixture cases pass, including T02 (TIGHT), T10
(BUDGET_MISALIGNED), T15 (possession ≠ deadline), T18 (HUMAN_REVIEW), F06
(late start qualifies), and the price-leak test passes.

---

## Phase 2 — Data layer

> Create Supabase migrations in supabase/migrations/ for: designers (id, name,
> telegram_chat_id, calendar_id, areas[], handles_commercial, active), calls
> (id, platform_call_id, phone_hash, phone_encrypted, started_at, ended_at,
> in_office_hours, pickup_seconds, duration_seconds, intent, outcome, flags[],
> criteria jsonb, transcript_url, cost_inr), leads (id, call_id, name, locality,
> property_type, project_category, rooms[], area_sqft, service, completion,
> budget_inr, decision_maker, referral_source, vip, recovery, designer_id,
> booking_id, status), handoffs (id, lead_id, sent_at, acked_at, acked_by,
> action, rerouted_at), consult_outcomes (lead_id, held bool, won bool,
> project_value_inr). Build app/services/crm.py with typed functions. Phone
> numbers: store a hash for lookup plus an encrypted copy. Add seed data for
> 3 test designers.

**Done when:** migrations apply on a fresh Supabase project, and crm.py has
tests against a local Postgres or a mocked client.

---

## Phase 3 — Tool webhook API

> Implement every tool in the CLAUDE.md tool contract as a FastAPI endpoint under
> /tools/{name}, plus app/platform/vapi.py that unwraps Vapi's tool-call envelope
> and wraps responses in the format Vapi expects (check Vapi's current docs —
> do not guess the schema). Authenticate with a shared-secret header (constant-
> time compare). Tool handlers stay thin: validate input → call qualify/ or
> services/ → return the contract response. Add /webhooks/end-of-call that
> logs the call server-side from Vapi's end-of-call report even if log_call
> was never called, and alerts the front desk if a QUALIFIED call has no handoff.

**Done when:** each endpoint has a test with a recorded sample payload, an
unauthenticated request gets 401, and no tool response can contain a price.

---

## Phase 4 — Telegram handoff

> Build app/services/telegram.py. send_handoff posts the template from the spec
> (Step 7) to the assigned designer's private chat, with inline buttons Accept /
> Calling now / Reassign. Handle callback queries at /webhooks/telegram and write
> acked_at, acked_by and action to handoffs. Add a scheduled job (APScheduler or
> a cron endpoint) that re-routes handoffs unacknowledged after
> routing.handoff_ack_reroute_hours to the studio head. After a consultation, send
> the designer a follow-up with buttons "Consultation held / No-show" and
> "Won / Lost" that write to consult_outcomes. escalate_urgent sends to the
> studio head immediately with an URGENT prefix.

**Done when:** a test bot in a test chat receives a handoff, button taps update
the DB, and the re-route job fires in a test with a shortened timer.

---

## Phase 5 — Calendar booking + SMS

> Build app/services/calendar.py with a Google Calendar service account.
> get_slots returns 2–3 slots in the next 10 days from designers whose areas
> include the caller's locality (commercial → handles_commercial), round-robin by
> least recent assignment, inside 10:00–19:00 on working days, plus weekend slots
> for site visits. book_consultation creates the event, retries once on failure,
> and returns confirmed: false if it still fails — never raise into the agent.
> Build app/services/sms.py behind an interface (provider chosen by env var) and
> send a confirmation after a confirmed booking.

**Done when:** booking works against a test calendar, a double-booking race is
handled (re-check free/busy before insert), and a failed insert returns
confirmed: false.

---

## Phase 6 — Voice agent on Vapi

> Write scripts/configure_agent.py that creates or updates the Vapi assistant
> through Vapi's API: system prompt from prompts/voice_agent_system_prompt.md,
> tool definitions matching the CLAUDE.md contract and pointing at our deployed
> /tools URLs, voice and transcriber settings that support English, Hindi and
> Marathi, max call duration 8 minutes, and the end-of-call webhook. Read Vapi's
> current API docs first. Also add whatever pre-speech or response hook Vapi
> currently offers to block any spoken price (₹, "lakh", "per square foot", digits
> near "cost") and replace it with the approved pricing line. If Vapi has no such
> hook, say so and implement the check as a post-call transcript audit that
> flags the call on the dashboard.

**Done when:** a test call to the number reaches the agent, the agent calls the
tools, and a booking lands in the test calendar.

---

## Phase 7 — Simulated-call evaluation

> Build tests/sim/: for each case in transcript_cases.yaml with a caller_brief,
> run a text-mode conversation where one LLM plays the caller (brief + persona,
> including pushing for price where the brief says so) and our agent prompt plus
> real tool endpoints play the studio. Assert the final outcome and flags match
> expected, that no price was ever spoken, that complaints never got a
> qualification question, and that bookings were only confirmed after
> confirmed: true. Output a markdown report with a pass rate.

**Done when:** at least 95% of cases pass across 3 runs. This is the launch bar.

---

## Phase 8 — Dashboard

> Build /dashboard (protected by basic auth or a magic-link token): one HTML page
> with Chart.js loaded as a UMD script from cdnjs. Sections, in order: Speed (%
> answered under 5 min, median pickup, after-hours share, median time to designer
> ack), Funnel (calls → enquiries → qualified → booked → held → won), Decline
> reasons, Quality (human-review rate, escalations, transfers, price-leak flags,
> weekly audit agreement), Cost and value (monthly cost, cost per call, cost per
> booked consultation, pipeline value = booked × win rate × average project value).
> Cost per call comes from Vapi's end-of-call cost field plus telephony plus a
> fixed monthly share; implement it in app/services/cost.py. Date-range filter,
> default last 30 days.

**Done when:** seeded data renders every section, and the numbers match a SQL
query you run by hand.

---

## Phase 9 — Deploy and pilot

> Add a Dockerfile and a render.yaml (web service + cron for the re-route job).
> Document go-live steps in README.md: set env vars, run migrations, run
> configure_agent.py, point the Indian number at Vapi. Configure call forwarding
> so office-hours calls ring the front desk first and overflow to the AI after
> 20 seconds, and after-hours calls go straight to the AI.

**Done when:** a two-week pilot on after-hours and overflow calls is live, with a
weekly 10-transcript human audit logged on the dashboard.

---

## Verify early (blockers people hit late)

1. **Indian inbound number.** Confirm which telephony providers your voice
   platform supports for an Indian DID (Plivo, Exotel and Twilio are common
   options) and how call forwarding from the existing studio number works. Do
   this in week one.
2. **Hindi/Marathi speech quality.** Test the transcriber on real Pune accents
   before the pilot.
3. **Recording consent and data retention** under India's DPDP Act — confirm the
   wording with whoever handles compliance for Aangan.
