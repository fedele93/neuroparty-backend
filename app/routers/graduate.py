"""Area festeggiato (GRADUATE_TOKEN_<ID> oppure ADMIN_TOKEN): chi viene, auguri ricevuti,
le proprie coordinate di pagamento come le vedono gli invitati, il proprio avatar.
Nessuna cifra delle quote: le vedono solo cassieri e organizzatori."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_graduate
from ..deps import get_db
from ..models import AssistantAvatar, BusBooking, GiftTarget, Guest, Wish
from ..routers.bus import bus_summary
from ..routers.guests import guests_summary

router = APIRouter(prefix="/api/graduate", tags=["festeggiato"])


@router.get("/me")
def me(request: Request, graduate_id: str | None = None, db: Session = Depends(get_db), scope: set[str] | None = Depends(require_graduate)):
    """Il cruscotto del festeggiato. Con il token organizzatore serve ?graduate_id=<id>."""
    if scope is not None:
        graduate_id = next(iter(scope))
    if not graduate_id:
        raise HTTPException(422, "Indica graduate_id")
    target = db.get(GiftTarget, graduate_id)
    if target is None:
        raise HTTPException(404, "Festeggiato non trovato")
    guests = db.scalars(select(Guest).order_by(Guest.full_name.asc())).all()
    short = target.short_name.lower()
    wishes = [
        w.to_dict() for w in db.scalars(select(Wish).order_by(Wish.created_at.desc())).all()
        if (w.target_graduate or "").strip().lower() == short or (w.target_graduate or "").strip().lower().startswith("tutti")
    ]
    avatar = db.get(AssistantAvatar, graduate_id)
    svc = request.app.state.assistant
    bookings = db.scalars(select(BusBooking).where(BusBooking.status != "WAITLIST")).all()
    return {
        "graduate": {**target.to_dict(), "email": target.email},
        "guests": {
            **guests_summary(guests),
            "confirmed": [{"fullName": g.full_name, "category": g.category, "guestsCount": g.guests_count, "companions": [c.get("name", "") for c in g.companions]}
                          for g in guests if g.rsvp_status == "CONFIRMED"],
            "pending": [g.full_name for g in guests if g.rsvp_status == "PENDING"],
        },
        "wishes": wishes,
        "wishesForMe": sum(1 for w in wishes if (w["targetGraduate"] or "").strip().lower() == short),
        "bus": {**bus_summary(request, db), "passengers": sum(b.seats_count for b in bookings)},
        "avatar": avatar.to_admin(svc.voices.fallback_available) if avatar else None,
        "assistantConfigured": svc.client.enabled,
    }
