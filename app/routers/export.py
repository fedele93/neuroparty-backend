"""Esportazione CSV per gli organizzatori (lista per il ristorante e per l'autista).

Richiede X-Admin-Token. Il CSV usa ";" come separatore e il BOM UTF-8, così Excel italiano
lo apre correttamente con un doppio clic.
"""
import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..deps import get_db
from ..models import BusBooking, Guest

router = APIRouter(prefix="/api/export", tags=["export"], dependencies=[Depends(require_admin)])

RSVP_LABELS = {"CONFIRMED": "Confermato", "PENDING": "In attesa", "DECLINED": "Declinato"}


def _fmt_ts(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d/%m/%Y %H:%M") if ms else ""


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


@router.get("/guests.csv")
def export_guests(db: Session = Depends(get_db)):
    rows = db.scalars(select(Guest).order_by(Guest.rsvp_status.asc(), Guest.full_name.asc())).all()
    return _csv_response(
        "invitati.csv",
        ["Nome", "Categoria", "Stato RSVP", "Persone", "Esigenze alimentari", "Contatto", "Aggiornato il"],
        [[g.full_name, g.category, RSVP_LABELS.get(g.rsvp_status, g.rsvp_status), g.guests_count,
          g.dietary_notes, g.contact_info, _fmt_ts(g.updated_at)] for g in rows],
    )


@router.get("/bus.csv")
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
