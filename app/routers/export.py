"""Esportazione CSV: lista per il ristorante, per l'autista e quote uniche per il cassiere.

Invitati e navetta richiedono X-Admin-Token oppure X-Automation-Token (per n8n, es. CSV
allegato a una mail); le quote uniche anche X-Treasurer-Token.
Il CSV usa ";" come separatore, la virgola decimale e il BOM UTF-8, così Excel italiano lo apre
con un doppio clic e riconosce gli importi come numeri.
"""
import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_automation, require_treasurer_or_automation
from ..deps import get_db
from ..models import POOL_STATUS_RECEIVED, BusBooking, GiftPoolContribution, GiftTarget, Guest

router = APIRouter(prefix="/api/export", tags=["export"])

RSVP_LABELS = {"CONFIRMED": "Confermato", "PENDING": "In attesa", "DECLINED": "Declinato"}
POOL_STATUS_LABELS = {"PENDING": "In attesa", "RECEIVED": "Ricevuta"}
SPLIT_LABELS = {"EQUAL": "Parti uguali", "CUSTOM": "Personalizzata"}


def _fmt_ts(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d/%m/%Y %H:%M") if ms else ""


def _fmt_amount(value: float | None) -> str:
    """Importo con la virgola decimale (Excel italiano) o vuoto."""
    return "" if value is None else f"{value:.2f}".replace(".", ",")


def _csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/guests.csv", dependencies=[Depends(require_automation)])
def export_guests(db: Session = Depends(get_db)):
    rows = db.scalars(select(Guest).order_by(Guest.rsvp_status.asc(), Guest.full_name.asc())).all()
    return _csv_response(
        "invitati.csv",
        ["Nome", "Categoria", "Stato RSVP", "Persone", "Esigenze alimentari", "Contatto", "Aggiornato il"],
        [[g.full_name, g.category, RSVP_LABELS.get(g.rsvp_status, g.rsvp_status), g.guests_count,
          g.dietary_notes, g.contact_info, _fmt_ts(g.updated_at)] for g in rows],
    )


@router.get("/bus.csv", dependencies=[Depends(require_automation)])
def export_bus(db: Session = Depends(get_db)):
    rows = db.scalars(select(BusBooking).order_by(BusBooking.pickup_stop.asc(), BusBooking.passenger_name.asc())).all()
    total = sum(b.seats_count for b in rows)
    data = [[b.passenger_name, b.seats_count, b.pickup_stop, "Sì" if b.return_trip_wanted else "No",
             b.contact_phone, b.notes, _fmt_ts(b.booked_at)] for b in rows]
    data.append(["TOTALE POSTI", total, "", "", "", "", ""])
    return _csv_response(
        "navetta.csv",
        ["Passeggero", "Posti", "Fermata", "Ritorno", "Telefono", "Note", "Prenotato il"],
        data,
    )


@router.get("/gift-pool.csv", dependencies=[Depends(require_treasurer_or_automation)])
def export_gift_pool(db: Session = Depends(get_db)):
    """Quote uniche per il cassiere: una riga per versamento, una colonna per neo-specialista
    e le righe dei totali (complessivo e già ricevuto), così in Excel si legge subito quanto
    spetta a ognuno."""
    targets = db.scalars(select(GiftTarget).order_by(GiftTarget.sort_order.asc(), GiftTarget.id.asc())).all()
    rows = db.scalars(select(GiftPoolContribution).order_by(GiftPoolContribution.created_at.asc())).all()
    columns = [t.id for t in targets]
    names = {t.id: t.name for t in targets}
    # neo-specialisti presenti solo nelle quote (es. rimossi dal JSON dopo il versamento)
    for c in rows:
        for a in c.allocations:
            if a.graduate_id not in names:
                names[a.graduate_id] = a.graduate_name or a.graduate_id
                columns.append(a.graduate_id)

    totals = {g: 0.0 for g in columns}
    received = {g: 0.0 for g in columns}
    data = []
    for c in rows:
        per = {a.graduate_id: a.amount for a in c.allocations}
        is_received = c.status == POOL_STATUS_RECEIVED
        for g, amount in per.items():
            totals[g] += amount
            if is_received:
                received[g] += amount
        data.append([
            _fmt_ts(c.created_at), c.donor_name, c.contact, c.payment_method, SPLIT_LABELS.get(c.split_mode, c.split_mode),
            _fmt_amount(c.total_amount), POOL_STATUS_LABELS.get(c.status, c.status), _fmt_ts(c.received_at), c.note,
            *[_fmt_amount(per.get(g)) for g in columns],
        ])
    fixed = ["Data", "Donatore", "Contatto", "Metodo", "Ripartizione", "Totale", "Stato", "Ricevuta il", "Note"]
    total_all = sum(c.total_amount for c in rows)
    total_received = sum(c.total_amount for c in rows if c.status == POOL_STATUS_RECEIVED)
    data.append(["TOTALE", "", "", "", "", _fmt_amount(total_all), "", "", "", *[_fmt_amount(totals[g]) for g in columns]])
    data.append([
        "DI CUI RICEVUTO", "", "", "", "", _fmt_amount(total_received), "", "", "",
        *[_fmt_amount(received[g]) for g in columns],
    ])
    return _csv_response("quote-uniche.csv", fixed + [names[g] for g in columns], data)
