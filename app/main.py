from __future__ import annotations

import hmac
import html
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ValidationError

from app.config import get_rules, get_settings
from app.dashboard import metrics
from app.platform import webhooks
from app.services import clock, jobs, telegram
from app.services.crm import Audit, get_crm, is_uuid
from app.tools.handlers import TOOLS

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


# ---------- scheduled jobs (Render cron calls these) ----------
def _cron_auth(request: Request) -> None:
    if not _same(request.headers.get("x-cron-secret"), get_settings().cron_secret):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/cron/reroute-handoffs", dependencies=[Depends(_cron_auth)])
def cron_reroute() -> dict:
    return {"rerouted": jobs.reroute_unacked_handoffs(get_crm(), clock.now())}


@app.post("/cron/consult-followups", dependencies=[Depends(_cron_auth)])
def cron_followups() -> dict:
    return {"sent": jobs.send_consult_followups(get_crm(), clock.now())}


# ---------- dashboard ----------
basic = HTTPBasic()


def _dashboard_auth(creds: HTTPBasicCredentials = Depends(basic)) -> str:
    s = get_settings()
    if not (_same(creds.username, s.dashboard_user) and _same(creds.password, s.dashboard_password)):
        raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Basic"})
    return creds.username


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(_dashboard_auth)])
def dashboard() -> str:
    return (Path(__file__).parent / "dashboard" / "index.html").read_text(encoding="utf-8")


@app.get("/dashboard/api/metrics", dependencies=[Depends(_dashboard_auth)])
def dashboard_metrics(start: date | None = None, end: date | None = None) -> dict:
    tz = clock.tz()
    end_d = end or clock.now().date()
    start_d = start or end_d - timedelta(days=30)
    return metrics.compute(
        get_crm(),
        datetime.combine(start_d, time.min, tzinfo=tz),
        datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=tz),
    )


@app.get("/dashboard/calls/{call_id}", response_class=HTMLResponse)
def dashboard_call(call_id: str, user: str = Depends(_dashboard_auth)) -> str:
    call = get_crm().get_call(call_id) if is_uuid(call_id) else None
    if not call:
        raise HTTPException(status_code=404)
    e = html.escape
    rows = "".join(
        f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>"
        for k, v in [
            ("started", call.started_at),
            ("outcome", call.outcome),
            ("flags", ", ".join(call.flags or [])),
            ("criteria", call.criteria),
            ("price leak", call.price_leak),
            ("ended reason", call.ended_reason),
            ("summary", call.summary),
        ]
    )
    return (
        "<!doctype html><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>Call detail</title>"
        "<body style='font:15px system-ui;max-width:800px;margin:auto;padding:16px'>"
        f"<h1>Call {e(call_id)}</h1><table>{rows}</table>"
        f"<h2>Transcript</h2><pre style='white-space:pre-wrap'>{e(call.transcript or '-')}</pre>"
        f"<h2>Audit</h2><form method='post' action='{e(call_id)}/audit'>"
        "<label><input type='radio' name='agrees' value='true' required> Agent handled it right"
        "</label><br><label><input type='radio' name='agrees' value='false'> Disagree</label><br>"
        "<textarea name='notes' rows='3' cols='60' placeholder='notes'></textarea><br>"
        "<button>Save audit</button></form></body>"
    )


class AuditIn(BaseModel):
    agrees: bool
    notes: str | None = None


@app.post("/dashboard/calls/{call_id}/audit", response_class=HTMLResponse)
async def dashboard_audit(call_id: str, request: Request, user: str = Depends(_dashboard_auth)) -> str:
    if not (is_uuid(call_id) and get_crm().get_call(call_id)):
        raise HTTPException(status_code=404)
    form = await request.form()
    data = AuditIn(agrees=form.get("agrees") == "true", notes=form.get("notes") or None)
    get_crm().add_audit(
        Audit(call_id=call_id, auditor=user, agrees=data.agrees, notes=data.notes, audited_at=clock.now())
    )
    return "<p>Audit saved. <a href='../../../dashboard'>Back to dashboard</a></p>"
