from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import can_modify, can_see_lists, get_client_id, is_admin, require_admin, require_lists
from ..deps import get_db
from ..models import Guest, now_ms
from ..schemas import GuestIn, GuestUpdate, ImportIn
from ..state import bump_version

router = APIRouter(prefix="/api/guests", tags=["invitati"])


@router.get("")
def list_guests(
    db: Session = Depends(get_db), admin: bool = Depends(is_admin), lists: bool = Depends(can_see_lists),
    client_id: str | None = Depends(get_client_id), x_guest_token: str | None = Header(default=None),
):
    """Lista invitati: completa per organizzatori, festeggiati e cassieri (con X-Admin-Token anche
    email, telefono e consenso); gli altri vedono solo i record creati da questo dispositivo o il proprio profilo."""
    rows = db.scalars(select(Guest).order_by(Guest.full_name.asc())).all()
    if not lists:
        token = (x_guest_token or "").strip()
        rows = [g for g in rows if (client_id and g.owner_client_id == client_id) or (token and g.access_token == token)]
    return [g.to_dict(private=admin) for g in rows]


# Note alimentari che significano "nessuna esigenza" (non vanno nel riepilogo per il catering)
_NO_DIET = {"", "nessuna", "nessuna restrizione", "no", "-", "niente", "nessuno"}


def guests_summary(guests: list[Guest]) -> dict:
    """Numeri per il catering: coperti confermati, per categoria ed esigenze alimentari."""
    confirmed = [g for g in guests if g.rsvp_status == "CONFIRMED"]
    pending = [g for g in guests if g.rsvp_status == "PENDING"]
    declined = [g for g in guests if g.rsvp_status == "DECLINED"]
    by_category: dict[str, dict] = {}
    for g in confirmed:
        cat = by_category.setdefault(g.category or "Invitato", {"category": g.category or "Invitato", "guests": 0, "covers": 0})
        cat["guests"] += 1
        cat["covers"] += g.guests_count
    # menu scelti dal profilo (invitato + accompagnatori); chi non ha scelto conta come "da scegliere"
    by_menu: dict[str, int] = {}
    for g in confirmed:
        people = [g.menu or ""] + [c.get("menu", "") or "" for c in g.companions]
        people += [""] * max(0, g.guests_count - len(people))  # coperti senza nome né menu
        for m in people[: max(g.guests_count, 1)]:
            by_menu[m] = by_menu.get(m, 0) + 1
    # allergie e note per persona (invitato e accompagnatori) e chi non ha ancora scelto il menu
    allergies = []
    missing_menu = []
    for g in confirmed:
        if (g.dietary_notes or "").strip().lower() not in _NO_DIET:
            allergies.append({"name": g.full_name, "notes": g.dietary_notes.strip(), "guest": g.full_name})
        if not g.menu:
            missing_menu.append(g.full_name)
        for c in g.companions:
            if (c.get("notes") or "").strip():
                allergies.append({"name": c.get("name") or "accompagnatore", "notes": c["notes"].strip(), "guest": g.full_name})
            if not c.get("menu"):
                missing_menu.append(f"{c.get('name') or 'accompagnatore'} (con {g.full_name})")
    dietary: dict[str, dict] = {}
    for g in confirmed:
        note = (g.dietary_notes or "").strip()
        if note.lower() in _NO_DIET:
            continue
        d = dietary.setdefault(note.lower(), {"note": note, "guests": [], "covers": 0})
        d["guests"].append(g.full_name)
        d["covers"] += g.guests_count
    return {
        "confirmedGuests": len(confirmed),
        "covers": sum(g.guests_count for g in confirmed),
        "pendingGuests": len(pending),
        "pendingCovers": sum(g.guests_count for g in pending),
        "declinedGuests": len(declined),
        "byCategory": sorted(by_category.values(), key=lambda c: (-c["covers"], c["category"])),
        "dietary": sorted(dietary.values(), key=lambda d: (-d["covers"], d["note"])),
        "byMenu": [{"menu": m or "", "covers": n} for m, n in sorted(by_menu.items(), key=lambda kv: (-kv[1], kv[0]))],
        "allergies": allergies,
        "missingMenu": missing_menu,
    }


@router.get("/summary", dependencies=[Depends(require_lists)])
def summary(db: Session = Depends(get_db)):
    """Riepilogo coperti e menu speciali (per ristorante e organizzatori)."""
    return guests_summary(db.scalars(select(Guest)).all())


def _emit(request: Request, event: str, guest: dict, db: Session, **extra) -> None:
    """Webhook verso n8n con il record e il riepilogo aggiornato dei coperti."""
    request.app.state.webhooks.emit(
        event, {"guest": guest, "summary": guests_summary(db.scalars(select(Guest)).all()), **extra}
    )


@router.post("", status_code=201)
def create_guest(
    body: GuestIn,
    request: Request,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
):
    guest = Guest(
        full_name=body.fullName,
        category=body.category or "Invitato",
        rsvp_status=body.rsvpStatus,
        guests_count=max(1, body.guestsCount) if body.rsvpStatus != "DECLINED" else body.guestsCount,
        dietary_notes=body.dietaryNotes,
        contact_info=body.contactInfo,
        owner_client_id=client_id,
    )
    db.add(guest)
    bump_version(db)
    db.commit()
    db.refresh(guest)
    _emit(request, "guest.created", guest.to_dict(), db)
    return guest.to_dict()


@router.put("/{guest_id}")
def update_guest(
    guest_id: int,
    body: GuestUpdate,
    request: Request,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
    admin: bool = Depends(is_admin),
):
    guest = db.get(Guest, guest_id)
    if guest is None:
        raise HTTPException(404, "Invitato non trovato")
    # Cambiare lo stato RSVP è consentito a tutti (è l'uso principale per gli invitati);
    # modificare gli altri campi richiede di essere il creatore o un organizzatore.
    changes = body.model_dump(exclude_none=True)
    non_status = {k for k in changes if k != "rsvpStatus"}
    if non_status and not can_modify(guest.owner_client_id, admin, client_id):
        raise HTTPException(403, "Solo chi ha inserito l'invitato o un organizzatore può modificarlo")
    if "fullName" in changes:
        guest.full_name = changes["fullName"].strip()
    if "category" in changes:
        guest.category = changes["category"].strip() or "Invitato"
    if "rsvpStatus" in changes:
        guest.rsvp_status = changes["rsvpStatus"]
    if "guestsCount" in changes:
        guest.guests_count = changes["guestsCount"]
    if "dietaryNotes" in changes:
        guest.dietary_notes = changes["dietaryNotes"].strip()
    if "contactInfo" in changes:
        guest.contact_info = changes["contactInfo"].strip()
    guest.updated_at = now_ms()
    bump_version(db)
    db.commit()
    db.refresh(guest)
    _emit(request, "guest.updated", guest.to_dict(), db, changes=changes)
    return guest.to_dict()


@router.delete("/{guest_id}", status_code=204)
def delete_guest(
    guest_id: int,
    request: Request,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
    admin: bool = Depends(is_admin),
):
    guest = db.get(Guest, guest_id)
    if guest is None:
        raise HTTPException(404, "Invitato non trovato")
    if not can_modify(guest.owner_client_id, admin, client_id):
        raise HTTPException(403, "Solo chi ha inserito l'invitato o un organizzatore può rimuoverlo")
    snapshot = guest.to_dict()
    db.delete(guest)
    bump_version(db)
    db.commit()
    _emit(request, "guest.deleted", snapshot, db)
    return None


# ---------------------------------------------------------------- profilo: link personale (organizzatori)


@router.get("/{guest_id}/link", dependencies=[Depends(require_admin)])
def guest_link(guest_id: int, request: Request, db: Session = Depends(get_db)):
    """Il link personale dell'invitato (lo crea se manca), da copiare o stampare come QR."""
    from .profile import ensure_token, profile_link

    guest = db.get(Guest, guest_id)
    if guest is None:
        raise HTTPException(404, "Invitato non trovato")
    token = ensure_token(guest)
    db.commit()
    return {"guestId": guest.id, "link": profile_link(request, token), "email": guest.email, "guest": guest.to_dict(private=True)}


@router.post("/{guest_id}/invite", status_code=202, dependencies=[Depends(require_admin)])
def guest_invite(guest_id: int, request: Request, db: Session = Depends(get_db)):
    """Manda all'invitato l'email con il link personale (serve l'email nel suo profilo)."""
    from .profile import ensure_token, profile_link, send_access_link

    guest = db.get(Guest, guest_id)
    if guest is None:
        raise HTTPException(404, "Invitato non trovato")
    if not guest.email:
        raise HTTPException(422, "L'invitato non ha un'email: copia il link e mandaglielo tu")
    mail = send_access_link(request, db, guest)
    return {"guestId": guest.id, "link": profile_link(request, ensure_token(guest)), "mail": mail}


@router.post("/import", dependencies=[Depends(require_admin)])
def import_guests(body: ImportIn, request: Request, db: Session = Depends(get_db)):
    """Import della lista invitati (righe già spezzate dalla PWA a partire da un CSV o da un foglio).
    Abbina per email, altrimenti per nome (senza distinguere maiuscole); aggiorna i campi indicati e crea
    chi manca (in attesa). Con sendLinks manda il link personale a chi ha l'email; con dryRun solo l'anteprima."""
    from .profile import find_by_email, send_access_link

    guests = db.scalars(select(Guest)).all()
    by_name = {g.full_name.strip().lower(): g for g in guests}
    results, created, updated, unchanged, skipped = [], 0, 0, 0, 0
    to_invite: list[Guest] = []
    for row in body.rows:
        if not row.fullName:
            skipped += 1
            results.append({"fullName": "", "action": "skipped", "reason": "nome mancante"})
            continue
        g = find_by_email(db, row.email) if row.email else None
        if g is None:
            g = by_name.get(row.fullName.lower())
        changes = {}
        if g is None:
            action = "created"
            g = Guest(full_name=row.fullName, category=row.category or "Invitato", rsvp_status=row.rsvpStatus or "PENDING",
                      guests_count=row.guestsCount if row.guestsCount is not None else 1, email=row.email, phone=row.phone,
                      contact_info=row.email or row.phone)
            if not body.dryRun:
                db.add(g)
            by_name[row.fullName.lower()] = g
            created += 1
        else:
            for attr, value in (("category", row.category), ("email", row.email), ("phone", row.phone)):
                if value and getattr(g, attr) != value:
                    changes[attr] = value
            if row.guestsCount is not None and g.guests_count != row.guestsCount:
                changes["guests_count"] = row.guestsCount
            if row.rsvpStatus and g.rsvp_status != row.rsvpStatus:
                changes["rsvp_status"] = row.rsvpStatus
            if changes:
                action = "updated"
                updated += 1
                if not body.dryRun:
                    for attr, value in changes.items():
                        setattr(g, attr, value)
                    g.contact_info = g.email or g.phone or g.contact_info
                    g.updated_at = now_ms()
            else:
                action = "unchanged"
                unchanged += 1
        results.append({"fullName": g.full_name, "email": g.email or row.email, "action": action,
                        "changes": sorted(changes.keys()), "willInvite": bool(body.sendLinks and (g.email or row.email))})
        if body.sendLinks and (g.email or row.email):
            to_invite.append(g)
    if body.dryRun:
        return {"dryRun": True, "created": created, "updated": updated, "unchanged": unchanged, "skipped": skipped, "invited": 0, "rows": results}
    if created or updated:
        bump_version(db)
    db.commit()
    invited, failed = 0, 0
    for g in to_invite:
        db.refresh(g)
        out = send_access_link(request, db, g)
        if out and out.get("status") in ("QUEUED", "DELIVERED"):
            invited += 1
        else:
            failed += 1
    if created or updated:
        request.app.state.webhooks.emit("guest.imported", {"created": created, "updated": updated, "invited": invited,
                                                           "summary": guests_summary(db.scalars(select(Guest)).all())})
    return {"dryRun": False, "created": created, "updated": updated, "unchanged": unchanged, "skipped": skipped,
            "invited": invited, "inviteFailed": failed, "rows": results}
