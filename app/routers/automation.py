"""Endpoint per le automazioni (n8n): report pronti da spedire, stato dei webhook, test.

Autenticazione: header X-Automation-Token (AUTOMATION_TOKEN, sola lettura) oppure X-Admin-Token.
Per inviare notifiche push da n8n si usa invece POST /api/notifications con X-Admin-Token.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from ..auth import require_automation
from ..deps import base_url_for, get_db
from ..report import build_report, render_html, render_text
from ..webhooks import EVENTS
from .bus import max_seats

router = APIRouter(prefix="/api/automation", tags=["automazioni (n8n)"], dependencies=[Depends(require_automation)])

SinceHours = Query(default=24, gt=0, le=24 * 365, description="finestra (ore) per la sezione 'novità'")


def _report(request: Request, db: Session, since_hours: float) -> dict:
    return build_report(
        db,
        request.app.state.event_data,
        max_seats=max_seats(request),
        base_url=base_url_for(request),
        since_hours=since_hours,
    )


@router.get("/report")
def report_json(request: Request, sinceHours: float = SinceHours, db: Session = Depends(get_db)):
    """Report completo in JSON: coperti, navetta, regali, novità recenti, notifiche programmate."""
    return _report(request, db, sinceHours)


@router.get("/report.txt", response_class=PlainTextResponse)
def report_text(request: Request, sinceHours: float = SinceHours, db: Session = Depends(get_db)):
    """Lo stesso report come testo semplice (corpo di una mail o di un messaggio Telegram)."""
    return PlainTextResponse(render_text(_report(request, db, sinceHours)), headers={"Cache-Control": "no-store"})


@router.get("/report.html", response_class=HTMLResponse)
def report_html(request: Request, sinceHours: float = SinceHours, db: Session = Depends(get_db)):
    """Lo stesso report come pagina HTML pronta per una mail."""
    return HTMLResponse(render_html(_report(request, db, sinceHours)), headers={"Cache-Control": "no-store"})


@router.get("/status")
def status(request: Request):
    """Configurazione dei webhook verso n8n (senza segreti), statistiche e catalogo degli eventi."""
    settings = request.app.state.settings
    return {
        "webhook": request.app.state.webhooks.status(),
        "automationTokenEnabled": bool(settings.automation_token),
        "events": [{"name": name, "description": desc} for name, desc in EVENTS.items()],
    }


@router.post("/webhook-test")
def webhook_test(request: Request):
    """Invia subito un evento "test" all'URL configurato e riporta l'esito (utile per collaudare n8n)."""
    hooks = request.app.state.webhooks
    if not hooks.enabled:
        raise HTTPException(503, "N8N_WEBHOOK_URL non configurato sul server")
    result = hooks.emit(
        "test",
        {"message": "Ciao da NeuroParty! Se leggi questo messaggio il collegamento con n8n funziona.",
         "app": base_url_for(request)},
        sync=True,
    )
    if result is None:  # "test" escluso dal filtro N8N_WEBHOOK_EVENTS
        raise HTTPException(409, "L'evento 'test' è escluso da N8N_WEBHOOK_EVENTS")
    return result
