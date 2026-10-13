from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import secrets

from pydantic import BaseModel

from ..auth import can_modify, can_see_lists, get_client_id, is_admin, require_admin
from ..deadlines import deadlines_info
from ..deps import get_db
from ..models import BusBooking, Guest, now_ms
from ..schemas import BookingIn
from ..state import bump_version

router = APIRouter(prefix="/api/bus", tags=["navetta"])

CONFIRMED = "CONFIRMED"
WAITLIST = "WAITLIST"


def new_code() -> str:
    """Codice prenotazione leggibile (es. NP-7K3Q), senza caratteri ambigui."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "NP-" + "".join(secrets.choice(alphabet) for _ in range(4))


def max_seats(request: Request) -> int:
    settings = request.app.state.settings
    if settings.max_bus_seats_override:
        return settings.max_bus_seats_override
    return int(request.app.state.event_data.get("meta", {}).get("maxBusSeats", 54))


def booked_seats(db: Session) -> int:
    """Posti confermati (la lista d'attesa non occupa posti)."""
    return int(db.scalar(select(func.coalesce(func.sum(BusBooking.seats_count), 0)).where(BusBooking.status != WAITLIST)) or 0)


def waitlist_seats(db: Session) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(BusBooking.seats_count), 0)).where(BusBooking.status == WAITLIST)) or 0)


def bus_summary(request: Request, db: Session) -> dict:
    total = max_seats(request)
    booked = booked_seats(db)
    info = deadlines_info(request.app.state.event_data)
    return {
        "maxSeats": total, "bookedSeats": booked, "availableSeats": max(0, total - booked),
        "waitlistSeats": waitlist_seats(db),
        "waitlistBookings": int(db.scalar(select(func.count()).select_from(BusBooking).where(BusBooking.status == WAITLIST)) or 0),
        "checkedInSeats": int(db.scalar(select(func.coalesce(func.sum(BusBooking.seats_count), 0)).where(BusBooking.checked_in_at.is_not(None))) or 0),
        "busDeadline": info["busDeadline"], "busLocked": info["busLocked"],
    }


def create_booking_record(request: Request, db: Session, body: BookingIn, client_id: str | None, guest: Guest | None = None) -> BusBooking:
    """Crea la prenotazione (confermata o in lista d'attesa se i posti non bastano e allowWaitlist).
    Usata dall'endpoint pubblico, dal profilo e dall'assistente."""
    total = max_seats(request)
    booked = booked_seats(db)
    status = CONFIRMED
    if booked + body.seatsCount > total:
        if not body.allowWaitlist:
            raise HTTPException(409, f"Posti non sufficienti (disponibili solo {max(0, total - booked)} posti).")
        status = WAITLIST
    booking = BusBooking(
        passenger_name=body.passengerName,
        seats_count=body.seatsCount,
        pickup_stop=body.pickupStop,
        return_trip_wanted=body.returnTripWanted,
        contact_phone=body.contactPhone,
        notes=body.notes,
        owner_client_id=client_id,
        guest_id=guest.id if guest else None,
        status=status,
        code=new_code(),
    )
    db.add(booking)
    bump_version(db)
    db.commit()
    db.refresh(booking)
    request.app.state.webhooks.emit("bus.booked", {"booking": booking.to_dict(), "bus": bus_summary(request, db)})
    return booking


def promote_waitlist(request: Request, db: Session) -> list[BusBooking]:
    """Quando si liberano posti, conferma le prenotazioni in lista d'attesa (le più vecchie prima,
    saltando quelle troppo grandi). Ritorna le prenotazioni promosse (la posta la manda chi chiama)."""
    total = max_seats(request)
    promoted = []
    waiting = db.scalars(select(BusBooking).where(BusBooking.status == WAITLIST).order_by(BusBooking.booked_at.asc(), BusBooking.id.asc())).all()
    for b in waiting:
        if booked_seats(db) + b.seats_count <= total:
            b.status = CONFIRMED
            b.booked_at = now_ms()
            db.commit()
            promoted.append(b)
    if promoted:
        bump_version(db)
        db.commit()
        for b in promoted:
            request.app.state.webhooks.emit("bus.promoted", {"booking": b.to_dict(), "bus": bus_summary(request, db)})
    return promoted


@router.get("/summary")
def summary(request: Request, db: Session = Depends(get_db)):
    return bus_summary(request, db)


@router.get("/bookings")
def list_bookings(
    db: Session = Depends(get_db), lists: bool = Depends(can_see_lists),
    client_id: str | None = Depends(get_client_id), x_guest_token: str | None = Header(default=None),
):
    """Prenotazioni: tutte per organizzatori, festeggiati e cassieri; gli altri solo le proprie
    (fatte da questo dispositivo o dal proprio profilo)."""
    rows = db.scalars(select(BusBooking).order_by(BusBooking.booked_at.desc())).all()
    if not lists:
        token = (x_guest_token or "").strip()
        guest = db.scalars(select(Guest).where(Guest.access_token == token)).first() if token else None
        rows = [b for b in rows if (client_id and b.owner_client_id == client_id) or (guest and b.guest_id == guest.id)]
    return [b.to_dict() for b in rows]


@router.post("/bookings", status_code=201)
def create_booking(
    body: BookingIn,
    request: Request,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
):
    return create_booking_record(request, db, body, client_id).to_dict()


@router.delete("/bookings/{booking_id}", status_code=204)
def delete_booking(
    booking_id: int,
    request: Request,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
    admin: bool = Depends(is_admin),
):
    booking = db.get(BusBooking, booking_id)
    if booking is None:
        raise HTTPException(404, "Prenotazione non trovata")
    if not can_modify(booking.owner_client_id, admin, client_id):
        raise HTTPException(403, "Solo chi ha prenotato o un organizzatore può cancellare la prenotazione")
    snapshot = booking.to_dict()
    db.delete(booking)
    bump_version(db)
    db.commit()
    request.app.state.webhooks.emit("bus.cancelled", {"booking": snapshot, "bus": bus_summary(request, db)})
    notify_promoted(request, db, promote_waitlist(request, db))
    return None


def notify_promoted(request: Request, db: Session, promoted: list[BusBooking]) -> None:
    """Email "posto confermato" a chi esce dalla lista d'attesa (se ha un profilo con email)."""
    from ..mail import bus_mail

    for b in promoted:
        guest = db.get(Guest, b.guest_id) if b.guest_id else None
        if guest and guest.email:
            subject, text, html, attachments = bus_mail("bus_promoted", guest, b, request)
            request.app.state.mailer.send("bus_promoted", guest.email, subject, text, html, guest_id=guest.id, attachments=attachments)


class CheckinIn(BaseModel):
    checkedIn: bool = True


@router.post("/bookings/{booking_id}/checkin", dependencies=[Depends(require_admin)])
def checkin(booking_id: int, body: CheckinIn, db: Session = Depends(get_db)):
    """Organizzatori, il giorno della festa: segna chi è salito sulla navetta."""
    booking = db.get(BusBooking, booking_id)
    if booking is None:
        raise HTTPException(404, "Prenotazione non trovata")
    booking.checked_in_at = now_ms() if body.checkedIn else None
    bump_version(db)
    db.commit()
    db.refresh(booking)
    return booking.to_dict()
