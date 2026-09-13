from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import GiftContribution, GiftTarget
from ..schemas import ContributionIn
from ..state import bump_version

router = APIRouter(prefix="/api/gifts", tags=["regali"])


@router.get("/targets")
def list_targets(db: Session = Depends(get_db)):
    rows = db.scalars(select(GiftTarget).order_by(GiftTarget.sort_order.asc(), GiftTarget.id.asc())).all()
    return [t.to_dict() for t in rows]


@router.get("/contributions")
def list_contributions(db: Session = Depends(get_db)):
    rows = db.scalars(select(GiftContribution).order_by(GiftContribution.contributed_at.desc())).all()
    return [c.to_dict() for c in rows]


@router.post("/contributions", status_code=201)
def add_contribution(body: ContributionIn, db: Session = Depends(get_db)):
    target = db.get(GiftTarget, body.targetGraduateId)
    if target is None:
        raise HTTPException(404, "Destinatario del regalo non trovato")
    donor = "Un invitato generoso" if body.isAnonymous else (body.donorName.strip() or "Invitato")
    contribution = GiftContribution(
        donor_name=donor,
        target_graduate_id=target.id,
        target_graduate_name=target.name,
        amount=round(float(body.amount), 2),
        payment_method=body.paymentMethod or "IBAN",
        note=body.note.strip(),
        is_anonymous=body.isAnonymous,
    )
    target.collected_amount = round(target.collected_amount + contribution.amount, 2)
    db.add(contribution)
    bump_version(db)
    db.commit()
    db.refresh(contribution)
    return {"contribution": contribution.to_dict(), "target": target.to_dict()}
