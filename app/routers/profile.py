"""Profilo personale dell'invitato (utenza leggera, senza password).

- POST /api/profile/request-link   {email, fullName?}  -> email con il link personale (crea l'invitato se non c'è)
- GET  /api/profile                 X-Guest-Token       -> il proprio profilo
- PUT  /api/profile                 X-Guest-Token       -> aggiorna RSVP, contatti, accompagnatori e menu; email di conferma
- POST /api/profile/logout          X-Guest-Token       -> rigenera il token (i link vecchi smettono di valere)
- POST /api/profile/verify-code     {email, code}       -> il token, dato il codice a 6 cifre spedito con il link

Il codice serve a chi ha aggiunto l'app alla schermata Home dell'iPhone: lì il link dell'email si apre
in Safari, che non condivide la memoria con l'app installata, quindi il token non arriverebbe mai
dentro l'app. Il codice si digita nell'app e vale LOGIN_CODE_MINUTES minuti, per pochi tentativi.

Il link è https://<app>/#profilo=<token>: la PWA lo legge, salva il token sul dispositivo e da
quel momento manda X-Guest-Token. Gli organizzatori generano e inviano i link da /api/guests.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deadlines import deadline_passed, deadlines_info
from ..deps import base_url_for, get_db
from ..mail import LOGIN_CODE_MINUTES, access_link_mail, bus_mail, rsvp_confirmation_mail
from ..models import BusBooking, Guest, now_ms
from ..routers import bus as bus_router
from ..routers.guests import guests_summary
from ..schemas import BookingIn, ProfileBusIn, ProfileCodeVerify, ProfileIn, ProfileLinkRequest
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


LOGIN_CODE_MAX_ATTEMPTS = 5


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def new_login_code(guest: Guest) -> str:
    """Nuovo codice a 6 cifre per l'invitato: nel database resta solo l'hash, con scadenza e tentativi azzerati."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    guest.login_code_hash = _code_hash(code)
    guest.login_code_expires_at = now_ms() + LOGIN_CODE_MINUTES * 60_000
    guest.login_code_attempts = 0
    return code


def clear_login_code(guest: Guest) -> None:
    guest.login_code_hash = ""
    guest.login_code_expires_at = None
    guest.login_code_attempts = 0


def send_access_link(request: Request, db: Session, guest: Guest) -> dict | None:
    """Email con il link personale e il codice a 6 cifre (richiede un'email nel profilo). Ritorna la riga
    dell'outbox o None."""
    if not guest.email:
        return None
    token = ensure_token(guest)
    code = new_login_code(guest)
    db.commit()
    subject, text, html = access_link_mail(guest, profile_link(request, token), request.app.state.event_data, code=code)
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


def own_booking(db: Session, guest: Guest) -> BusBooking | None:
    return db.scalars(select(BusBooking).where(BusBooking.guest_id == guest.id).order_by(BusBooking.id.asc())).first()


def _profile_payload(guest: Guest, db: Session, request: Request) -> dict:
    booking = own_booking(db, guest)
    return {
        "guest": guest.to_dict(private=True),
        "busBooking": booking.to_dict() if booking else None,
        "bus": bus_router.bus_summary(request, db),
        "deadlines": deadlines_info(request.app.state.event_data),
        "summary": {"covers": guests_summary(db.scalars(select(Guest)).all())["covers"]},
    }


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


@router.post("/verify-code")
def verify_code(body: ProfileCodeVerify, db: Session = Depends(get_db)):
    """Scambia email + codice a 6 cifre con il token del profilo (stesso token del link). Il codice vale
    una volta sola: giusto o esaurito, si cancella. La risposta non rivela se l'email esiste."""
    bad = HTTPException(401, "Codice non valido o scaduto: richiedi un nuovo link con la tua email")
    guest = find_by_email(db, body.email)
    if guest is None or not guest.login_code_hash or len(body.code) != 6:
        raise bad
    if (guest.login_code_expires_at or 0) < now_ms():
        clear_login_code(guest)
        db.commit()
        raise bad
    if not hmac.compare_digest(guest.login_code_hash, _code_hash(body.code)):
        guest.login_code_attempts += 1
        if guest.login_code_attempts >= LOGIN_CODE_MAX_ATTEMPTS:
            clear_login_code(guest)
        db.commit()
        raise bad
    token = ensure_token(guest)
    clear_login_code(guest)
    guest.last_login_at = now_ms()
    db.commit()
    return {"token": token}


@router.get("")
def my_profile(request: Request, guest: Guest = Depends(current_guest), db: Session = Depends(get_db)):
    guest.last_login_at = now_ms()
    db.commit()
    return _profile_payload(guest, db, request)


@router.put("")
def update_profile(body: ProfileIn, request: Request, guest: Guest = Depends(current_guest), db: Session = Depends(get_db)):
    if guest.consent_at is None and not body.consent:
        raise HTTPException(422, "Per salvare il profilo serve il consenso al trattamento dei dati")
    companions = [c.model_dump() for c in body.companions if c.name.strip()]
    for c in companions:
        c["name"] = c["name"].strip()
        c["notes"] = c["notes"].strip()
    # dopo la data limite del menu, menu/accompagnatori/allergie e partecipazione sono bloccati (gli organizzatori possono ancora)
    if deadline_passed(request.app.state.event_data, "menuDeadline"):
        food_changed = (body.menu != guest.menu or body.dietaryNotes != guest.dietary_notes or body.rsvpStatus != guest.rsvp_status
                        or json.dumps(companions, ensure_ascii=False) != json.dumps(guest.companions, ensure_ascii=False))
        if food_changed:
            raise HTTPException(409, "Il termine per modificare partecipazione, menu e accompagnatori è passato: per cambiamenti contatta gli organizzatori")
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
    return {**_profile_payload(guest, db, request), "mail": mail}


# ---------------------------------------------------------------------- navetta dal profilo


def _send_bus_mail(request: Request, guest: Guest, booking: BusBooking, kind: str) -> dict | None:
    if not guest.email:
        return None
    subject, text, html, attachments = bus_mail(kind, guest, booking, request)
    return request.app.state.mailer.send(kind, guest.email, subject, text, html, guest_id=guest.id, attachments=attachments)


@router.put("/bus")
def set_bus(body: ProfileBusIn, request: Request, guest: Guest = Depends(current_guest), db: Session = Depends(get_db)):
    """Prenota o aggiorna la navetta dal profilo: una sola prenotazione per invitato, posti =
    persone del profilo se non indicati, lista d'attesa a posti esauriti, email di conferma con calendario."""
    if deadline_passed(request.app.state.event_data, "busDeadline"):
        raise HTTPException(409, "Il termine per prenotare o modificare la navetta è passato: contatta gli organizzatori")
    if guest.rsvp_status == "DECLINED":
        raise HTTPException(409, "Hai indicato che non parteciperai: conferma prima la partecipazione")
    seats = body.seatsCount or max(1, guest.guests_count)
    existing = own_booking(db, guest)
    if existing is not None:
        # modifica: libera i posti attuali e verifica la capienza sui nuovi
        total = bus_router.max_seats(request)
        others = bus_router.booked_seats(db) - (existing.seats_count if existing.status == bus_router.CONFIRMED else 0)
        existing.seats_count = seats
        existing.pickup_stop = body.pickupStop.strip()
        existing.return_trip_wanted = body.returnTripWanted
        existing.contact_phone = body.contactPhone.strip() or guest.phone
        existing.notes = body.notes.strip()
        existing.passenger_name = guest.full_name
        existing.status = bus_router.CONFIRMED if others + seats <= total else bus_router.WAITLIST
        bump_version(db)
        db.commit()
        db.refresh(existing)
        bus_router.notify_promoted(request, db, bus_router.promote_waitlist(request, db))
        db.refresh(existing)
        booking = existing
    else:
        req = BookingIn(passengerName=guest.full_name, seatsCount=seats, pickupStop=body.pickupStop, returnTripWanted=body.returnTripWanted,
                        contactPhone=body.contactPhone or guest.phone, notes=body.notes, allowWaitlist=True)
        booking = bus_router.create_booking_record(request, db, req, None, guest=guest)
    kind = "bus_waitlist" if booking.status == bus_router.WAITLIST else "bus_confirmation"
    mail = _send_bus_mail(request, guest, booking, kind)
    return {**_profile_payload(guest, db, request), "mail": mail}


@router.delete("/bus")
def cancel_bus(request: Request, guest: Guest = Depends(current_guest), db: Session = Depends(get_db)):
    """Annulla la propria prenotazione (i posti liberati vanno alla lista d'attesa)."""
    existing = own_booking(db, guest)
    if existing is None:
        raise HTTPException(404, "Nessuna prenotazione da annullare")
    if deadline_passed(request.app.state.event_data, "busDeadline"):
        raise HTTPException(409, "Il termine per modificare la navetta è passato: contatta gli organizzatori")
    snapshot = existing.to_dict()
    db.delete(existing)
    bump_version(db)
    db.commit()
    request.app.state.webhooks.emit("bus.cancelled", {"booking": snapshot, "bus": bus_router.bus_summary(request, db)})
    bus_router.notify_promoted(request, db, bus_router.promote_waitlist(request, db))

    class _Snap:  # per il modello dell'email di annullamento
        code, seats_count, pickup_stop, return_trip_wanted, id = snapshot["code"], snapshot["seatsCount"], snapshot["pickupStop"], snapshot["returnTripWanted"], snapshot["id"]

    mail = _send_bus_mail(request, guest, _Snap(), "bus_cancelled")
    return {**_profile_payload(guest, db, request), "mail": mail}


@router.post("/logout")
def logout(guest: Guest = Depends(current_guest), db: Session = Depends(get_db)):
    """Rigenera il token: il link ricevuto finora non vale più (utile se è stato inoltrato per sbaglio)."""
    guest.access_token = new_token()
    guest.token_created_at = now_ms()
    clear_login_code(guest)
    db.commit()
    return {"ok": True}
