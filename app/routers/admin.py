"""Cruscotto degli organizzatori (ADMIN_TOKEN): i numeri che servono a colpo d'occhio e le cose da fare."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..deadlines import deadlines_info
from ..deps import get_db
from ..models import AssistantAvatar, BusBooking, GiftPoolContribution, GiftTarget, Guest, MailOutbox, SharedPhoto, Wish, now_ms
from ..push import subscription_count
from ..routers.bus import bus_summary
from ..routers.gifts import pool_summary
from ..routers.guests import guests_summary
from ..seed import gift_collectors_info

router = APIRouter(prefix="/api/admin", tags=["organizzatori"], dependencies=[Depends(require_admin)])


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    guests = db.scalars(select(Guest).order_by(Guest.full_name.asc())).all()
    summary = guests_summary(guests)
    confirmed = [g for g in guests if g.rsvp_status == "CONFIRMED"]
    with_profile = [g for g in guests if g.access_token]
    mail_counts = {s: int(n) for s, n in db.execute(select(MailOutbox.status, func.count()).group_by(MailOutbox.status)).all()}
    failed_mails = db.scalars(select(MailOutbox).where(MailOutbox.status == "FAILED").order_by(MailOutbox.created_at.desc()).limit(10)).all()
    bus = bus_summary(request, db)
    bookings = db.scalars(select(BusBooking)).all()
    targets = db.scalars(select(GiftTarget).order_by(GiftTarget.sort_order.asc(), GiftTarget.id.asc())).all()
    pool = pool_summary(db.scalars(select(GiftPoolContribution)).all(), targets)
    svc = request.app.state.assistant
    avatars = db.scalars(select(AssistantAvatar)).all()
    ready = [a for a in avatars if a.is_ready(svc.voices.fallback_available)]
    mailer = request.app.state.mailer
    deadlines = deadlines_info(request.app.state.event_data)
    week_ago = now_ms() - 7 * 86_400_000

    todo: list[dict] = []
    if summary["pendingGuests"]:
        todo.append({"key": "pending", "text": f"{summary['pendingGuests']} invitati ancora in attesa di risposta", "section": "rsvp"})
    if summary["missingMenu"] and not deadlines["menuLocked"]:
        todo.append({"key": "menu", "text": f"{len(summary['missingMenu'])} persone senza menu: manda il promemoria", "action": "remind_menu"})
    no_profile = [g for g in confirmed if not g.access_token and not g.email]
    if no_profile:
        todo.append({"key": "no_profile", "text": f"{len(no_profile)} confermati senza email né profilo: raccogli le email o importa la lista", "action": "import"})
    if bus["waitlistBookings"]:
        todo.append({"key": "waitlist", "text": f"{bus['waitlistBookings']} prenotazioni in lista d'attesa per la navetta", "section": "bus"})
    if mail_counts.get("FAILED"):
        todo.append({"key": "mail_failed", "text": f"{mail_counts['FAILED']} email non consegnate: rimandale dal pannello Email", "action": "mail"})
    if not mailer.enabled:
        todo.append({"key": "mail_off", "text": "Email disattivate: imposta N8N_WEBHOOK_URL sul server", "action": "mail"})
    if pool["pending"]:
        todo.append({"key": "pool", "text": f"{pool['pending']} quote uniche in attesa dai cassieri", "section": "gifts"})
    if avatars and not ready:
        todo.append({"key": "assistant", "text": "Nessun avatar dell'assistente è pronto", "action": "assistant"})

    return {
        "generatedAt": now_ms(),
        "guests": {
            "confirmed": summary["confirmedGuests"], "covers": summary["covers"], "pending": summary["pendingGuests"],
            "pendingCovers": summary["pendingCovers"], "declined": summary["declinedGuests"], "total": len(guests),
            "withProfile": len(with_profile), "withEmail": sum(1 for g in guests if g.email),
            "consented": sum(1 for g in guests if g.consent_at), "missingMenu": len(summary["missingMenu"]),
            "byMenu": summary["byMenu"], "allergies": len(summary["allergies"]),
            "updatedLastWeek": sum(1 for g in guests if (g.updated_at or 0) >= week_ago),
        },
        "bus": {**bus, "bookings": len(bookings), "withEmail": sum(1 for b in bookings if b.guest_id)},
        "mail": {"enabled": mailer.enabled, "counts": mail_counts, "failed": [m.to_dict() for m in failed_mails]},
        # quote uniche: solo conteggi, mai cifre (le vedono soltanto i cassieri con il loro token)
        "pool": {"contributions": pool["contributions"], "pending": pool["pending"], "received": pool["received"],
                 "collectors": [{"id": c["id"], "name": c["name"]} for c in gift_collectors_info(request.app.state.event_data)]},
        "wishes": int(db.scalar(select(func.count()).select_from(Wish)) or 0),
        "photos": int(db.scalar(select(func.count()).select_from(SharedPhoto)) or 0),
        "pushSubscriptions": subscription_count(db),
        "assistant": {"configured": svc.client.enabled, "avatars": len(avatars), "ready": len(ready)},
        "deadlines": deadlines,
        "todo": todo,
    }
