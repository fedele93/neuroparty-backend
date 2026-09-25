"""Notifiche programmate: pubblica quelle il cui orario è arrivato e le invia in push.

Le notifiche con scheduled_at valorizzato non compaiono negli elenchi pubblici. Un ciclo in
background (avviato nel lifespan dell'app) chiama publish_due_notifications ogni pochi secondi:
ogni notifica scaduta viene resa visibile (timestamp = adesso, scheduled_at = NULL), la versione
dati viene incrementata (i client ricaricano) e parte il broadcast Web Push.
"""
import asyncio
import logging

from sqlalchemy import select

from .models import EventNotification, now_ms
from .state import bump_version

log = logging.getLogger("neuroparty.scheduler")

# Le notifiche con sendAt entro questa soglia partono subito (evita "programmate" di pochi secondi).
IMMEDIATE_THRESHOLD_MS = 30_000


def publish_due_notifications(session_factory, push, now: int | None = None) -> list[dict]:
    """Pubblica e invia in push tutte le notifiche programmate con scheduled_at <= now."""
    now = now if now is not None else now_ms()
    published: list[EventNotification] = []
    with session_factory() as db:
        due = db.scalars(
            select(EventNotification)
            .where(EventNotification.scheduled_at.is_not(None), EventNotification.scheduled_at <= now)
            .order_by(EventNotification.scheduled_at.asc())
        ).all()
        if not due:
            return []
        for n in due:
            n.timestamp = now
            n.scheduled_at = None
            published.append(n)
        bump_version(db)
        db.commit()
        result = [n.to_dict() for n in published]
    for n in result:
        try:
            push.broadcast(session_factory, n["title"], n["message"], n["category"], n["id"])
        except Exception:  # noqa: BLE001 - un errore push non deve fermare le altre
            log.exception("Broadcast della notifica programmata #%s fallito", n["id"])
    log.info("Pubblicate %d notifiche programmate", len(result))
    return result


async def run_scheduler(app, interval_s: float) -> None:
    """Ciclo in background: controlla le notifiche in scadenza ogni interval_s secondi."""
    while True:
        try:
            await asyncio.to_thread(publish_due_notifications, app.state.session_factory, app.state.push)
        except Exception:  # noqa: BLE001
            log.exception("Errore nello scheduler delle notifiche")
        await asyncio.sleep(interval_s)
