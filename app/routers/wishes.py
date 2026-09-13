from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..deps import get_db
from ..models import Wish
from ..schemas import WishIn
from ..state import bump_version

router = APIRouter(prefix="/api/wishes", tags=["auguri"])


@router.get("")
def list_wishes(db: Session = Depends(get_db)):
    rows = db.scalars(select(Wish).order_by(Wish.created_at.desc())).all()
    return [w.to_dict() for w in rows]


@router.post("", status_code=201)
def create_wish(body: WishIn, db: Session = Depends(get_db)):
    wish = Wish(
        author_name=body.authorName,
        target_graduate=body.targetGraduate or "Tutti i Laureandi",
        message=body.message,
        emoji_badge=body.emojiBadge or "🎓",
        heart_count=1,
    )
    db.add(wish)
    bump_version(db)
    db.commit()
    db.refresh(wish)
    return wish.to_dict()


@router.post("/{wish_id}/heart")
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
