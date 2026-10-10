"""Typed data access for calls, leads, designers, handoffs and outcomes.

The CRM logic is written once against a tiny table backend. `PostgresBackend`
talks to Neon (Postgres) in production; `MemoryBackend` backs unit tests.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any, Protocol

from cryptography.fernet import Fernet
from pydantic import BaseModel

from app.config import get_settings


# ---------- phone numbers ----------
def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    return "+" + digits


def hash_phone(raw: str, key: str | None = None) -> str:
    secret = (key or get_settings().phone_hash_key).encode()
    if not secret:
        raise RuntimeError("PHONE_HASH_KEY is not set")
    return hmac.new(secret, normalize_phone(raw).encode(), hashlib.sha256).hexdigest()


def encrypt_phone(raw: str, key: str | None = None) -> str:
    return (
        Fernet((key or get_settings().phone_encryption_key).encode())
        .encrypt(normalize_phone(raw).encode())
        .decode()
    )


def decrypt_phone(token: str, key: str | None = None) -> str:
    return Fernet((key or get_settings().phone_encryption_key).encode()).decrypt(token.encode()).decode()


# ---------- records ----------
class Designer(BaseModel):
    id: str
    name: str
    telegram_chat_id: str | None = None  # private chat only, never a group
    email: str | None = None  # handoffs by email (Resend); at least one of the two is set
    calendar_id: str
    areas: list[str] = []
    handles_commercial: bool = False
    active: bool = True
    last_assigned_at: datetime | None = None


class CallRecord(BaseModel):
    id: str | None = None
    platform_call_id: str | None = None
    phone_hash: str | None = None
    phone_encrypted: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    in_office_hours: bool | None = None
    pickup_seconds: float | None = None
    duration_seconds: float | None = None
    intent: str | None = None
    outcome: str | None = None
    flags: list[str] | None = None
    criteria: dict[str, Any] | None = None
    transcript_url: str | None = None
    cost_inr: float | None = None
    transcript: str | None = None
    ended_reason: str | None = None
    platform_cost_usd: float | None = None
    summary: str | None = None
    price_leak: bool | None = None
    logged_by_agent: bool | None = None
    is_demo: bool | None = None


class DemoSession(BaseModel):
    id: str | None = None
    call_id: str | None = None
    messages: list[dict[str, Any]] = []
    turns: int = 0
    language: str = "en"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Lead(BaseModel):
    id: str | None = None
    call_id: str | None = None
    name: str | None = None
    locality: str | None = None
    property_type: str | None = None
    project_category: str | None = None
    rooms: list[str] = []
    area_sqft: float | None = None
    service: str | None = None
    completion: str | None = None
    budget_inr: float | None = None
    decision_maker: str | None = None
    referral_source: str | None = None
    vip: bool = False
    recovery: bool = False
    designer_id: str | None = None
    booking_id: str | None = None
    status: str = "new"
    consult_type: str | None = None
    consult_at: datetime | None = None
    open_question: str | None = None
    followup_sent_at: datetime | None = None
    created_at: datetime | None = None


class Handoff(BaseModel):
    id: str | None = None
    lead_id: str
    sent_at: datetime
    acked_at: datetime | None = None
    acked_by: str | None = None
    action: str | None = None
    rerouted_at: datetime | None = None


class ConsultOutcome(BaseModel):
    lead_id: str
    held: bool | None = None
    won: bool | None = None
    project_value_inr: float | None = None


class Audit(BaseModel):
    id: str | None = None
    call_id: str
    auditor: str
    agrees: bool
    notes: str | None = None
    audited_at: datetime | None = None


# ---------- backends ----------
class Backend(Protocol):
    def insert(self, table: str, row: dict) -> dict: ...
    def update(self, table: str, match: dict, fields: dict) -> list[dict]: ...
    def select(
        self,
        table: str,
        eq: dict | None = None,
        in_: dict | None = None,
        gte: dict | None = None,
        lt: dict | None = None,
        is_null: list[str] | None = None,
    ) -> list[dict]: ...


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


class MemoryBackend:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {}

    def insert(self, table: str, row: dict) -> dict:
        row = dict(row)
        if table != "consult_outcomes":
            row.setdefault("id", str(uuid.uuid4()))
        self.tables.setdefault(table, []).append(row)
        return dict(row)

    def update(self, table: str, match: dict, fields: dict) -> list[dict]:
        out = []
        for row in self.tables.get(table, []):
            if all(row.get(k) == v for k, v in match.items()):
                row.update(fields)
                out.append(dict(row))
        return out

    def select(self, table, eq=None, in_=None, gte=None, lt=None, is_null=None):
        rows = []
        for row in self.tables.get(table, []):
            if eq and any(row.get(k) != v for k, v in eq.items()):
                continue
            if in_ and any(row.get(k) not in v for k, v in in_.items()):
                continue
            if gte and any(row.get(k) is None or row[k] < v for k, v in gte.items()):
                continue
            if lt and any(row.get(k) is None or row[k] >= v for k, v in lt.items()):
                continue
            if is_null and any(row.get(k) is not None for k in is_null):
                continue
            rows.append(dict(row))
        return rows


TABLES = {
    "designers",
    "calls",
    "leads",
    "handoffs",
    "consult_outcomes",
    "transcript_audits",
    "demo_sessions",
}
UUID_COLUMNS = {"id", "call_id", "lead_id", "designer_id"}
JSONB_COLUMNS = {"criteria", "messages"}


def is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


class PostgresBackend:
    """Plain Postgres (Neon) over psycopg 3.

    Neon's pooled endpoint is PgBouncer in transaction mode, so server-side prepared
    statements are disabled (prepare_threshold=None). The small client pool checks a
    connection before handing it out and drops idle ones before Neon's 5-minute
    scale-to-zero would kill them.
    """

    def __init__(self, conninfo: str, max_size: int = 5) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.pool = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=max_size,
            max_idle=240,
            check=ConnectionPool.check_connection,
            kwargs={"prepare_threshold": None, "connect_timeout": 10, "row_factory": dict_row},
            open=True,
        )

    @staticmethod
    def _table(name: str):
        from psycopg import sql

        if name not in TABLES:
            raise ValueError(f"unknown table {name}")
        return sql.Identifier(name)

    @staticmethod
    def _value(col: str, value: Any) -> Any:
        from psycopg.types.json import Jsonb

        if col in JSONB_COLUMNS or isinstance(value, dict):
            return Jsonb(_jsonable(value))  # lists in jsonb columns must not become SQL arrays
        return value

    @staticmethod
    def _cast(col: str):
        from psycopg import sql

        return sql.SQL("::uuid") if col in UUID_COLUMNS else sql.SQL("")

    @staticmethod
    def _row(row: dict) -> dict:
        out = {}
        for k, v in row.items():
            if isinstance(v, uuid.UUID):
                v = str(v)
            elif isinstance(v, Decimal):
                v = float(v)
            out[k] = v
        return out

    def _run(self, query, params: list, retry: bool = True) -> list[dict]:
        import psycopg

        attempts = 2 if retry else 1  # Neon may have suspended and dropped the connection
        for attempt in range(1, attempts + 1):
            try:
                with self.pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(query, params)
                    return [self._row(r) for r in cur.fetchall()] if cur.description else []
            except psycopg.OperationalError:
                if attempt == attempts:
                    raise
        return []

    def insert(self, table: str, row: dict) -> dict:
        from psycopg import sql

        cols = list(row)
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            self._table(table),
            sql.SQL(", ").join(map(sql.Identifier, cols)),
            sql.SQL(", ").join(sql.SQL("%s") + self._cast(c) for c in cols),
        )
        # No retry: a dropped connection after commit would insert twice.
        return self._run(query, [self._value(c, row[c]) for c in cols], retry=False)[0]

    def update(self, table: str, match: dict, fields: dict) -> list[dict]:
        from psycopg import sql

        sets = [sql.SQL("{} = %s").format(sql.Identifier(c)) + self._cast(c) for c in fields]
        where, params = self._where(eq=match)
        query = sql.SQL("UPDATE {} SET {} WHERE {} RETURNING *").format(
            self._table(table), sql.SQL(", ").join(sets), where
        )
        return self._run(query, [self._value(c, v) for c, v in fields.items()] + params)

    def select(self, table, eq=None, in_=None, gte=None, lt=None, is_null=None):
        from psycopg import sql

        where, params = self._where(eq, in_, gte, lt, is_null)
        query = sql.SQL("SELECT * FROM {} WHERE {}").format(self._table(table), where)
        return self._run(query, params)

    def _where(self, eq=None, in_=None, gte=None, lt=None, is_null=None):
        from psycopg import sql

        parts, params = [sql.SQL("TRUE")], []
        for k, v in (eq or {}).items():
            if k in UUID_COLUMNS and not is_uuid(v):
                parts.append(sql.SQL("FALSE"))  # e.g. a made-up designer_id from the LLM
                continue
            parts.append(sql.SQL("{} = %s").format(sql.Identifier(k)) + self._cast(k))
            params.append(self._value(k, v))
        for k, v in (in_ or {}).items():
            values = [x for x in v if is_uuid(x)] if k in UUID_COLUMNS else list(v)
            arr = sql.SQL("::uuid[]") if k in UUID_COLUMNS else sql.SQL("")
            parts.append(sql.SQL("{} = ANY(%s").format(sql.Identifier(k)) + arr + sql.SQL(")"))
            params.append(values)
        for op, cond in ((">=", gte), ("<", lt)):
            for k, v in (cond or {}).items():
                parts.append(sql.SQL("{} " + op + " %s").format(sql.Identifier(k)))
                params.append(v)
        for k in is_null or []:
            parts.append(sql.SQL("{} IS NULL").format(sql.Identifier(k)))
        return sql.SQL(" AND ").join(parts), params


# ---------- CRM ----------
def _clean(model: BaseModel) -> dict:
    return {k: v for k, v in model.model_dump().items() if v is not None}


class CRM:
    def __init__(self, backend: Backend) -> None:
        self.db = backend

    # calls
    def upsert_call(self, call: CallRecord) -> CallRecord:
        """Insert or merge a call by platform_call_id. None fields never overwrite data."""
        fields = _clean(call)
        fields.pop("id", None)
        if call.platform_call_id:
            existing = self.db.select("calls", eq={"platform_call_id": call.platform_call_id})
            if existing:
                rows = self.db.update("calls", {"id": existing[0]["id"]}, fields)
                return CallRecord(**rows[0])
        return CallRecord(**self.db.insert("calls", fields))

    def get_call(self, call_id: str) -> CallRecord | None:
        rows = self.db.select("calls", eq={"id": call_id})
        return CallRecord(**rows[0]) if rows else None

    def get_call_by_platform_id(self, platform_call_id: str) -> CallRecord | None:
        rows = self.db.select("calls", eq={"platform_call_id": platform_call_id})
        return CallRecord(**rows[0]) if rows else None

    def calls_by_phone_hash(self, phone_hash: str) -> list[CallRecord]:
        return [CallRecord(**r) for r in self.db.select("calls", eq={"phone_hash": phone_hash})]

    def calls_between(self, start: datetime, end: datetime) -> list[CallRecord]:
        rows = self.db.select("calls", gte={"started_at": start}, lt={"started_at": end})
        return [CallRecord(**r) for r in rows]

    # leads
    def create_lead(self, lead: Lead) -> Lead:
        return Lead(**self.db.insert("leads", _clean(lead)))

    def update_lead(self, lead_id: str, **fields: Any) -> Lead:
        return Lead(**self.db.update("leads", {"id": lead_id}, fields)[0])

    def get_lead(self, lead_id: str) -> Lead | None:
        rows = self.db.select("leads", eq={"id": lead_id})
        return Lead(**rows[0]) if rows else None

    def leads_for_calls(self, call_ids: list[str]) -> list[Lead]:
        if not call_ids:
            return []
        return [Lead(**r) for r in self.db.select("leads", in_={"call_id": call_ids})]

    def leads_due_followup(self, now: datetime) -> list[Lead]:
        rows = self.db.select(
            "leads",
            eq={"status": "booked"},
            lt={"consult_at": now},
            is_null=["followup_sent_at"],
        )
        return [Lead(**r) for r in rows]

    # designers
    def list_designers(self, active_only: bool = True) -> list[Designer]:
        rows = self.db.select("designers", eq={"active": True} if active_only else None)
        return [Designer(**r) for r in rows]

    def get_designer(self, designer_id: str) -> Designer | None:
        rows = self.db.select("designers", eq={"id": designer_id})
        return Designer(**rows[0]) if rows else None

    def touch_designer(self, designer_id: str, at: datetime) -> None:
        self.db.update("designers", {"id": designer_id}, {"last_assigned_at": at})

    # handoffs
    def create_handoff(self, lead_id: str, sent_at: datetime) -> Handoff:
        return Handoff(**self.db.insert("handoffs", {"lead_id": lead_id, "sent_at": sent_at}))

    def get_handoff(self, handoff_id: str) -> Handoff | None:
        rows = self.db.select("handoffs", eq={"id": handoff_id})
        return Handoff(**rows[0]) if rows else None

    def handoffs_for_leads(self, lead_ids: list[str]) -> list[Handoff]:
        if not lead_ids:
            return []
        return [Handoff(**r) for r in self.db.select("handoffs", in_={"lead_id": lead_ids})]

    def ack_handoff(self, handoff_id: str, by: str, action: str, at: datetime) -> Handoff:
        rows = self.db.update(
            "handoffs", {"id": handoff_id}, {"acked_at": at, "acked_by": by, "action": action}
        )
        return Handoff(**rows[0])

    def unacked_handoffs(self, sent_before: datetime) -> list[Handoff]:
        rows = self.db.select("handoffs", lt={"sent_at": sent_before}, is_null=["acked_at", "rerouted_at"])
        return [Handoff(**r) for r in rows]

    def mark_rerouted(self, handoff_id: str, at: datetime) -> None:
        self.db.update("handoffs", {"id": handoff_id}, {"rerouted_at": at})

    # consultation outcomes
    def upsert_consult_outcome(self, lead_id: str, **fields: Any) -> ConsultOutcome:
        if self.db.select("consult_outcomes", eq={"lead_id": lead_id}):
            rows = self.db.update("consult_outcomes", {"lead_id": lead_id}, fields)
            return ConsultOutcome(**rows[0])
        return ConsultOutcome(**self.db.insert("consult_outcomes", {"lead_id": lead_id, **fields}))

    def outcomes_for_leads(self, lead_ids: list[str]) -> list[ConsultOutcome]:
        if not lead_ids:
            return []
        rows = self.db.select("consult_outcomes", in_={"lead_id": lead_ids})
        return [ConsultOutcome(**r) for r in rows]

    # audits
    def add_audit(self, audit: Audit) -> Audit:
        return Audit(**self.db.insert("transcript_audits", _clean(audit)))

    def audits_between(self, start: datetime, end: datetime) -> list[Audit]:
        rows = self.db.select("transcript_audits", gte={"audited_at": start}, lt={"audited_at": end})
        return [Audit(**r) for r in rows]

    # caller lookup
    def audits_for_call(self, call_id: str) -> list[Audit]:
        return [Audit(**r) for r in self.db.select("transcript_audits", eq={"call_id": call_id})]

    # website demo sessions
    def create_demo_session(self, call_id: str, language: str, at: datetime) -> DemoSession:
        row = {
            "call_id": call_id,
            "messages": [],
            "turns": 0,
            "language": language,
            "created_at": at,
            "updated_at": at,
        }
        return DemoSession(**self.db.insert("demo_sessions", row))

    def get_demo_session(self, session_id: str) -> DemoSession | None:
        rows = self.db.select("demo_sessions", eq={"id": session_id})
        return DemoSession(**rows[0]) if rows else None

    def save_demo_session(self, session: DemoSession, at: datetime) -> DemoSession:
        fields = {
            "messages": session.messages,
            "turns": session.turns,
            "language": session.language,
            "updated_at": at,
        }
        return DemoSession(**self.db.update("demo_sessions", {"id": session.id}, fields)[0])

    def demo_turns_since(self, since: datetime) -> int:
        return sum(
            int(r.get("turns") or 0) for r in self.db.select("demo_sessions", gte={"updated_at": since})
        )

    def lookup_caller(self, phone_hash: str, exclude_call_id: str | None = None) -> tuple[bool, Lead | None]:
        """(existing_client, most recent prior enquiry), ignoring the current call."""
        calls = [c for c in self.calls_by_phone_hash(phone_hash) if c.id != exclude_call_id]
        leads = self.leads_for_calls([c.id for c in calls if c.id])
        if not leads:
            return False, None
        won = {o.lead_id for o in self.outcomes_for_leads([lead.id for lead in leads]) if o.won}
        existing = any(lead.id in won or lead.status == "client" for lead in leads)
        latest = max(leads, key=lambda lead: lead.created_at.timestamp() if lead.created_at else 0)
        return existing, latest


_override: CRM | None = None


def set_crm(crm: CRM | None) -> None:
    """Tests inject a MemoryBackend-backed CRM here."""
    global _override
    _override = crm
    _default_crm.cache_clear()


@lru_cache
def _default_crm() -> CRM:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return CRM(PostgresBackend(url))


def get_crm() -> CRM:
    return _override or _default_crm()
