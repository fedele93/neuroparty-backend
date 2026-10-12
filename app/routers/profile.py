"""Profilo personale dell'invitato (utenza leggera, senza password).

- POST /api/profile/request-link   {email, fullName?}  -> email con il link personale (crea l'invitato se non c'è)
- GET  /api/profile                 X-Guest-Token       -> il proprio profilo
- PUT  /api/profile                 X-Guest-Token       -> aggiorna RSVP, contatti, accompagnatori e menu; email di conferma
- POST /api/profile/logout          X-Guest-Token       -> rigenera il token (i link vecchi smettono di valere)

Il link è https://<app>/#profilo=<token>: la PWA lo legge, salva il token sul dispositivo e da
quel momento manda X-Guest-Token. Gli organizzatori generano e inviano i link da /api/guests.
"""
from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import base_url_for, get_db
from ..mail import access_link_mail, rsvp_confirmation_mail
from ..models import Guest, now_ms
from ..routers.guests import guests_summary
from ..schemas import ProfileIn, ProfileLinkRequest
from ..state import bump_version

router = APIRouter(prefix="/api/profile", tags=["profilo"])


def new_token() -> str:
    return secrets.token_urlsafe(24)


def profile_link(request: Request, token: str) -> str:
    return f"{base_url_for(request)}/#profilo={token}"


def ensure_token(guest: Guest) -> str:
    if not guest.access_token:
        guest.access_token = new_token()
        guest.token_created_at = now_ms()
    return guest.access_token


def find_by_email(db: Session, email: str) -> Guest | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    return db.scalars(select(Guest).where(func.lower(Guest.email) == email).order_by(Guest.id.asc())).first()


def send_access_link(request: Request, db: Session, guest: Guest) -> dict | None:
    """Email con il link personale (richiede un'email nel profilo). Ritorna la riga dell'outbox o None."""
    if not guest.email:
        return None
    token = ensure_token(guest)
    db.commit()
    subject, text, html = access_link_mail(guest, profile_link(request, token), request.app.state.event_data)
    return request.app.state.mailer.send("access_link", guest.email, subject, text, html, guest_id=guest.id)


def current_guest(
    request: Request,
    db: Session = Depends(get_db),
    x_guest_token: str | None = Header(default=None),
) -> Guest:
    token = (x_guest_token or "").strip()
    if not token:
        raise HTTPException(401, "Apri il tuo link personale per accedere al profilo")
    guest = db.scalars(select(Guest).where(Guest.access_token == token)).first()
    if guest is None:
        raise HTTPException(401, "Link non valido o scaduto: richiedine uno nuovo con la tua email")
    return guest


def _profile_payload(guest: Guest, db: Session) -> dict:
    return {"guest": guest.to_dict(private=True), "summary": {"covers": guests_summary(db.scalars(select(Guest)).all())["covers"]}}


# ---------------------------------------------------------------------- endpoint


@router.post("/request-link", status_code=202)
def request_link(body: ProfileLinkRequest, request: Request, db: Session = Depends(get_db)):
    """Manda il link personale all'email indicata. Se l'email non è in lista e c'è un nome, crea
    l'invitato (in attesa). La risposta non rivela se l'email esiste."""
    guest = find_by_email(db, body.email)
    created = False
    if guest is None:
        if not body.fullName.strip():
            return {"queued": True, "created": False, "message": "Se l'indirizzo è in lista riceverai il link fra poco. Non sei ancora in lista? Aggiungi il tuo nome."}
        guest = Guest(full_name=body.fullName.strip(), category="Invitato", rsvp_status="PENDING", guests_count=1, email=body.email)
        db.add(guest)
        bump_version(db)
        db.commit()
        db.refresh(guest)
        created = True
    outbox = send_access_link(request, db, guest)
    mail_ok = bool(outbox and outbox.get("status") in ("QUEUED", "DELIVERED"))
    msg = "Ti abbiamo inviato il link personale: controlla la posta (anche lo spam)." if mail_ok else \
        "Il server non può inviare email in questo momento: chiedi il link agli organizzatori."
    return {"queued": mail_ok, "created": created, "message": msg}


@router.get("")
def my_profile(guest: Guest = Depends(current_guest), db: Session = Depends(get_db)):
    guest.last_login_at = now_ms()
    db.commit()
    return _profile_payload(guest, db)


@router.put("")
def update_profile(body: ProfileIn, request: Request, guest: Guest = Depends(current_guest), db: Session = Depends(get_db)):
    if guest.consent_at is None and not body.consent:
        raise HTTPException(422, "Per salvare il profilo serve il consenso al trattamento dei dati")
    companions = [c.model_dump() for c in body.companions if c.name.strip()]
    for c in companions:
        c["name"] = c["name"].strip()
        c["notes"] = c["notes"].strip()
    changes = {}
    for attr, value in (
        ("full_name", body.fullName), ("category", body.category or "Invitato"), ("rsvp_status", body.rsvpStatus),
        ("email", body.email), ("phone", body.phone), ("menu", body.menu), ("dietary_notes", body.dietaryNotes),
        ("companions_json", json.dumps(companions, ensure_ascii=False)),
    ):
        if getattr(guest, attr) != value:
            changes[attr] = value
            setattr(guest, attr, value)
    guest.guests_count = 0 if body.rsvpStatus == "DECLINED" else 1 + len(companions)
    guest.contact_info = guest.email or guest.phone or guest.contact_info
    if body.consent and guest.consent_at is None:
        guest.consent_at = now_ms()
    guest.updated_at = now_ms()
    bump_version(db)
    db.commit()
    db.refresh(guest)
    request.app.state.webhooks.emit(
        "guest.updated", {"guest": guest.to_dict(), "changes": {k: v for k, v in changes.items() if k != "companions_json"},
                          "summary": guests_summary(db.scalars(select(Guest)).all()), "viaProfile": True}
    )
    mail = None
    if guest.email:
        subject, text, html = rsvp_confirmation_mail(guest, profile_link(request, ensure_token(guest)), request.app.state.event_data)
        mail = request.app.state.mailer.send("rsvp_confirmation", guest.email, subject, text, html, guest_id=guest.id)
    return {**_profile_payload(guest, db), "mail": mail}


@router.post("/logout")
def logout(guest: Guest = Depends(current_guest), db: Session = Depends(get_db)):
    """Rigenera il token: il link ricevuto finora non vale più (utile se è stato inoltrato per sbaglio)."""
    guest.access_token = new_token()
    guest.token_created_at = now_ms()
    db.commit()
    return {"ok": True}
