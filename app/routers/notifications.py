from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..deps import get_db
from ..models import EventNotification, now_ms
from ..scheduler import IMMEDIATE_THRESHOLD_MS
from ..schemas import NotificationIn
from ..state import bump_version

router = APIRouter(prefix="/api/notifications", tags=["notifiche"])


@router.get("")
def list_notifications(
    since: int = Query(default=0, ge=0, description="timestamp (ms): restituisce solo le notifiche successive"),
    db: Session = Depends(get_db),
):
    """Cronologia pubblica: le notifiche programmate compaiono solo quando vengono pubblicate."""
    stmt = (
        select(EventNotification)
        .where(EventNotification.scheduled_at.is_(None))
        .order_by(EventNotification.timestamp.desc())
    )
    if since:
        stmt = stmt.where(EventNotification.timestamp > since)
    return [n.to_dict() for n in db.scalars(stmt).all()]


@router.get("/scheduled", dependencies=[Depends(require_admin)])
def list_scheduled(db: Session = Depends(get_db)):
    """Notifiche programmate non ancora pubblicate (solo organizzatori)."""
    stmt = (
        select(EventNotification)
        .where(EventNotification.scheduled_at.is_not(None))
        .order_by(EventNotification.scheduled_at.asc())
    )
    return [n.to_dict() for n in db.scalars(stmt).all()]


@router.post("", status_code=201, dependencies=[Depends(require_admin)])
def send_notification(
    body: NotificationIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Salva la notifica e la invia in push a tutti i dispositivi iscritti (in background).
    Con sendAt nel futuro la notifica resta in attesa: lo scheduler la pubblica all'ora indicata."""
    now = now_ms()
    if body.sendAt is not None and body.sendAt > now + IMMEDIATE_THRESHOLD_MS:
        notif = EventNotification(
            title=body.title, message=body.message, category=body.category,
            timestamp=body.sendAt, scheduled_at=body.sendAt,
        )
        db.add(notif)
        db.commit()  # nessun bump di versione: per gli invitati non è ancora successo nulla
        db.refresh(notif)
        return notif.to_dict()

    notif = EventNotification(title=body.title, message=body.message, category=body.category)
    db.add(notif)
    bump_version(db)
    db.commit()
    db.refresh(notif)
    push = request.app.state.push
    background.add_task(
        push.broadcast, request.app.state.session_factory, notif.title, notif.message, notif.category, notif.id
    )
    return notif.to_dict()


@router.delete("/{notification_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_notification(notification_id: int, db: Session = Depends(get_db)):
    """Annulla una notifica programmata o rimuove dalla cronologia una già pubblicata."""
    notif = db.get(EventNotification, notification_id)
    if notif is None:
        raise HTTPException(404, "Notifica non trovata")
    was_public = notif.scheduled_at is None
    db.delete(notif)
    if was_public:
        bump_version(db)
    db.commit()
    return None
