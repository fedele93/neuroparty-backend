"""Cruscotto degli organizzatori (ADMIN_TOKEN): i numeri che servono a colpo d'occhio e le cose da fare."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..deadlines import deadlines_info
from ..deps import base_url_for, get_db
from ..mail import graduate_link_mail, treasurer_invite_mail
from ..models import (
    AssistantAvatar, BusBooking, EventNotification, GiftPoolAllocation, GiftPoolContribution, GiftTarget, Guest,
    MailOutbox, Meta, SharedPhoto, Wish, now_ms,
)
from ..push import subscription_count
from ..routers.bus import bus_summary
from ..routers.gifts import pool_summary
from ..routers.graduate import graduate_link
from ..routers.guests import guests_summary
from ..routers.photos import remove_photo_files
from ..seed import gift_collectors_info
from ..state import bump_version

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


# ------------------------------------------------------------------ inviti a festeggiati e cassieri


class InvitesIn(BaseModel):
    """Chi invitare: "graduates" (link #festeggiato= alla GRADUATE_EMAIL_<ID>) e/o "treasurers"
    (link #cassiere=<TREASURER_TOKEN_<ID>> alla TREASURER_EMAIL_<ID>). ids vuoto = tutti.
    dryRun = solo l'elenco di chi riceverebbe cosa; testPhase = aggiunge le istruzioni per la fase di prova."""
    roles: list[str] = ["graduates", "treasurers"]
    ids: list[str] = []
    dryRun: bool = True
    testPhase: bool = False


@router.post("/invites")
def send_invites(body: InvitesIn, request: Request, db: Session = Depends(get_db)):
    """Manda (o con dryRun elenca) gli inviti con link personale e istruzioni dettagliate a festeggiati e cassieri."""
    settings = request.app.state.settings
    mailer = request.app.state.mailer
    event_data = request.app.state.event_data
    wanted = {i.strip().lower() for i in body.ids if i.strip()}

    # Candidati omogenei: (ruolo, id, nome, email, motivo per cui non si può invitare, costruttore della mail).
    # I link dei festeggiati vengono creati senza commit: uno solo alla fine, prima di mettere in coda le email.
    candidates = []
    if "graduates" in body.roles:
        for t in db.scalars(select(GiftTarget).order_by(GiftTarget.sort_order.asc())).all():
            candidates.append(("graduate", t.id, t.name, t.email, None,
                               lambda t=t: ("graduate_link", graduate_link_mail(t, graduate_link(request, db, t, commit=False), event_data, test_phase=body.testPhase))))
    if "treasurers" in body.roles:
        for c in gift_collectors_info(event_data):
            token = settings.treasurer_tokens.get(c["id"], "")
            link = f"{base_url_for(request)}/#cassiere={token}"
            candidates.append(("treasurer", c["id"], c.get("name", ""), settings.treasurer_emails.get(c["id"], ""),
                               None if token else "NO_TOKEN",
                               lambda c=c, link=link: ("treasurer_invite", treasurer_invite_mail(c, link, event_data, test_phase=body.testPhase))))

    rows = []
    for role, rid, name, email, blocked, build in candidates:
        if wanted and rid not in wanted:
            continue
        if blocked:
            status = blocked
        elif not email:
            status = "NO_EMAIL"
        elif body.dryRun:
            status = "READY"
        elif not mailer.enabled:
            status = "MAIL_DISABLED"
        else:
            kind, (subject, text, html) = build()
            db.commit()  # il token appena creato deve essere salvato prima che l'email parta
            status = (mailer.send(kind, email, subject, text, html) or {}).get("status", "QUEUED")
        rows.append({"role": role, "id": rid, "name": name, "email": email, "status": status, "hasLink": blocked is None})
    return {"dryRun": body.dryRun, "mailEnabled": mailer.enabled, "invites": rows,
            "ready": sum(1 for r in rows if r["status"] in ("READY", "QUEUED", "DELIVERED"))}


# ------------------------------------------------------------------ azzeramento dei dati di prova


class ResetIn(BaseModel):
    confirm: str = ""


@router.post("/reset-test-data")
def reset_test_data(body: ResetIn, request: Request, db: Session = Depends(get_db)):
    """Cancella i dati raccolti durante le prove (invitati e profili, navetta, auguri, foto con i file, quote uniche,
    notifiche, email in coda) prima di aprire l'app agli invitati. Restano la configurazione dell'evento, i regali
    con le coordinate e i link dei festeggiati, gli avatar dell'assistente e le iscrizioni alle notifiche push.
    Serve confirm = "AZZERA"."""
    if body.confirm.strip().upper() != "AZZERA":
        raise HTTPException(422, 'Per confermare scrivi AZZERA')
    upload_dir = request.app.state.settings.upload_dir
    for p in db.scalars(select(SharedPhoto)).all():
        remove_photo_files(upload_dir, p)
    counts = {}
    # prima ciò che rimanda ad altro (email e prenotazioni agli invitati, ripartizioni alle quote)
    for name, model in (("mails", MailOutbox), ("busBookings", BusBooking), ("poolAllocations", GiftPoolAllocation),
                        ("poolContributions", GiftPoolContribution), ("wishes", Wish), ("photos", SharedPhoto),
                        ("notifications", EventNotification), ("guests", Guest)):
        counts[name] = db.execute(delete(model)).rowcount or 0
    # i dati di esempio non devono tornare al prossimo riavvio
    if db.get(Meta, "seeded") is None:
        db.add(Meta(key="seeded", value="1"))
    bump_version(db)
    db.commit()
    request.app.state.webhooks.emit("admin.reset", {"deleted": counts})
    return {"deleted": counts}
