"""Regali: coordinate per donare direttamente ai neo-specialisti e quota unica tramite il cassiere.

- I destinatari (GET /targets) espongono solo nome, regalo e coordinate di pagamento: chi dona
  direttamente copia l'IBAN o apre PayPal/Satispay, senza registrare nulla sul server.
- La quota unica (POST /pool) viene invece registrata con la ripartizione fra i neo-specialisti:
  serve al cassiere (vedi "giftCollector" in event-data.json) per sapere quanto girare a ognuno.
  L'elenco completo è riservato al cassiere/organizzatori (X-Treasurer-Token o X-Admin-Token);
  ogni dispositivo vede solo le quote che ha registrato (X-Client-Id).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_client_id, is_treasurer, require_treasurer
from ..deps import get_db
from ..models import (
    POOL_STATUS_PENDING,
    POOL_STATUS_RECEIVED,
    GiftPoolAllocation,
    GiftPoolContribution,
    GiftTarget,
    now_ms,
)
from ..schemas import PoolContributionIn, PoolStatusIn
from ..seed import gift_collector_info
from ..state import bump_version

router = APIRouter(prefix="/api/gifts", tags=["regali"])


def _targets(db: Session) -> list[GiftTarget]:
    return db.scalars(select(GiftTarget).order_by(GiftTarget.sort_order.asc(), GiftTarget.id.asc())).all()


def _cents(value: float) -> int:
    return int(round(float(value) * 100))


def split_equally(total_cents: int, count: int) -> list[int]:
    """Divide i centesimi in parti uguali; i centesimi di resto vanno ai primi della lista."""
    base, rest = divmod(total_cents, count)
    return [base + (1 if i < rest else 0) for i in range(count)]


def build_allocations(body: PoolContributionIn, targets: list[GiftTarget]) -> tuple[list[GiftPoolAllocation], float]:
    """Trasforma la richiesta in righe di ripartizione, validando i destinatari. Ritorna (righe, totale)."""
    by_id = {t.id: t for t in targets}
    if body.splitMode == "EQUAL":
        if body.totalAmount is None:
            raise HTTPException(422, "Indica l'importo totale della quota")
        ids = body.graduateIds or [t.id for t in targets]
        if len(set(ids)) != len(ids):
            raise HTTPException(422, "Neo-specialista indicato due volte")
        missing = [g for g in ids if g not in by_id]
        if missing:
            raise HTTPException(404, f"Neo-specialista non trovato: {', '.join(missing)}")
        if not ids:
            raise HTTPException(422, "Nessun neo-specialista da includere nella quota")
        total_cents = _cents(body.totalAmount)
        if total_cents < len(ids):
            raise HTTPException(422, "Importo troppo basso per essere diviso fra i neo-specialisti scelti")
        parts = split_equally(total_cents, len(ids))
        rows = [
            GiftPoolAllocation(graduate_id=g, graduate_name=by_id[g].name, amount=p / 100)
            for g, p in zip(ids, parts)
        ]
        return rows, total_cents / 100

    # CUSTOM: un importo per ciascun neo-specialista scelto; il totale è la somma
    if not body.allocations:
        raise HTTPException(422, "Indica almeno un importo personalizzato")
    ids = [a.graduateId for a in body.allocations]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Neo-specialista indicato due volte")
    missing = [g for g in ids if g not in by_id]
    if missing:
        raise HTTPException(404, f"Neo-specialista non trovato: {', '.join(missing)}")
    rows = [
        GiftPoolAllocation(graduate_id=a.graduateId, graduate_name=by_id[a.graduateId].name, amount=_cents(a.amount) / 100)
        for a in body.allocations
    ]
    total_cents = sum(_cents(a.amount) for a in body.allocations)
    if body.totalAmount is not None and _cents(body.totalAmount) != total_cents:
        raise HTTPException(422, "La somma degli importi personalizzati non corrisponde al totale")
    return rows, total_cents / 100


def pool_summary(rows: list[GiftPoolContribution], targets: list[GiftTarget]) -> dict:
    """Totali per il cassiere: complessivi, per neo-specialista e per metodo di pagamento."""
    per_grad: dict[str, dict] = {
        t.id: {"graduateId": t.id, "graduateName": t.name, "amount": 0, "receivedAmount": 0, "contributions": 0}
        for t in targets
    }
    per_method: dict[str, dict] = {}
    total = received = 0
    for c in rows:
        cents = _cents(c.total_amount)
        total += cents
        is_received = c.status == POOL_STATUS_RECEIVED
        if is_received:
            received += cents
        m = per_method.setdefault(c.payment_method, {"paymentMethod": c.payment_method, "amount": 0, "contributions": 0})
        m["amount"] += cents
        m["contributions"] += 1
        for a in c.allocations:
            g = per_grad.setdefault(
                a.graduate_id,
                {"graduateId": a.graduate_id, "graduateName": a.graduate_name, "amount": 0, "receivedAmount": 0, "contributions": 0},
            )
            g["amount"] += _cents(a.amount)
            g["contributions"] += 1
            if is_received:
                g["receivedAmount"] += _cents(a.amount)
    for g in per_grad.values():
        g["amount"] /= 100
        g["receivedAmount"] /= 100
    for m in per_method.values():
        m["amount"] /= 100
    return {
        "contributions": len(rows),
        "received": sum(1 for c in rows if c.status == POOL_STATUS_RECEIVED),
        "pending": sum(1 for c in rows if c.status != POOL_STATUS_RECEIVED),
        "totalAmount": total / 100,
        "receivedAmount": received / 100,
        "pendingAmount": (total - received) / 100,
        "byGraduate": list(per_grad.values()),
        "byMethod": sorted(per_method.values(), key=lambda m: -m["amount"]),
    }


def _all_pool(db: Session) -> list[GiftPoolContribution]:
    return db.scalars(select(GiftPoolContribution).order_by(GiftPoolContribution.created_at.desc())).all()


# ---------------------------------------------------------------- destinatari e cassiere


@router.get("/targets")
def list_targets(db: Session = Depends(get_db)):
    """Neo-specialisti con regalo e coordinate di pagamento (nessun importo)."""
    return [t.to_dict() for t in _targets(db)]


@router.get("/collector")
def collector(request: Request):
    """Chi raccoglie le quote uniche, con IBAN/PayPal e metodi ammessi."""
    return gift_collector_info(request.app.state.event_data)


# ---------------------------------------------------------------- quota unica: invitati


@router.post("/pool", status_code=201)
def add_pool_contribution(
    body: PoolContributionIn,
    request: Request,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
):
    targets = _targets(db)
    if not targets:
        raise HTTPException(409, "Nessun neo-specialista configurato")
    allocations, total = build_allocations(body, targets)
    contribution = GiftPoolContribution(
        donor_name=body.donorName,
        contact=body.contact,
        payment_method=body.paymentMethod,
        split_mode=body.splitMode,
        total_amount=total,
        note=body.note,
        status=POOL_STATUS_PENDING,
        client_id=client_id,
        allocations=allocations,
    )
    db.add(contribution)
    bump_version(db)
    db.commit()
    db.refresh(contribution)
    result = contribution.to_dict()
    request.app.state.webhooks.emit(
        "gift.pooled", {"contribution": result, "summary": pool_summary(_all_pool(db), targets)}
    )
    return result


@router.get("/pool/mine")
def my_pool_contributions(db: Session = Depends(get_db), client_id: str | None = Depends(get_client_id)):
    """Le quote uniche registrate da questo dispositivo."""
    if not client_id:
        return []
    rows = db.scalars(
        select(GiftPoolContribution)
        .where(GiftPoolContribution.client_id == client_id)
        .order_by(GiftPoolContribution.created_at.desc())
    ).all()
    return [c.to_dict() for c in rows]


@router.delete("/pool/{contribution_id}", status_code=204)
def delete_pool_contribution(
    contribution_id: int,
    request: Request,
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
    treasurer: bool = Depends(is_treasurer),
):
    """Chi ha registrato la quota può cancellarla finché il cassiere non l'ha segnata come
    ricevuta; il cassiere e gli organizzatori sempre."""
    c = db.get(GiftPoolContribution, contribution_id)
    if c is None:
        raise HTTPException(404, "Quota non trovata")
    owner = bool(client_id) and c.client_id == client_id
    if not treasurer:
        if not owner:
            raise HTTPException(403, "Puoi cancellare solo le quote registrate da questo dispositivo")
        if c.status == POOL_STATUS_RECEIVED:
            raise HTTPException(409, "Quota già ricevuta dal cassiere: contattalo per modificarla")
    db.delete(c)
    bump_version(db)
    db.commit()
    request.app.state.webhooks.emit("gift.pool_deleted", {"contributionId": contribution_id})
    return None


# ---------------------------------------------------------------- quota unica: cassiere


@router.get("/pool", dependencies=[Depends(require_treasurer)])
def list_pool_contributions(db: Session = Depends(get_db)):
    """Cruscotto del cassiere: tutte le quote uniche e i totali per neo-specialista."""
    rows = _all_pool(db)
    return {"contributions": [c.to_dict() for c in rows], "summary": pool_summary(rows, _targets(db))}


@router.patch("/pool/{contribution_id}/status", dependencies=[Depends(require_treasurer)])
def set_pool_status(contribution_id: int, body: PoolStatusIn, db: Session = Depends(get_db)):
    """Il cassiere segna la quota come ricevuta (o la rimette in attesa)."""
    c = db.get(GiftPoolContribution, contribution_id)
    if c is None:
        raise HTTPException(404, "Quota non trovata")
    if c.status != body.status:
        c.status = body.status
        c.received_at = now_ms() if body.status == POOL_STATUS_RECEIVED else None
        bump_version(db)
        db.commit()
        db.refresh(c)
    return c.to_dict()
