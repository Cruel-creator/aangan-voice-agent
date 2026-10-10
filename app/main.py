from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from app import demo
from app.config import get_rules, get_settings
from app.dashboard import metrics
from app.platform import webhooks
from app.services import clock, jobs, telegram
from app.services.crm import Audit, get_crm, is_uuid
from app.tools.handlers import TOOLS

WEB = Path(__file__).parent / "web"

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_rules()  # fail fast on a bad rules.yaml
    yield


app = FastAPI(title="Aangan Studio voice agent backend", lifespan=lifespan)


def _same(a: str | None, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------- agent tools (contract: docs/PLATFORM_API.md) ----------
@app.post("/tools/{name}", dependencies=[Depends(webhooks.verify_secret)])
def tool_endpoint(name: str, body: webhooks.ToolRequest) -> JSONResponse:
    if name not in TOOLS:
        raise HTTPException(status_code=404, detail="unknown tool")
    model, handler = TOOLS[name]
    try:
        args = model.model_validate(body.arguments)
    except ValidationError as e:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})
        return JSONResponse(status_code=422, content={"error": "invalid arguments", "fields": fields})
    ctx = webhooks.CallContext(call_id=body.call_id, caller_number=body.caller_number)
    try:
        out = handler(args, ctx)
    except Exception:
        log.exception("tool %s failed", name)
        return JSONResponse(status_code=500, content={"error": "temporary problem, please continue"})
    return JSONResponse(content=out.model_dump(mode="json"))


# ---------- platform webhooks ----------
@app.post("/webhooks/end-of-call", dependencies=[Depends(webhooks.verify_secret)])
def end_of_call(report: webhooks.EndOfCallReport) -> dict:
    call = jobs.process_end_of_call(get_crm(), report)
    return {"ok": True, "call_id": call.id}


@app.post("/webhooks/telegram")
async def telegram_webhook(request: Request) -> dict:
    token = request.headers.get("x-telegram-bot-api-secret-token")
    if not _same(token, get_settings().telegram_webhook_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    status = telegram.handle_callback(get_crm(), await request.json(), clock.now())
    return {"ok": True, "status": status}


# ---------- scheduled jobs (.github/workflows/cron.yml calls these) ----------
def _cron_auth(request: Request) -> None:
    if not _same(request.headers.get("x-cron-secret"), get_settings().cron_secret):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/cron/reroute-handoffs", dependencies=[Depends(_cron_auth)])
def cron_reroute() -> dict:
    return {"rerouted": jobs.reroute_unacked_handoffs(get_crm(), clock.now())}


@app.post("/cron/consult-followups", dependencies=[Depends(_cron_auth)])
def cron_followups() -> dict:
    return {"sent": jobs.send_consult_followups(get_crm(), clock.now())}


# ---------- public website: receptionist demo + dashboard (no login, by owner's choice) ----------
app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


@app.middleware("http")
async def revalidate_site_files(request: Request, call_next):
    """Pages and their scripts must always match after a deploy: browsers keep a copy but
    revalidate it (a cheap 304 when unchanged) instead of running a stale script."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/") or path in ("/", "/dashboard") or path.startswith("/dashboard/calls/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _page(name: str) -> FileResponse:
    return FileResponse(WEB / name, media_type="text/html")


@app.get("/", include_in_schema=False)
def home_page() -> FileResponse:
    return _page("index.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard_page() -> FileResponse:
    return _page("dashboard.html")


@app.get("/dashboard/calls/{call_id}", include_in_schema=False)
def call_page(call_id: str) -> FileResponse:
    return _page("call.html")


def _window(start: date | None, end: date | None) -> tuple[datetime, datetime]:
    tz = clock.tz()
    end_d = end or clock.now().date()
    start_d = start or end_d - timedelta(days=30)
    return (
        datetime.combine(start_d, time.min, tzinfo=tz),
        datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=tz),
    )


@app.get("/dashboard/api/metrics")
def dashboard_metrics(start: date | None = None, end: date | None = None, include_demo: bool = False) -> dict:
    return metrics.compute(get_crm(), *_window(start, end), include_demo=include_demo)


@app.get("/dashboard/api/calls")
def dashboard_calls(start: date | None = None, end: date | None = None, include_demo: bool = False) -> dict:
    return {"calls": metrics.recent_calls(get_crm(), *_window(start, end), include_demo=include_demo)}


@app.get("/dashboard/api/calls/{call_id}")
def dashboard_call(call_id: str) -> dict:
    crm = get_crm()
    call = crm.get_call(call_id) if is_uuid(call_id) else None
    if not call:
        raise HTTPException(status_code=404, detail="call not found")
    lines = []
    for raw in demo.redact(call.transcript).splitlines():
        speaker, sep, text = raw.partition(": ")
        agent = speaker.strip().lower() in {"ai", "agent", "assistant", "bot"}
        lines.append({"speaker": "agent" if agent else "caller", "text": text if sep else raw})
    return {
        "id": call.id,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "duration_seconds": call.duration_seconds,
        "outcome": call.outcome,
        "intent": call.intent,
        "flags": call.flags or [],
        "criteria": call.criteria or {},
        "summary": demo.redact(call.summary),
        "is_demo": bool(call.is_demo),
        "price_leak": bool(call.price_leak),
        "in_office_hours": call.in_office_hours,
        "transcript": lines,
        "audits": [
            {
                "agrees": a.agrees,
                "notes": a.notes,
                "auditor": a.auditor,
                "audited_at": a.audited_at.isoformat() if a.audited_at else None,
            }
            for a in crm.audits_for_call(call.id)
        ],
    }


class AuditIn(BaseModel):
    agrees: bool
    notes: str | None = Field(None, max_length=2000)
    auditor: str | None = Field(None, max_length=80)


@app.post("/dashboard/api/calls/{call_id}/audit")
def dashboard_audit(call_id: str, body: AuditIn) -> dict:
    crm = get_crm()
    if not (is_uuid(call_id) and crm.get_call(call_id)):
        raise HTTPException(status_code=404, detail="call not found")
    crm.add_audit(
        Audit(
            call_id=call_id,
            auditor=(body.auditor or "dashboard").strip() or "dashboard",
            agrees=body.agrees,
            notes=body.notes,
            audited_at=clock.now(),
        )
    )
    return {"saved": True}


class DemoChatIn(BaseModel):
    session_id: str | None = Field(None, max_length=64)
    message: str = Field(max_length=demo.MAX_MESSAGE_CHARS)
    language: str = "en"


@app.get("/demo/status")
def demo_status() -> dict:
    return demo.status()


@app.post("/demo/chat")
def demo_chat(body: DemoChatIn) -> JSONResponse:
    session_id = body.session_id if body.session_id and is_uuid(body.session_id) else None
    try:
        return JSONResponse(demo.chat(session_id, body.message, body.language))
    except demo.DemoUnavailable as e:
        return JSONResponse(status_code=e.status, content={"error": e.message})
    except Exception:
        log.exception("demo chat failed")
        return JSONResponse(
            status_code=502, content={"error": "The receptionist couldn't answer just now. Please try again."}
        )
