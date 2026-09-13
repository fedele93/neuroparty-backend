from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import can_modify, get_client_id, is_admin
from ..deps import get_db
from ..models import Guest, now_ms
from ..schemas import GuestIn, GuestUpdate
from ..state import bump_version

router = APIRouter(prefix="/api/guests", tags=["invitati"])


@router.get("")
def list_guests(db: Session = Depends(get_db)):
    rows = db.scalars(select(Guest).order_by(Guest.full_name.asc())).all()
    return [g.to_dict() for g in rows]


@router.post("", status_code=201)
def create_guest(body: GuestIn, db: Session = Depends(get_db), client_id: str | None = Depends(get_client_id)):
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
    return guest.to_dict()


@router.put("/{guest_id}")
def update_guest(
    guest_id: int,
    body: GuestUpdate,
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
    return guest.to_dict()


@router.delete("/{guest_id}", status_code=204)
def delete_guest(
    guest_id: int,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
    admin: bool = Depends(is_admin),
):
    guest = db.get(Guest, guest_id)
    if guest is None:
        raise HTTPException(404, "Invitato non trovato")
    if not can_modify(guest.owner_client_id, admin, client_id):
        raise HTTPException(403, "Solo chi ha inserito l'invitato o un organizzatore può rimuoverlo")
    db.delete(guest)
    bump_version(db)
    db.commit()
    return None
