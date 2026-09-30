"""Caricamento del file event-data.json e popolamento iniziale del database."""
import json
import os
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .schedule import get_schedule, resolve_placeholders
from .state import bump_version
from .models import (
    BusBooking,
    EventNotification,
    GiftPoolAllocation,
    GiftPoolContribution,
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
    "giftCollector": {},
}

# Cassiere delle quote uniche: chi non vuole fare un bonifico per ogni neo-specialista versa
# a lui una cifra sola, che poi ripartisce. Metodi ammessi: bonifico, PayPal e contanti.
COLLECTOR_DEFAULTS = {
    "name": "",
    "roleTitle": "Cassiere delle quote uniche",
    "description": "",
    "iban": "",
    "ibanHolder": "",
    "paypalMeUrl": "",
    "paymentMethods": ["IBAN", "PayPal", "Contanti"],
    "transferReason": "Regalo specializzazione Neurologia",
}


def gift_collector_info(data: dict) -> dict:
    """Blocco "giftCollector" del JSON completato con i valori di default."""
    info = dict(COLLECTOR_DEFAULTS)
    info.update({k: v for k, v in (data.get("giftCollector") or {}).items() if v is not None})
    info["paymentMethods"] = [m for m in info["paymentMethods"] if m in ("IBAN", "PayPal", "Contanti")]
    return info


def load_event_file(path: str) -> dict:
    """Legge il JSON dell'evento; se manca restituisce una struttura vuota ma valida."""
    if not path or not os.path.exists(path):
        return dict(EMPTY_EVENT)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def public_event_info(data: dict) -> dict:
    """La parte 'statica' del JSON (programma, mappa, navetta...), senza i dati demo.
    I segnaposto degli orari (es. {partyTime|...}) vengono risolti qui, così i client
    ricevono testi già pronti (vedi app/schedule.py)."""
    schedule = get_schedule(data)
    return {
        "meta": data.get("meta", EMPTY_EVENT["meta"]),
        "schedule": schedule,
        "graduates": data.get("graduates", []),
        "program": resolve_placeholders(data.get("program", EMPTY_EVENT["program"]), schedule),
        "busSchedule": resolve_placeholders(data.get("busSchedule", EMPTY_EVENT["busSchedule"]), schedule),
        "mapPoints": resolve_placeholders(data.get("mapPoints", []), schedule),
        "giftCollector": gift_collector_info(data),
    }


def _age_ms(age_hours) -> int:
    return int(time.time() * 1000) - int(round(float(age_hours or 0) * 3600000))


# Campi di un regalo che il JSON può aggiornare.
GIFT_TEXT_FIELDS = {
    "name": ("name", str), "specialization": ("specialization", str), "roleTitle": ("role_title", str),
    "giftTitle": ("gift_title", str), "giftDescription": ("gift_description", str),
    "iban": ("iban", str), "ibanHolder": ("iban_holder", str),
    "satispayUrl": ("satispay_url", str), "paypalMeUrl": ("paypal_me_url", str),
}


def sync_gift_targets(db: Session, data: dict, include_demo: bool, update_texts: bool = False) -> dict:
    """Allinea i regali del database a event-data.json.
    - I regali del JSON che mancano nel database vengono inseriti (es. un nuovo neo-specialista
      aggiunto dopo il primo avvio).
    - Con update_texts=True (variabile GIFT_SYNC_UPDATE_TEXTS) anche i regali già presenti
      vengono aggiornati nei testi e negli IBAN/link.
    - I regali presenti nel database ma non più nel JSON vengono rimossi (es. il vecchio
      "regalo comune", sostituito dal cassiere delle quote uniche).
    Ritorna {"added": n, "updated": n, "removed": n}."""
    first_fill = db.scalar(select(GiftTarget).limit(1)) is None
    added = updated = removed = 0
    wanted = {t["id"] for t in data.get("giftTargets", [])}
    for stale in db.scalars(select(GiftTarget)).all():
        if stale.id not in wanted:
            db.delete(stale)
            removed += 1
    for i, t in enumerate(data.get("giftTargets", [])):
        existing = db.get(GiftTarget, t["id"])
        if existing is not None:
            if update_texts:
                changed = False
                for json_key, (attr, cast) in GIFT_TEXT_FIELDS.items():
                    new_value = cast(t.get(json_key, ""))
                    if getattr(existing, attr) != new_value:
                        setattr(existing, attr, new_value)
                        changed = True
                if existing.sort_order != i:
                    existing.sort_order = i
                    changed = True
                updated += int(changed)
            continue
        db.add(
            GiftTarget(
                id=t["id"],
                name=t.get("name", ""),
                specialization=t.get("specialization", ""),
                role_title=t.get("roleTitle", ""),
                gift_title=t.get("giftTitle", ""),
                gift_description=t.get("giftDescription", ""),
                iban=t.get("iban", ""),
                iban_holder=t.get("ibanHolder", ""),
                satispay_url=t.get("satispayUrl", ""),
                paypal_me_url=t.get("paypalMeUrl", ""),
                sort_order=i,
            )
        )
        added += 1
    if (added and not first_fill) or updated or removed:
        bump_version(db)  # i client in polling ricaricano lo snapshot e vedono le modifiche
    if added or updated or removed:
        db.commit()
    return {"added": added, "updated": updated, "removed": removed}


def seed_database(db: Session, data: dict, include_demo: bool, update_gift_texts: bool = False) -> None:
    """Popola le tabelle vuote. I regali (configurazione) vengono sempre allineati al JSON
    (aggiunti se mancanti, anche su un database già avviato; testi aggiornati solo con
    update_gift_texts); invitati, prenotazioni, auguri, foto, quote uniche e notifiche solo se
    include_demo e solo al primo avvio."""
    sync_gift_targets(db, data, include_demo, update_texts=update_gift_texts)

    if db.get(Meta, "seeded"):
        return

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
        names = {t["id"]: t.get("name", "") for t in data.get("giftTargets", [])}
        for c in data.get("giftPoolContributions", []):
            allocations = [
                GiftPoolAllocation(
                    graduate_id=a["graduateId"],
                    graduate_name=names.get(a["graduateId"], ""),
                    amount=round(float(a.get("amount", 0)), 2),
                )
                for a in c.get("allocations", [])
            ]
            created = _age_ms(c.get("ageHours"))
            received = c.get("status", "PENDING") == "RECEIVED"
            db.add(
                GiftPoolContribution(
                    donor_name=c["donorName"],
                    contact=c.get("contact", ""),
                    payment_method=c.get("paymentMethod", "IBAN"),
                    split_mode=c.get("splitMode", "EQUAL"),
                    total_amount=round(sum(a.amount for a in allocations), 2),
                    note=c.get("note", ""),
                    status="RECEIVED" if received else "PENDING",
                    created_at=created,
                    received_at=created if received else None,
                    allocations=allocations,
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
