"""Email (tramite n8n): stato, outbox e reinvio, per gli organizzatori."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..deps import get_db
from ..mail import KINDS, test_mail
from ..models import MailOutbox

router = APIRouter(prefix="/api/mail", tags=["email"], dependencies=[Depends(require_admin)])


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
