-- Designers can receive handoffs by email (Resend) as well as, or instead of, Telegram.
alter table designers add column email text;
alter table designers alter column telegram_chat_id drop not null;
alter table designers add constraint designers_contact_check
    check (telegram_chat_id is not null or email is not null);
