"""Report riassuntivo per gli organizzatori, pensato per essere spedito da n8n (es. mail mattutina).

build_report raccoglie in un solo dizionario tutto ciò che serve per un aggiornamento:
coperti confermati e menu speciali, posti navetta, quote uniche per il cassiere, novità delle ultime ore,
notifiche programmate. render_text / render_html lo trasformano nel corpo di una mail, così in
n8n basta un nodo HTTP Request + un nodo Send Email (vedi n8n/ nel repo).
"""
from __future__ import annotations

import html
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    BusBooking,
    EventNotification,
    GiftPoolContribution,
    GiftTarget,
    Guest,
    PushSubscription,
    SharedPhoto,
    Wish,
    now_ms,
)
from .routers.gifts import pool_summary
from .routers.guests import _NO_DIET, guests_summary
from .schedule import get_schedule
from .state import get_version

try:  # Europe/Rome se il sistema ha i dati dei fusi orari, altrimenti ora locale del server
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo("Europe/Rome")
except Exception:  # noqa: BLE001
    _TZ = None


def _fmt_ts(ms: int | None) -> str:
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return (dt.astimezone(_TZ) if _TZ else dt.astimezone()).strftime("%d/%m/%Y %H:%M")


def _fmt_eur(value: float) -> str:
    s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _days_to(iso_date: str, today: date | None = None) -> int | None:
    try:
        target = date.fromisoformat(iso_date)
    except (TypeError, ValueError):
        return None
    return (target - (today or date.today())).days


def build_report(
    db: Session,
    event_data: dict,
    *,
    max_seats: int,
    base_url: str = "",
    since_hours: float = 24,
    now: int | None = None,
) -> dict:
    now = now if now is not None else now_ms()
    since = now - int(since_hours * 3_600_000)
    sch = get_schedule(event_data)
    program = event_data.get("program") or {}

    guests = db.scalars(select(Guest).order_by(Guest.full_name.asc())).all()
    bookings = db.scalars(select(BusBooking).order_by(BusBooking.booked_at.desc())).all()
    targets = db.scalars(select(GiftTarget).order_by(GiftTarget.sort_order.asc(), GiftTarget.id.asc())).all()
    pool = db.scalars(select(GiftPoolContribution).order_by(GiftPoolContribution.created_at.desc())).all()
    wishes = db.scalars(select(Wish).order_by(Wish.created_at.desc())).all()
    photos = db.scalars(select(SharedPhoto).order_by(SharedPhoto.created_at.desc())).all()
    scheduled = db.scalars(
        select(EventNotification)
        .where(EventNotification.scheduled_at.is_not(None))
        .order_by(EventNotification.scheduled_at.asc())
    ).all()
    published_count = int(
        db.scalar(
            select(func.count()).select_from(EventNotification).where(EventNotification.scheduled_at.is_(None))
        ) or 0
    )
    push_count = int(db.scalar(select(func.count()).select_from(PushSubscription)) or 0)

    booked = sum(b.seats_count for b in bookings)
    summary = guests_summary(guests)
    summary["totalGuests"] = len(guests)

    recent_guests = [g for g in guests if (g.updated_at or 0) >= since]
    recent_bookings = [b for b in bookings if (b.booked_at or 0) >= since]
    recent_wishes = [w for w in wishes if (w.created_at or 0) >= since]
    recent_photos = [p for p in photos if (p.created_at or 0) >= since]
    recent_pool = [c for c in pool if (c.created_at or 0) >= since]

    return {
        "generatedAt": now,
        "generatedAtText": _fmt_ts(now),
        "sinceHours": since_hours,
        "version": get_version(db),
        "event": {
            "title": program.get("title") or "NeuroParty",
            "ceremonyDate": sch["ceremonyDate"],
            "partyDate": sch["partyDate"],
            "partyTime": sch["partyTime"],
            "daysToCeremony": _days_to(sch["ceremonyDate"]),
            "daysToParty": _days_to(sch["partyDate"]),
            "url": base_url,
        },
        "guests": summary,
        "bus": {
            "maxSeats": max_seats,
            "bookedSeats": booked,
            "availableSeats": max(0, max_seats - booked),
            "bookings": len(bookings),
            "returnTripSeats": sum(b.seats_count for b in bookings if b.return_trip_wanted),
            "byStop": _by_stop(bookings),
        },
        # quote uniche versate al cassiere (le donazioni dirette ai neo-specialisti non passano dal server)
        "gifts": {**pool_summary(pool, targets), "graduates": len(targets)},
        "wishes": {"count": len(wishes), "hearts": sum(w.heart_count for w in wishes)},
        "photos": {"count": len(photos), "likes": sum(p.likes_count for p in photos)},
        "notifications": {
            "published": published_count,
            "scheduled": [
                {
                    "id": n.id, "title": n.title, "category": n.category,
                    "scheduledAt": n.scheduled_at, "scheduledAtText": _fmt_ts(n.scheduled_at),
                }
                for n in scheduled
            ],
        },
        "push": {"subscriptions": push_count},
        "recent": {
            "guests": [g.to_dict() for g in recent_guests],
            "busBookings": [b.to_dict() for b in recent_bookings],
            "wishes": [w.to_dict() for w in recent_wishes],
            "photos": [p.to_dict(base_url) for p in recent_photos],
            "giftPool": [c.to_dict() for c in recent_pool],
            "counts": {
                "guests": len(recent_guests),
                "busBookings": len(recent_bookings),
                "wishes": len(recent_wishes),
                "photos": len(recent_photos),
                "giftPool": len(recent_pool),
            },
        },
    }


def _by_stop(bookings: list[BusBooking]) -> list[dict]:
    stops: dict[str, dict] = {}
    for b in bookings:
        stop = b.pickup_stop or "Fermata non indicata"
        s = stops.setdefault(stop, {"stop": stop, "seats": 0, "bookings": 0})
        s["seats"] += b.seats_count
        s["bookings"] += 1
    return sorted(stops.values(), key=lambda s: (-s["seats"], s["stop"]))


RSVP_LABELS = {"CONFIRMED": "confermato", "PENDING": "in attesa", "DECLINED": "declinato"}


def _lines(report: dict) -> list[tuple[str, list[str]]]:
    """Sezioni (titolo, righe) comuni al formato testo e HTML."""
    ev, g, bus, gifts, notif, rec = (
        report["event"], report["guests"], report["bus"], report["gifts"], report["notifications"], report["recent"],
    )
    head = []
    if ev.get("daysToParty") is not None:
        d = ev["daysToParty"]
        if d == 0:
            head.append("La festa è oggi! 🎉")
        elif d > 0:
            head.append(f"Mancano {d} giorni alla festa ({ev['partyDate']}).")
        else:
            head.append("La festa si è già svolta.")
    if ev.get("daysToCeremony") is not None and ev["daysToCeremony"] >= 0:
        head.append(f"Seduta di specializzazione tra {ev['daysToCeremony']} giorni ({ev['ceremonyDate']}).")

    guests_lines = [
        f"Coperti confermati: {g['covers']} ({g['confirmedGuests']} invitati); "
        f"in attesa: {g['pendingCovers']} coperti; declinati: {g['declinedGuests']}.",
    ]
    if g["byCategory"]:
        guests_lines.append("Per categoria: " + ", ".join(f"{c['category']} {c['covers']}" for c in g["byCategory"]) + ".")
    if g["dietary"]:
        diets = "; ".join(f"{d['note']} ({d['covers']}: {', '.join(d['guests'])})" for d in g["dietary"])
        guests_lines.append(f"Esigenze alimentari: {diets}.")
    else:
        guests_lines.append("Nessuna esigenza alimentare segnalata.")

    bus_lines = [
        f"Posti prenotati: {bus['bookedSeats']} su {bus['maxSeats']} (liberi {bus['availableSeats']}), "
        f"{bus['bookings']} prenotazioni, {bus['returnTripSeats']} posti con ritorno."
    ]
    if bus["byStop"]:
        bus_lines.append("Per fermata: " + ", ".join(f"{s['stop']} {s['seats']}" for s in bus["byStop"]) + ".")

    gift_lines = [
        f"Quote uniche al cassiere: {gifts['contributions']} per {_fmt_eur(gifts['totalAmount'])} "
        f"(ricevute {gifts['received']} per {_fmt_eur(gifts['receivedAmount'])}, "
        f"in attesa {_fmt_eur(gifts['pendingAmount'])})."
    ]
    for t in gifts["byGraduate"]:
        if t["contributions"]:
            gift_lines.append(f"{t['graduateName']}: {_fmt_eur(t['amount'])} da {_plural(t['contributions'], 'quota', 'quote')}")
    gift_lines.append("Le donazioni dirette ai neo-specialisti non passano dal server.")

    c = rec["counts"]
    recent_lines = []
    if not any(c.values()):
        recent_lines.append(f"Nessuna novità nelle ultime {report['sinceHours']:g} ore.")
    else:
        for gu in rec["guests"]:
            diet = gu["dietaryNotes"] if gu["dietaryNotes"].strip().lower() not in _NO_DIET else ""
            status = RSVP_LABELS.get(gu["rsvpStatus"], gu["rsvpStatus"])
            recent_lines.append(
                f"RSVP {status}: {gu['fullName']} ({gu['category']}, {gu['guestsCount']} pers.)" + (f" - {diet}" if diet else "")
            )
        for b in rec["busBookings"]:
            seats = _plural(b["seatsCount"], "posto", "posti")
            recent_lines.append(f"Navetta: {b['passengerName']}, {seats}, {b['pickupStop'] or 'fermata da indicare'}")
        for k in rec["giftPool"]:
            recent_lines.append(
                f"Quota unica: {k['donorName']} {_fmt_eur(k['totalAmount'])} ({k['paymentMethod']}) "
                f"per {_plural(len(k['allocations']), 'neo-specialista', 'neo-specialisti')}"
            )
        for w in rec["wishes"]:
            recent_lines.append(f"Augurio di {w['authorName']} per {w['targetGraduate']}: \"{w['message'][:120]}\"")
        for p in rec["photos"]:
            recent_lines.append(f"Foto di {p['authorName']}" + (f": {p['caption']}" if p["caption"] else ""))

    notif_lines = [
        f"Notifiche pubblicate: {notif['published']}; dispositivi iscritti alle push: {report['push']['subscriptions']}."
    ]
    notif_lines += [
        f"Programmata per {n['scheduledAtText']}: {n['title']} [{n['category']}]" for n in notif["scheduled"]
    ] or ["Nessuna notifica programmata."]

    other = [
        f"Auguri in bacheca: {report['wishes']['count']} ({report['wishes']['hearts']} cuori); "
        f"foto: {report['photos']['count']} ({report['photos']['likes']} like)."
    ]

    return [
        ("", head),
        ("Invitati", guests_lines),
        ("Navetta", bus_lines),
        ("Regali", gift_lines),
        (f"Novità nelle ultime {report['sinceHours']:g} ore", recent_lines),
        ("Notifiche", notif_lines),
        ("Bacheca e galleria", other),
    ]


def render_text(report: dict) -> str:
    out = [f"{report['event']['title']} - report del {report['generatedAtText']}", ""]
    for title, lines in _lines(report):
        if not lines:
            continue
        if title:
            out += [title.upper(), "-" * len(title)]
        out += [("- " + ln if title else ln) for ln in lines]
        out.append("")
    if report["event"].get("url"):
        out.append(f"App: {report['event']['url']}")
    return "\n".join(out).rstrip() + "\n"


def render_html(report: dict) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html lang=\"it\"><head><meta charset=\"utf-8\">",
        f"<title>{e(report['event']['title'])} - report</title></head>",
        "<body style=\"font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:640px;"
        "margin:0 auto;padding:16px;color:#1f2937;line-height:1.45\">",
        f"<h1 style=\"font-size:20px;margin:0 0 4px\">{e(report['event']['title'])}</h1>",
        f"<p style=\"margin:0 0 16px;color:#6b7280\">Report del {e(report['generatedAtText'])}</p>",
    ]
    for title, lines in _lines(report):
        if not lines:
            continue
        if title:
            parts.append(
                "<h2 style=\"font-size:15px;margin:18px 0 6px;border-bottom:1px solid #e5e7eb;padding-bottom:4px\">"
                f"{e(title)}</h2>"
            )
            parts.append("<ul style=\"margin:0;padding-left:18px\">" + "".join(f"<li>{e(ln)}</li>" for ln in lines) + "</ul>")
        else:
            parts.append("".join(f"<p style=\"margin:0 0 6px;font-weight:600\">{e(ln)}</p>" for ln in lines))
    if report["event"].get("url"):
        u = e(report["event"]["url"])
        parts.append(f"<p style=\"margin-top:20px;font-size:13px;color:#6b7280\">App: <a href=\"{u}\">{u}</a></p>")
    parts.append("</body></html>")
    return "".join(parts)
