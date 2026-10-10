-- Website receptionist demo.
-- Demo conversations are stored like calls but flagged, so they never mix into real
-- numbers and never trigger handoffs, bookings, SMS or follow-ups.

alter table calls add column if not exists is_demo boolean not null default false;
create index if not exists calls_is_demo_started_idx on calls (is_demo, started_at);

create table if not exists demo_sessions (
    id          uuid primary key default gen_random_uuid(),
    call_id     uuid references calls (id) on delete cascade,
    messages    jsonb not null default '[]'::jsonb,   -- full model conversation, append-only
    turns       integer not null default 0,
    language    text not null default 'en',
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists demo_sessions_updated_idx on demo_sessions (updated_at);

alter table demo_sessions enable row level security;
