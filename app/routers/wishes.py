from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_member
from ..deps import get_db
from ..models import Guest, Wish
from ..schemas import WishIn
from ..state import bump_version

router = APIRouter(prefix="/api/wishes", tags=["auguri"])


@router.get("", dependencies=[Depends(require_member)])
def list_wishes(db: Session = Depends(get_db)):
    rows = db.scalars(select(Wish).order_by(Wish.created_at.desc())).all()
    return [w.to_dict() for w in rows]


@router.post("", status_code=201)
def create_wish(body: WishIn, request: Request, db: Session = Depends(get_db), member: Guest | None = Depends(require_member)):
    """Nuovo augurio. Per gli invitati registrati l'autore è il nome del profilo (nessuno firma per un altro)."""
    wish = Wish(
        author_name=member.full_name if member is not None else body.authorName,
        target_graduate=body.targetGraduate or "Tutti i Laureandi",
        message=body.message,
        emoji_badge=body.emojiBadge or "🎓",
        heart_count=1,
    )
    db.add(wish)
    bump_version(db)
    db.commit()
    db.refresh(wish)
    request.app.state.webhooks.emit("wish.created", {"wish": wish.to_dict()})
    notify_graduate(request, db, wish)
    return wish.to_dict()


def notify_graduate(request: Request, db: Session, wish: Wish) -> None:
    """Email al festeggiato destinatario dell'augurio (se ha un'email privata in giftTargets)."""
    from ..deps import base_url_for
    from ..mail import wish_received_mail
    from ..models import GiftTarget

    target_name = (wish.target_graduate or "").strip().lower()
    if not target_name or target_name.startswith("tutti"):
        return
    for t in db.scalars(select(GiftTarget)).all():
        if t.email and t.short_name.lower() == target_name:
            subject, text, html = wish_received_mail(t, wish, base_url_for(request))
            request.app.state.mailer.send("wish_received", t.email, subject, text, html)
            return


@router.post("/{wish_id}/heart", dependencies=[Depends(require_member)])
def heart_wish(wish_id: int, db: Session = Depends(get_db)):
    wish = db.get(Wish, wish_id)
    if wish is None:
        raise HTTPException(404, "Augurio non trovato")
    wish.heart_count += 1
    bump_version(db)
    db.commit()
    db.refresh(wish)
    return wish.to_dict()


@router.delete("/{wish_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_wish(wish_id: int, db: Session = Depends(get_db)):
    wish = db.get(Wish, wish_id)
    if wish is None:
        raise HTTPException(404, "Augurio non trovato")
    db.delete(wish)
    bump_version(db)
    db.commit()
    return None
