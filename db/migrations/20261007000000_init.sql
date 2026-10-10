-- Aangan Studio voice agent: initial schema.
-- Phone numbers are stored as an HMAC hash (lookup) + an encrypted copy (Fernet), never in clear.
-- gen_random_uuid() is built into Postgres 13+, no extension needed.

create table designers (
    id                  uuid primary key default gen_random_uuid(),
    name                text not null,
    telegram_chat_id    text not null,          -- private chat only, never a group
    calendar_id         text not null,
    areas               text[] not null default '{}',
    handles_commercial  boolean not null default false,
    active              boolean not null default true,
    last_assigned_at    timestamptz             -- added: round-robin by least recent assignment
);

create table calls (
    id                uuid primary key default gen_random_uuid(),
    platform_call_id  text unique,
    phone_hash        text,
    phone_encrypted   text,
    started_at        timestamptz,
    ended_at          timestamptz,
    in_office_hours   boolean,
    pickup_seconds    numeric,
    duration_seconds  numeric,
    intent            text,
    outcome           text,
    flags             text[] not null default '{}',
    criteria          jsonb not null default '{}'::jsonb,
    transcript_url    text,
    cost_inr          numeric,
    -- added: raw platform data kept server-side for audits and cost
    transcript        text,
    ended_reason      text,
    platform_cost_usd numeric,
    summary           text,
    price_leak        boolean not null default false,
    logged_by_agent   boolean not null default false,
    created_at        timestamptz not null default now()
);
create index calls_phone_hash_idx on calls (phone_hash);
create index calls_started_at_idx on calls (started_at);

create table leads (
    id                uuid primary key default gen_random_uuid(),
    call_id           uuid references calls (id) on delete set null,
    name              text,
    locality          text,
    property_type     text,
    project_category  text,
    rooms             text[] not null default '{}',
    area_sqft         numeric,
    service           text,
    completion        text,
    budget_inr        numeric,
    decision_maker    text,
    referral_source   text,
    vip               boolean not null default false,
    recovery          boolean not null default false,
    designer_id       uuid references designers (id),
    booking_id        text,
    status            text not null default 'new',
    -- added
    consult_type      text,
    consult_at        timestamptz,
    open_question     text,
    followup_sent_at  timestamptz,
    created_at        timestamptz not null default now()
);
create index leads_call_id_idx on leads (call_id);
create index leads_created_at_idx on leads (created_at);

create table handoffs (
    id           uuid primary key default gen_random_uuid(),
    lead_id      uuid not null references leads (id) on delete cascade,
    sent_at      timestamptz not null default now(),
    acked_at     timestamptz,
    acked_by     text,
    action       text,          -- accept | calling_now | reassign
    rerouted_at  timestamptz
);
create index handoffs_unacked_idx on handoffs (sent_at) where acked_at is null and rerouted_at is null;

create table consult_outcomes (
    lead_id            uuid primary key references leads (id) on delete cascade,
    held               boolean,
    won                boolean,
    project_value_inr  numeric,
    updated_at         timestamptz not null default now()
);

-- added: weekly 10-transcript human audit (Phase 9)
create table transcript_audits (
    id          uuid primary key default gen_random_uuid(),
    call_id     uuid not null references calls (id) on delete cascade,
    auditor     text not null,
    agrees      boolean not null,
    notes       text,
    audited_at  timestamptz not null default now()
);

-- The backend connects as the table owner, which bypasses RLS. Enabling RLS with no
-- policies keeps every other role (e.g. if Neon's Data API is ever switched on) locked out.
alter table designers        enable row level security;
alter table calls            enable row level security;
alter table leads            enable row level security;
alter table handoffs         enable row level security;
alter table consult_outcomes enable row level security;
alter table transcript_audits enable row level security;
