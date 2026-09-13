from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import can_modify, get_client_id, is_admin
from ..deps import get_db
from ..models import BusBooking
from ..schemas import BookingIn
from ..state import bump_version

router = APIRouter(prefix="/api/bus", tags=["navetta"])


def max_seats(request: Request) -> int:
    settings = request.app.state.settings
    if settings.max_bus_seats_override:
        return settings.max_bus_seats_override
    return int(request.app.state.event_data.get("meta", {}).get("maxBusSeats", 54))


def booked_seats(db: Session) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(BusBooking.seats_count), 0))) or 0)


@router.get("/summary")
def summary(request: Request, db: Session = Depends(get_db)):
    total = max_seats(request)
    booked = booked_seats(db)
    return {"maxSeats": total, "bookedSeats": booked, "availableSeats": max(0, total - booked)}


@router.get("/bookings")
def list_bookings(db: Session = Depends(get_db)):
    rows = db.scalars(select(BusBooking).order_by(BusBooking.booked_at.desc())).all()
    return [b.to_dict() for b in rows]


@router.post("/bookings", status_code=201)
def create_booking(
    body: BookingIn,
    request: Request,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
):
    total = max_seats(request)
    booked = booked_seats(db)
    if booked + body.seatsCount > total:
        raise HTTPException(
            409, f"Posti non sufficienti (disponibili solo {max(0, total - booked)} posti)."
        )
    booking = BusBooking(
        passenger_name=body.passengerName,
        seats_count=body.seatsCount,
        pickup_stop=body.pickupStop,
        return_trip_wanted=body.returnTripWanted,
        contact_phone=body.contactPhone,
        notes=body.notes,
        owner_client_id=client_id,
    )
    db.add(booking)
    bump_version(db)
    db.commit()
    db.refresh(booking)
    return booking.to_dict()


@router.delete("/bookings/{booking_id}", status_code=204)
def delete_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
    admin: bool = Depends(is_admin),
):
    booking = db.get(BusBooking, booking_id)
    if booking is None:
        raise HTTPException(404, "Prenotazione non trovata")
    if not can_modify(booking.owner_client_id, admin, client_id):
        raise HTTPException(403, "Solo chi ha prenotato o un organizzatore può cancellare la prenotazione")
    db.delete(booking)
    bump_version(db)
    db.commit()
    return None
