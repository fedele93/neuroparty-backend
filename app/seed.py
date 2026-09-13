"""Caricamento del file event-data.json e popolamento iniziale del database."""
import json
import os
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    BusBooking,
    EventNotification,
    GiftContribution,
    GiftTarget,
    Guest,
    Meta,
    SharedPhoto,
    Wish,
)

EMPTY_EVENT = {
    "meta": {"maxBusSeats": 54},
    "graduates": [],
    "program": {"badge": "", "title": "", "subtitle": "", "dateLabel": "", "locationLabel": "", "timeline": []},
    "busSchedule": {"subtitle": "", "andata": {}, "ritorno": {}, "pickupStops": []},
    "mapPoints": [],
}


def load_event_file(path: str) -> dict:
    """Legge il JSON dell'evento; se manca restituisce una struttura vuota ma valida."""
    if not path or not os.path.exists(path):
        return dict(EMPTY_EVENT)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def public_event_info(data: dict) -> dict:
    """La parte 'statica' del JSON (programma, mappa, navetta...), senza i dati demo."""
    return {
        "meta": data.get("meta", EMPTY_EVENT["meta"]),
        "graduates": data.get("graduates", []),
        "program": data.get("program", EMPTY_EVENT["program"]),
        "busSchedule": data.get("busSchedule", EMPTY_EVENT["busSchedule"]),
        "mapPoints": data.get("mapPoints", []),
    }


def _age_ms(age_hours) -> int:
    return int(time.time() * 1000) - int(round(float(age_hours or 0) * 3600000))


def seed_database(db: Session, data: dict, include_demo: bool) -> None:
    """Popola le tabelle vuote. I regali (configurazione) vengono sempre inseriti;
    invitati, prenotazioni, auguri, foto, contributi e notifiche solo se include_demo."""
    if db.get(Meta, "seeded"):
        return

    if db.scalar(select(GiftTarget).limit(1)) is None:
        for i, t in enumerate(data.get("giftTargets", [])):
            db.add(
                GiftTarget(
                    id=t["id"],
                    name=t.get("name", ""),
                    specialization=t.get("specialization", ""),
                    role_title=t.get("roleTitle", ""),
                    gift_title=t.get("giftTitle", ""),
                    gift_description=t.get("giftDescription", ""),
                    target_amount=float(t.get("targetAmount", 0)),
                    collected_amount=float(t.get("collectedAmount", 0)) if include_demo else 0.0,
                    iban=t.get("iban", ""),
                    iban_holder=t.get("ibanHolder", ""),
                    satispay_url=t.get("satispayUrl", ""),
                    paypal_me_url=t.get("paypalMeUrl", ""),
                    sort_order=i,
                )
            )

    if include_demo:
        for g in data.get("guests", []):
            db.add(
                Guest(
                    full_name=g["fullName"],
                    category=g.get("category", "Invitato"),
                    rsvp_status=g.get("rsvpStatus", "CONFIRMED"),
                    guests_count=int(g.get("guestsCount", 1)),
                    dietary_notes=g.get("dietaryNotes", ""),
                    contact_info=g.get("contactInfo", ""),
                )
            )
        for b in data.get("busBookings", []):
            db.add(
                BusBooking(
                    passenger_name=b["passengerName"],
                    seats_count=int(b.get("seatsCount", 1)),
                    pickup_stop=b.get("pickupStop", ""),
                    return_trip_wanted=bool(b.get("returnTripWanted", True)),
                    contact_phone=b.get("contactPhone", ""),
                    notes=b.get("notes", ""),
                )
            )
        for w in data.get("wishes", []):
            db.add(
                Wish(
                    author_name=w["authorName"],
                    target_graduate=w.get("targetGraduate", "Tutti i Laureandi"),
                    message=w["message"],
                    emoji_badge=w.get("emojiBadge", "🎓"),
                    heart_count=int(w.get("heartCount", 0)),
                    created_at=_age_ms(w.get("ageHours")),
                )
            )
        for p in data.get("photos", []):
            db.add(
                SharedPhoto(
                    author_name=p["authorName"],
                    caption=p.get("caption", ""),
                    image_res_id=int(p.get("imageResId", 0)),
                    image_path="",
                    likes_count=int(p.get("likesCount", 0)),
                    created_at=_age_ms(p.get("ageHours")),
                )
            )
        for c in data.get("giftContributions", []):
            db.add(
                GiftContribution(
                    donor_name=c["donorName"],
                    target_graduate_id=c["targetGraduateId"],
                    target_graduate_name=c.get("targetGraduateName", ""),
                    amount=float(c.get("amount", 0)),
                    payment_method=c.get("paymentMethod", "IBAN"),
                    note=c.get("note", ""),
                    is_anonymous=bool(c.get("isAnonymous", False)),
                    contributed_at=_age_ms(c.get("ageHours")),
                )
            )
        for n in data.get("notifications", []):
            db.add(
                EventNotification(
                    title=n["title"],
                    message=n["message"],
                    category=n.get("category", "Organizzazione"),
                    timestamp=_age_ms(n.get("ageHours")),
                )
            )

    db.add(Meta(key="seeded", value="1"))
    if db.get(Meta, "version") is None:
        db.add(Meta(key="version", value="1"))
    db.commit()
