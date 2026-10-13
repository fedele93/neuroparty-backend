"""Email (tramite n8n): stato, outbox e reinvio, per gli organizzatori."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_automation, require_treasurer_or_automation
from ..deadlines import deadline, deadline_passed
from ..deps import base_url_for, get_db
from ..mail import KINDS, bus_mail, format_date_it, menu_reminder_mail, test_mail
from ..models import BusBooking, Guest, MailOutbox, now_ms
from .gifts import collectors_of, effective_collector_id, pool_rows_for

router = APIRouter(prefix="/api/mail", tags=["email"], dependencies=[Depends(require_admin)])
# Promemoria: anche con il token automazioni, così n8n può lanciarli a orario (es. il giorno prima)
reminders = APIRouter(prefix="/api/mail/reminders", tags=["email"], dependencies=[Depends(require_automation)])


class TestMailIn(BaseModel):
    to: str = Field(min_length=3, max_length=200)


@router.get("/status")
def mail_status(request: Request, db: Session = Depends(get_db)):
    counts = {s: int(n) for s, n in db.execute(select(MailOutbox.status, func.count()).group_by(MailOutbox.status)).all()}
    return {"enabled": request.app.state.mailer.enabled, "webhook": request.app.state.webhooks.status(), "counts": counts, "kinds": KINDS}


@router.get("/outbox")
def outbox(limit: int = 100, status: str | None = None, db: Session = Depends(get_db)):
    q = select(MailOutbox).order_by(MailOutbox.created_at.desc()).limit(max(1, min(limit, 500)))
    if status:
        q = q.where(MailOutbox.status == status.upper())
    return [m.to_dict() for m in db.scalars(q).all()]


@router.post("/outbox/{mail_id}/resend")
def resend(mail_id: int, request: Request):
    row = request.app.state.mailer.resend(mail_id)
    if row is None:
        raise HTTPException(404, "Email non trovata")
    return row


@router.post("/test", status_code=202)
def send_test(body: TestMailIn, request: Request):
    subject, text, html = test_mail(request.app.state.event_data)
    return request.app.state.mailer.send("test", body.to.strip(), subject, text, html)


def _link(request: Request, guest: Guest) -> str:
    from .profile import ensure_token

    return f"{base_url_for(request)}/#profilo={ensure_token(guest)}"


@reminders.post("/menu")
def remind_menu(request: Request, db: Session = Depends(get_db)):
    """Email a chi ha confermato, ha un'email e non ha ancora scelto il menu (suo o di un accompagnatore).
    Niente invii dopo la data limite del menu."""
    event_data = request.app.state.event_data
    if deadline_passed(event_data, "menuDeadline"):
        return {"sent": 0, "skipped": "termine del menu passato"}
    deadline_text = format_date_it(deadline(event_data, "menuDeadline")) if deadline(event_data, "menuDeadline") else ""
    sent = []
    for g in db.scalars(select(Guest).where(Guest.rsvp_status == "CONFIRMED")).all():
        if not g.email:
            continue
        if g.menu and all(c.get("menu") for c in g.companions):
            continue
        link = _link(request, g)
        db.commit()
        subject, text, html = menu_reminder_mail(g, link, event_data, deadline_text)
        sent.append(request.app.state.mailer.send("menu_reminder", g.email, subject, text, html, guest_id=g.id))
    return {"sent": len(sent), "mails": sent}


@reminders.post("/bus")
def remind_bus(request: Request, db: Session = Depends(get_db)):
    """Promemoria a tutte le prenotazioni confermate collegate a un profilo con email (da lanciare il giorno prima)."""
    sent = []
    for b in db.scalars(select(BusBooking).where(BusBooking.status != "WAITLIST", BusBooking.guest_id.is_not(None))).all():
        g = db.get(Guest, b.guest_id)
        if not g or not g.email:
            continue
        subject, text, html, attachments = bus_mail("bus_reminder", g, b, request)
        sent.append(request.app.state.mailer.send("bus_reminder", g.email, subject, text, html, guest_id=g.id, attachments=attachments))
    return {"sent": len(sent), "mails": sent}


pool_reminders = APIRouter(prefix="/api/mail/reminders", tags=["email"])


@pool_reminders.post("/pool")
def remind_pool(request: Request, days: int = 7, db: Session = Depends(get_db), scope: set[str] | None = Depends(require_treasurer_or_automation)):
    """Promemoria ai donatori con quota unica ancora in attesa da almeno `days` giorni e con un'email
    (cassiere: solo le proprie quote; organizzatori e automazioni: tutte)."""
    collectors = {c["id"]: c for c in collectors_of(request)}
    limit = now_ms() - max(0, days) * 86_400_000
    sent = []
    for c in pool_rows_for(db, scope, list(collectors.values())):
        email = c.email or (c.contact if "@" in (c.contact or "") and " " not in c.contact else "")
        if c.status == "RECEIVED" or not email or (c.created_at or 0) > limit:
            continue
        collector = collectors.get(effective_collector_id(c, list(collectors.values()))) or {}
        from ..mail import pool_mail

        subject, text, html = pool_mail("pool_reminder", c, collector, base_url_for(request))
        sent.append(request.app.state.mailer.send("pool_reminder", email, subject, text, html))
    return {"sent": len(sent), "mails": sent}
