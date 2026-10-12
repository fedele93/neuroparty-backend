"""Email agli invitati: il backend prepara il messaggio (oggetto, testo, HTML) e lo consegna a
n8n con l'evento webhook `mail.send`; il nodo Send Email di n8n, già configurato dagli
organizzatori, fa la spedizione. Ogni email finisce nella tabella mail_outbox con l'esito,
così gli organizzatori vedono quelle non consegnate e possono rimandarle.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from .models import MAIL_DELIVERED, MAIL_FAILED, MAIL_QUEUED, MAIL_SKIPPED, Guest, MailOutbox, now_ms
from .schedule import get_schedule

log = logging.getLogger("neuroparty.mail")

MENU_LABELS = {
    "": "da scegliere",
    "STANDARD": "menu completo",
    "VEGETARIANO": "vegetariano",
    "VEGANO": "vegano",
    "SENZA_GLUTINE": "senza glutine",
    "ALTRO": "altro (vedi note)",
}
RSVP_LABELS = {"CONFIRMED": "Confermata", "PENDING": "In attesa di conferma", "DECLINED": "Non parteciperà"}

KINDS = {
    "access_link": "Link personale",
    "rsvp_confirmation": "Conferma partecipazione",
    "test": "Email di prova",
}


def format_date_it(iso: str) -> str:
    from datetime import date

    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or "data da definire"
    days = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    months = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    return f"{days[d.weekday()]} {d.day} {months[d.month - 1]} {d.year}"


def event_summary(event_data: dict) -> dict:
    sch = get_schedule(event_data)
    program = event_data.get("program") or {}
    return {
        "title": program.get("title") or "NeuroParty",
        "partyDate": format_date_it(sch.get("partyDate", "")),
        "partyTime": sch.get("partyTime") or "",
        "location": program.get("locationLabel") or "",
    }


# ---------------------------------------------------------------------- modelli


def _layout(title: str, body_html: str, footer: str) -> str:
    e = html_lib.escape
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:560px;margin:0 auto;color:#0F1B2D;">'
        f'<div style="background:#0A192F;color:#fff;padding:18px 20px;border-radius:14px 14px 0 0;"><div style="font-size:12px;letter-spacing:.08em;color:#C9A227;">NEUROPARTY</div>'
        f'<h1 style="font-size:20px;margin:6px 0 0;">{e(title)}</h1></div>'
        f'<div style="border:1px solid #E2E8F0;border-top:none;padding:18px 20px;border-radius:0 0 14px 14px;font-size:15px;line-height:1.5;">{body_html}'
        f'<p style="font-size:12px;color:#5B6677;margin-top:22px;">{e(footer)}</p></div></div>'
    )


def _button(url: str, label: str) -> str:
    return (f'<p style="margin:18px 0;"><a href="{html_lib.escape(url)}" style="background:#005FB0;color:#fff;text-decoration:none;'
            f'padding:12px 18px;border-radius:12px;font-weight:700;display:inline-block;">{html_lib.escape(label)}</a></p>')


def first_name(full_name: str) -> str:
    import re

    name = re.sub(r"^(Dott\.(ssa)?|Prof\.(ssa)?|Sig\.(ra)?)\s*", "", full_name or "", flags=re.IGNORECASE).strip()
    return name.split(" ")[0] if name else "ciao"


def access_link_mail(guest: Guest, link: str, event_data: dict) -> tuple[str, str, str]:
    ev = event_summary(event_data)
    subject = f"Il tuo link personale per {ev['title']}"
    text = (
        f"Ciao {first_name(guest.full_name)},\n\n"
        f"ecco il tuo link personale per la festa di specializzazione ({ev['partyDate']}"
        f"{', ' + ev['location'] if ev['location'] else ''}).\n"
        f"Da qui confermi la partecipazione, indichi chi viene con te e il menu, e trovi tutto il resto:\n\n{link}\n\n"
        "Il link è personale: non inoltrarlo. Se non l'hai richiesto tu, ignora questa email.\n\nA presto!"
    )
    body = (
        f"<p>Ciao {html_lib.escape(first_name(guest.full_name))},</p>"
        f"<p>ecco il tuo link personale per la festa di specializzazione (<b>{html_lib.escape(ev['partyDate'])}</b>"
        f"{', ' + html_lib.escape(ev['location']) if ev['location'] else ''}). Da qui confermi la partecipazione, indichi chi viene con te e il menu, e trovi tutto il resto.</p>"
        + _button(link, "Apri il mio profilo")
        + f'<p style="font-size:13px;color:#5B6677;">Oppure copia questo indirizzo: <a href="{html_lib.escape(link)}">{html_lib.escape(link)}</a></p>'
    )
    return subject, text, _layout("Il tuo link personale", body, "Il link è personale: non inoltrarlo. Se non l'hai richiesto tu, ignora questa email.")


def rsvp_confirmation_mail(guest: Guest, link: str, event_data: dict) -> tuple[str, str, str]:
    ev = event_summary(event_data)
    status = RSVP_LABELS.get(guest.rsvp_status, guest.rsvp_status)
    people = [f"{guest.full_name}: {MENU_LABELS.get(guest.menu, guest.menu)}"] + [
        f"{c.get('name') or 'accompagnatore'}: {MENU_LABELS.get(c.get('menu', ''), c.get('menu', ''))}"
        + (f" ({c.get('notes')})" if c.get("notes") else "") for c in guest.companions
    ]
    subject = f"{status}: {guest.full_name} ({guest.guests_count} {'persona' if guest.guests_count == 1 else 'persone'})"
    if guest.rsvp_status == "DECLINED":
        subject = f"Ci dispiace non averti con noi, {first_name(guest.full_name)}"
    lines = [
        f"Ciao {first_name(guest.full_name)},",
        "",
        f"abbiamo registrato la tua risposta per la festa di {ev['partyDate']}{' alle ' + ev['partyTime'] if ev['partyTime'] else ''}"
        f"{', ' + ev['location'] if ev['location'] else ''}.",
        f"Partecipazione: {status}",
    ]
    if guest.rsvp_status != "DECLINED":
        lines += [f"Persone: {guest.guests_count}", "Menu:"] + [f"- {p}" for p in people]
        if guest.dietary_notes.strip():
            lines.append(f"Allergie o note: {guest.dietary_notes.strip()}")
    lines += ["", f"Per cambiare qualcosa apri il tuo profilo: {link}", "", "A presto!"]
    text = "\n".join(lines)
    e = html_lib.escape
    body = f"<p>Ciao {e(first_name(guest.full_name))},</p><p>abbiamo registrato la tua risposta per la festa di <b>{e(ev['partyDate'])}</b>" \
           f"{' alle ' + e(ev['partyTime']) if ev['partyTime'] else ''}{', ' + e(ev['location']) if ev['location'] else ''}.</p>" \
           f"<p><b>Partecipazione:</b> {e(status)}</p>"
    if guest.rsvp_status != "DECLINED":
        body += f"<p><b>Persone:</b> {guest.guests_count}</p><p><b>Menu</b></p><ul>" + "".join(f"<li>{e(p)}</li>" for p in people) + "</ul>"
        if guest.dietary_notes.strip():
            body += f"<p><b>Allergie o note:</b> {e(guest.dietary_notes.strip())}</p>"
    body += _button(link, "Modifica il mio profilo")
    return subject, text, _layout("Risposta registrata", body, "Hai ricevuto questa email perché hai aggiornato il tuo profilo nell'app della festa.")


def test_mail(event_data: dict) -> tuple[str, str, str]:
    ev = event_summary(event_data)
    subject = "Email di prova da NeuroParty"
    text = f"Se leggi questo messaggio, il collegamento backend -> n8n -> email funziona.\nFesta: {ev['partyDate']}."
    body = f"<p>Se leggi questo messaggio, il collegamento backend → n8n → email funziona.</p><p>Festa: <b>{html_lib.escape(ev['partyDate'])}</b>.</p>"
    return subject, text, _layout("Email di prova", body, "Inviata dal pannello organizzatori.")


# ---------------------------------------------------------------------- consegna


class Mailer:
    """Accoda le email in mail_outbox e le consegna a n8n come evento mail.send."""

    def __init__(self, webhooks, session_factory, *, sync: bool = False):
        self.webhooks = webhooks
        self.session_factory = session_factory
        self.sync = sync
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mail")

    @property
    def enabled(self) -> bool:
        return self.webhooks.accepts("mail.send")

    def send(self, kind: str, to: str, subject: str, text: str, html: str = "", *, guest_id: int | None = None,
             reply_to: str = "", attachments: list[dict] | None = None) -> dict:
        payload = {"kind": kind, "to": to, "subject": subject, "text": text, "html": html, "replyTo": reply_to,
                   "attachments": attachments or []}
        with self.session_factory() as db:
            row = MailOutbox(kind=kind, to_email=to, subject=subject[:300], payload_json=json.dumps(payload, ensure_ascii=False), guest_id=guest_id)
            if not self.enabled:
                row.status = MAIL_SKIPPED
                row.error = "n8n non configurato (N8N_WEBHOOK_URL vuoto o evento mail.send escluso)"
            db.add(row)
            db.commit()
            db.refresh(row)
            result = row.to_dict()
            row_id = row.id
        if self.enabled:
            if self.sync:
                self._deliver(row_id, payload)
                return self.get(row_id)
            self._executor.submit(self._deliver_logged, row_id, payload)
        return result

    def resend(self, row_id: int) -> dict | None:
        with self.session_factory() as db:
            row = db.get(MailOutbox, row_id)
            if row is None:
                return None
            payload = json.loads(row.payload_json or "{}")
            if not self.enabled:
                row.status = MAIL_SKIPPED
                row.error = "n8n non configurato"
                db.commit()
                return row.to_dict()
            row.status = MAIL_QUEUED
            db.commit()
        if self.sync:
            self._deliver(row_id, payload)
        else:
            self._executor.submit(self._deliver_logged, row_id, payload)
        return self.get(row_id)

    def get(self, row_id: int) -> dict | None:
        with self.session_factory() as db:
            row = db.get(MailOutbox, row_id)
            return row.to_dict() if row else None

    def _deliver_logged(self, row_id: int, payload: dict) -> None:
        try:
            self._deliver(row_id, payload)
        except Exception:  # noqa: BLE001
            log.exception("Errore inatteso nell'invio dell'email #%s", row_id)

    def _deliver(self, row_id: int, payload: dict) -> None:
        result = self.webhooks.emit("mail.send", {**payload, "outboxId": row_id}, sync=True) or {}
        with self.session_factory() as db:
            row = db.get(MailOutbox, row_id)
            if row is None:
                return
            row.attempts += int(result.get("attempts") or 1)
            if result.get("delivered"):
                row.status = MAIL_DELIVERED
                row.delivered_at = now_ms()
                row.error = ""
            else:
                row.status = MAIL_FAILED
                row.error = str(result.get("error") or "consegna a n8n non riuscita")[:500]
            db.commit()

    def close(self) -> None:
        self._executor.shutdown(wait=False)
