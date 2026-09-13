from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..deps import get_db
from ..models import EventNotification
from ..schemas import NotificationIn
from ..state import bump_version

router = APIRouter(prefix="/api/notifications", tags=["notifiche"])


@router.get("")
def list_notifications(
    since: int = Query(default=0, ge=0, description="timestamp (ms): restituisce solo le notifiche successive"),
    db: Session = Depends(get_db),
):
    stmt = select(EventNotification).order_by(EventNotification.timestamp.desc())
    if since:
        stmt = stmt.where(EventNotification.timestamp > since)
    return [n.to_dict() for n in db.scalars(stmt).all()]


@router.post("", status_code=201, dependencies=[Depends(require_admin)])
def send_notification(
    body: NotificationIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Salva la notifica e la invia in push a tutti i dispositivi iscritti (in background)."""
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
