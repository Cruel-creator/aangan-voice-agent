-- Hand-check the dashboard (Phase 8 "done when"). Run in the Neon SQL editor (or psql on DATABASE_URL_UNPOOLED).
-- Set the window to match the dashboard's date filter (end date is inclusive on the page).
with w as (select timestamptz '2026-09-01 00:00+05:30' as s, timestamptz '2026-10-01 00:00+05:30' as e),
c as (select * from calls, w where started_at >= w.s and started_at < w.e),
l as (select leads.* from leads join c on c.id = leads.call_id),
o as (select consult_outcomes.* from consult_outcomes join l on l.id = consult_outcomes.lead_id)
select
  (select count(*) from c)                                                   as calls,
  (select count(*) from c where intent in ('new','follow_up'))               as enquiries,
  (select count(*) from c where outcome = 'QUALIFIED')                       as qualified,
  (select count(*) from l where booking_id is not null)                      as booked,
  (select count(*) from l join o on o.lead_id = l.id where l.booking_id is not null and o.held) as held,
  (select count(*) from l join o on o.lead_id = l.id where l.booking_id is not null and o.won)  as won,
  (select count(*) from c where outcome = 'ESCALATE_URGENT')                 as escalations,
  (select count(*) from c where price_leak)                                  as price_leak_flags,
  (select count(*) from c where outcome is null)                             as unlogged_calls,
  (select round(coalesce(sum(cost_inr),0), 2) from c)                        as variable_cost_inr;
-- total cost = variable_cost_inr + FIXED_MONTHLY_COST_INR × (window days / 30)
