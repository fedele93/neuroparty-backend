from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import base_url_for, get_db
from ..models import (
    BusBooking,
    EventNotification,
    GiftContribution,
    GiftTarget,
    Guest,
    SharedPhoto,
    Wish,
    now_ms,
)
from ..push import subscription_count
from ..schedule import build_ics
from ..seed import public_event_info
from ..state import get_version

router = APIRouter(prefix="/api", tags=["evento"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/event")
def event_info(request: Request):
    """Dati statici dell'evento (programma, mappa, navetta, laureandi) letti da event-data.json."""
    return public_event_info(request.app.state.event_data)


@router.get("/event/calendar.ics")
def event_calendar(request: Request):
    """File iCalendar con seduta e festa (date/orari dal blocco "schedule" del JSON):
    su iPhone e Android apre direttamente "Aggiungi al calendario"."""
    ics = build_ics(request.app.state.event_data, request.app.state.settings.public_url)
    return Response(
        content=ics,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="neuroparty.ics"', "Cache-Control": "no-cache"},
    )


@router.get("/state")
def state(request: Request, db: Session = Depends(get_db)):
    """Chiamata leggera per il polling: se 'version' cambia, il client ricarica lo snapshot."""
    return {
        "version": get_version(db),
        "serverTime": now_ms(),
        "adminEnabled": bool(request.app.state.settings.admin_token),
        "pushSubscriptions": subscription_count(db),
    }


@router.get("/snapshot")
def snapshot(request: Request, db: Session = Depends(get_db)):
    """Tutte le collezioni in una sola risposta: usato dall'app Android per sincronizzare Room."""
    base = base_url_for(request)
    return {
        "version": get_version(db),
        "serverTime": now_ms(),
        "event": public_event_info(request.app.state.event_data),
        "guests": [g.to_dict() for g in db.scalars(select(Guest).order_by(Guest.full_name.asc())).all()],
        "busBookings": [b.to_dict() for b in db.scalars(select(BusBooking).order_by(BusBooking.booked_at.desc())).all()],
        "wishes": [w.to_dict() for w in db.scalars(select(Wish).order_by(Wish.created_at.desc())).all()],
        "photos": [p.to_dict(base) for p in db.scalars(select(SharedPhoto).order_by(SharedPhoto.created_at.desc())).all()],
        "giftTargets": [t.to_dict() for t in db.scalars(select(GiftTarget).order_by(GiftTarget.sort_order.asc())).all()],
        "giftContributions": [
            c.to_dict() for c in db.scalars(select(GiftContribution).order_by(GiftContribution.contributed_at.desc())).all()
        ],
        "notifications": [
            n.to_dict()
            for n in db.scalars(
                select(EventNotification)
                .where(EventNotification.scheduled_at.is_(None))
                .order_by(EventNotification.timestamp.desc())
            ).all()
        ],
    }
