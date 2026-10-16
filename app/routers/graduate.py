"""Area festeggiato (GRADUATE_TOKEN_<ID>, link personale via email oppure ADMIN_TOKEN): chi viene, auguri ricevuti,
le proprie coordinate di pagamento come le vedono gli invitati, il proprio avatar.
Nessuna cifra delle quote: le vedono solo cassieri e organizzatori."""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_graduate
from ..deps import base_url_for, get_db
from ..mail import graduate_link_mail
from ..models import AssistantAvatar, BusBooking, GiftTarget, Guest, Wish
from ..routers.bus import bus_summary
from ..routers.guests import guests_summary
from ..schemas import ProfileLinkRequest

router = APIRouter(prefix="/api/graduate", tags=["festeggiato"])


@router.post("/request-link", status_code=202)
def request_link(body: ProfileLinkRequest, request: Request, db: Session = Depends(get_db)):
    """Manda al festeggiato il link personale (https://<app>/#festeggiato=<token>) se l'email è la sua
    (GRADUATE_EMAIL_<ID> nel .env, oppure il campo email del JSON). Il link vale come GRADUATE_TOKEN_<ID>
    e resta lo stesso a ogni richiesta. La risposta non rivela se l'email è di un festeggiato."""
    mailer = request.app.state.mailer
    if not mailer.enabled:
        return {"queued": False, "message": "Il server non può inviare email in questo momento: chiedi il token agli organizzatori."}
    target = db.scalars(select(GiftTarget).where(func.lower(GiftTarget.email) == body.email)).first()
    if target is not None:
        if not target.access_token:
            target.access_token = secrets.token_urlsafe(24)
            db.commit()
        link = f"{base_url_for(request)}/#festeggiato={target.access_token}"
        subject, text, html = graduate_link_mail(target, link, request.app.state.event_data)
        mailer.send("graduate_link", target.email, subject, text, html)
    return {"queued": True, "message": "Se l'email è quella di un festeggiato riceverai il link fra poco: controlla la posta (anche lo spam)."}


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
