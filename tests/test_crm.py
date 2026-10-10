from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from app.services.crm import (
    CRM,
    CallRecord,
    Lead,
    MemoryBackend,
    decrypt_phone,
    encrypt_phone,
    hash_phone,
    normalize_phone,
)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=IST)


def test_phone_normalisation_and_hash_are_stable():
    assert normalize_phone("098220 12345") == "+919822012345"
    assert normalize_phone("+91 98220-12345") == "+919822012345"
    assert hash_phone("9822012345", "k") == hash_phone("+91 98220 12345", "k")
    assert hash_phone("9822012345", "k") != hash_phone("9822012345", "other")
    assert "9822012345" not in hash_phone("9822012345", "k")


def test_phone_encryption_round_trip():
    key = Fernet.generate_key().decode()
    token = encrypt_phone("9822012345", key)
    assert "9822012345" not in token
    assert decrypt_phone(token, key) == "+919822012345"


def test_upsert_call_merges_without_overwriting_with_none():
    crm = CRM(MemoryBackend())
    crm.upsert_call(CallRecord(platform_call_id="c1", intent="new", outcome="QUALIFIED"))
    merged = crm.upsert_call(CallRecord(platform_call_id="c1", cost_inr=12.5))
    assert merged.intent == "new" and merged.outcome == "QUALIFIED" and merged.cost_inr == 12.5
    assert len(crm.db.tables["calls"]) == 1


def test_lookup_caller_finds_prior_enquiry_and_existing_client():
    crm = CRM(MemoryBackend())
    call = crm.upsert_call(CallRecord(platform_call_id="c1", phone_hash="h1"))
    lead = crm.create_lead(Lead(call_id=call.id, locality="Viman Nagar", status="new"))
    assert crm.lookup_caller("h1") == (False, crm.get_lead(lead.id))
    crm.upsert_consult_outcome(lead.id, held=True, won=True)
    existing, prior = crm.lookup_caller("h1")
    assert existing and prior.id == lead.id
    assert crm.lookup_caller("unknown") == (False, None)


def test_unacked_handoffs_and_reroute():
    crm = CRM(MemoryBackend())
    old = crm.create_handoff("L1", NOW - timedelta(hours=3))
    acked = crm.create_handoff("L2", NOW - timedelta(hours=3))
    crm.create_handoff("L3", NOW)
    crm.ack_handoff(acked.id, "designer", "accept", NOW)
    due = crm.unacked_handoffs(NOW - timedelta(hours=2))
    assert [h.id for h in due] == [old.id]
    crm.mark_rerouted(old.id, NOW)
    assert crm.unacked_handoffs(NOW - timedelta(hours=2)) == []


def test_leads_due_followup():
    crm = CRM(MemoryBackend())
    due = crm.create_lead(Lead(status="booked", consult_at=NOW - timedelta(hours=1)))
    crm.create_lead(Lead(status="booked", consult_at=NOW + timedelta(hours=1)))
    crm.create_lead(Lead(status="booked", consult_at=NOW - timedelta(hours=1), followup_sent_at=NOW))
    assert [lead.id for lead in crm.leads_due_followup(NOW)] == [due.id]
