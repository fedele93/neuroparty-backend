from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import get_client_id
from ..deps import get_db
from ..models import PushSubscription
from ..push import subscription_count
from ..schemas import PushSubscriptionIn, PushUnsubscribeIn

router = APIRouter(prefix="/api/push", tags=["web push"])


@router.get("/vapid-public-key")
def vapid_public_key(request: Request):
    return {"publicKey": request.app.state.push.public_key_b64url}


@router.post("/subscribe", status_code=201)
def subscribe(
    body: PushSubscriptionIn,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
):
    existing = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if existing:
        existing.p256dh = body.keys.p256dh
        existing.auth = body.keys.auth
        existing.client_id = client_id
    else:
        db.add(
            PushSubscription(endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth, client_id=client_id)
        )
    db.commit()
    return {"ok": True, "subscriptions": subscription_count(db)}


@router.post("/unsubscribe")
def unsubscribe(body: PushUnsubscribeIn, db: Session = Depends(get_db)):
    db.execute(delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    db.commit()
    return {"ok": True}
