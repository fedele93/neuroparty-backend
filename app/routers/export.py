"""Esportazione CSV: lista per il ristorante, per l'autista e quote uniche per il cassiere.

Invitati e navetta richiedono X-Admin-Token oppure X-Automation-Token (per n8n, es. CSV
allegato a una mail); le quote uniche anche X-Treasurer-Token.
Il CSV usa ";" come separatore, la virgola decimale e il BOM UTF-8, così Excel italiano lo apre
con un doppio clic e riconosce gli importi come numeri.
"""
import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_automation, require_treasurer_or_automation
from ..deps import get_db
from ..mail import MENU_LABELS
from ..models import POOL_STATUS_RECEIVED, BusBooking, GiftTarget, Guest
from .gifts import collectors_of, effective_collector_id, pool_rows_for

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
        ["Nome", "Categoria", "Stato RSVP", "Persone", "Menu", "Accompagnatori e menu", "Esigenze alimentari", "Email", "Telefono", "Contatto", "Aggiornato il"],
        [[g.full_name, g.category, RSVP_LABELS.get(g.rsvp_status, g.rsvp_status), g.guests_count,
          MENU_LABELS.get(g.menu, g.menu) if g.menu else "",
          "; ".join(f"{c.get('name', '')}: {MENU_LABELS.get(c.get('menu', ''), c.get('menu', '')) if c.get('menu') else 'da scegliere'}"
                    + (f" ({c['notes']})" if c.get("notes") else "") for c in g.companions),
          g.dietary_notes, g.email, g.phone, g.contact_info, _fmt_ts(g.updated_at)] for g in rows],
    )


@router.get("/bus.csv", dependencies=[Depends(require_automation)])
def export_bus(db: Session = Depends(get_db)):
    rows = db.scalars(select(BusBooking).order_by(BusBooking.status.asc(), BusBooking.pickup_stop.asc(), BusBooking.passenger_name.asc())).all()
    guests = {g.id: g for g in db.scalars(select(Guest)).all()}
    confirmed = [b for b in rows if b.status != "WAITLIST"]
    data = [[b.passenger_name, b.seats_count, "Lista d'attesa" if b.status == "WAITLIST" else "Confermata", b.code, b.pickup_stop,
             "Sì" if b.return_trip_wanted else "No", b.contact_phone or (guests[b.guest_id].phone if b.guest_id in guests else ""),
             guests[b.guest_id].email if b.guest_id in guests else "", "Sì" if b.checked_in_at else "", b.notes, _fmt_ts(b.booked_at)] for b in rows]
    data.append(["TOTALE POSTI CONFERMATI", sum(b.seats_count for b in confirmed), "", "", "", "", "", "", "", "", ""])
    data.append(["IN LISTA D'ATTESA", sum(b.seats_count for b in rows if b.status == "WAITLIST"), "", "", "", "", "", "", "", "", ""])
    return _csv_response(
        "navetta.csv",
        ["Passeggero", "Posti", "Stato", "Codice", "Fermata", "Ritorno", "Telefono", "Email", "Salito", "Note", "Prenotato il"],
        data,
    )


@router.get("/gift-pool.csv")
def export_gift_pool(request: Request, collector: str = "", db: Session = Depends(get_db), scope: set[str] | None = Depends(require_treasurer_or_automation)):
    """Quote uniche per i cassieri: una riga per versamento (con il cassiere), una colonna per
    neo-specialista e le righe dei totali per cassiere e complessivi (anche "già ricevuto"),
    così in Excel si legge subito quanto spetta a ognuno. Con TREASURER_TOKEN_<ID> solo le
    quote di quel cassiere; `?collector=<id>` restringe a un cassiere (per n8n, che manda a
    ogni cassiere il suo CSV)."""
    targets = db.scalars(select(GiftTarget).order_by(GiftTarget.sort_order.asc(), GiftTarget.id.asc())).all()
    collectors = collectors_of(request)
    if collector:
        if scope is not None and collector not in scope:
            raise HTTPException(403, "Questo cassiere non è nel tuo ambito")
        scope = {collector}
    rows = sorted(pool_rows_for(db, scope, collectors), key=lambda c: c.created_at or 0)
    names_c = {c["id"]: c["name"] for c in collectors}
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
    per_collector: dict[str, dict] = {}
    data = []
    for c in rows:
        cid = effective_collector_id(c, collectors)
        cname = c.collector_name or names_c.get(cid, cid)
        per = {a.graduate_id: a.amount for a in c.allocations}
        is_received = c.status == POOL_STATUS_RECEIVED
        pc = per_collector.setdefault(cid, {"name": cname, "total": 0.0, "received": 0.0, "by": {g: 0.0 for g in columns}})
        pc["total"] += c.total_amount
        if is_received:
            pc["received"] += c.total_amount
        for g, amount in per.items():
            totals[g] += amount
            pc["by"][g] = pc["by"].get(g, 0.0) + amount
            if is_received:
                received[g] += amount
        data.append([
            _fmt_ts(c.created_at), cname, c.donor_name, c.contact, c.payment_method, SPLIT_LABELS.get(c.split_mode, c.split_mode),
            _fmt_amount(c.total_amount), POOL_STATUS_LABELS.get(c.status, c.status), _fmt_ts(c.received_at), c.note,
            *[_fmt_amount(per.get(g)) for g in columns],
        ])
    fixed = ["Data", "Cassiere", "Donatore", "Contatto", "Metodo", "Ripartizione", "Totale", "Stato", "Ricevuta il", "Note"]
    blanks = [""] * 8
    if len(per_collector) > 1:
        for pc in per_collector.values():
            data.append([f"TOTALE {pc['name'].upper()}", "", *blanks[:4], _fmt_amount(pc["total"]), "", "", "",
                         *[_fmt_amount(pc["by"].get(g, 0.0)) for g in columns]])
    total_all = sum(c.total_amount for c in rows)
    total_received = sum(c.total_amount for c in rows if c.status == POOL_STATUS_RECEIVED)
    data.append(["TOTALE", "", *blanks[:4], _fmt_amount(total_all), "", "", "", *[_fmt_amount(totals[g]) for g in columns]])
    data.append(["DI CUI RICEVUTO", "", *blanks[:4], _fmt_amount(total_received), "", "", "", *[_fmt_amount(received[g]) for g in columns]])
    return _csv_response("quote-uniche.csv", fixed + [names[g] for g in columns], data)
