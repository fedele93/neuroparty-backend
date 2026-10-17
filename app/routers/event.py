from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import lists_allowed, memories_access, roles_for
from ..deps import base_url_for, get_db
from ..models import (
    BusBooking,
    EventNotification,
    GiftTarget,
    Guest,
    Wish,
    now_ms,
)
from ..push import subscription_count
from ..routers.photos import photos_total, recent_photos
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
        "treasurerEnabled": bool(request.app.state.settings.treasurer_token or request.app.state.settings.treasurer_tokens),
        # con i token inviati in questa chiamata, il client vede gli elenchi completi di invitati e navetta?
        "canSeeLists": lists_allowed(request, db),
        # foto e auguri: solo invitati registrati (X-Guest-Token) e ruoli
        "canSeeMemories": memories_access(request, db)[0],
        # ruoli sbloccati dai token di questa chiamata (guida del ruolo nella PWA, token sbagliati)
        "roles": roles_for(request),
        "pushSubscriptions": subscription_count(db),
    }


@router.get("/snapshot")
def snapshot(request: Request, db: Session = Depends(get_db)):
    """Tutte le collezioni pubbliche in una sola risposta: usato dall'app Android per sincronizzare Room.
    Invitati e prenotazioni: completi solo con un token (organizzatore, festeggiato, cassiere),
    altrimenti solo i propri record (X-Client-Id o X-Guest-Token). Foto e auguri: solo per gli invitati
    registrati e i ruoli, agli altri arrivano liste vuote (canSeeMemories=false)."""
    base = base_url_for(request)
    lists = lists_allowed(request, db)
    memories, _ = memories_access(request, db)
    # foto: solo la prima pagina (le più recenti); le più vecchie si chiedono a /api/photos/page
    photos, photos_more = recent_photos(db) if memories else ([], False)
    client_id = request.headers.get("x-client-id") or None
    token = (request.headers.get("x-guest-token") or "").strip()
    me = db.scalars(select(Guest).where(Guest.access_token == token)).first() if token else None
    guests = db.scalars(select(Guest).order_by(Guest.full_name.asc())).all()
    bookings = db.scalars(select(BusBooking).order_by(BusBooking.booked_at.desc())).all()
    if not lists:
        guests = [g for g in guests if (client_id and g.owner_client_id == client_id) or (me and g.id == me.id)]
        bookings = [b for b in bookings if (client_id and b.owner_client_id == client_id) or (me and b.guest_id == me.id)]
    return {
        "version": get_version(db),
        "serverTime": now_ms(),
        "event": public_event_info(request.app.state.event_data),
        "canSeeLists": lists,
        "canSeeMemories": memories,
        "guests": [g.to_dict() for g in guests],
        "busBookings": [b.to_dict() for b in bookings],
        "wishes": [w.to_dict() for w in db.scalars(select(Wish).order_by(Wish.created_at.desc())).all()] if memories else [],
        "photos": [p.to_dict(base) for p in photos],
        "photosHasMore": photos_more,
        # il conteggio serve solo se c'è altro oltre la prima pagina
        "photosTotal": (photos_total(db) if photos_more else len(photos)) if memories else 0,
        # niente importi né elenco delle quote: le quote uniche le vede solo il cassiere (GET /api/gifts/pool)
        "giftTargets": [t.to_dict() for t in db.scalars(select(GiftTarget).order_by(GiftTarget.sort_order.asc())).all()],
        "notifications": [
            n.to_dict()
            for n in db.scalars(
                select(EventNotification)
                .where(EventNotification.scheduled_at.is_(None))
                .order_by(EventNotification.timestamp.desc())
            ).all()
        ],
    }
