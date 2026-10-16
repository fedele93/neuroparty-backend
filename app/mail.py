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
    "graduate_link": "Link del festeggiato",
    "rsvp_confirmation": "Conferma partecipazione",
    "menu_reminder": "Promemoria menu",
    "bus_confirmation": "Conferma navetta",
    "bus_waitlist": "Navetta: lista d'attesa",
    "bus_promoted": "Navetta: posto confermato",
    "bus_cancelled": "Navetta: prenotazione annullata",
    "bus_reminder": "Promemoria navetta",
    "pool_confirmation": "Quota unica registrata",
    "pool_reminder": "Promemoria quota unica",
    "wish_received": "Nuovo augurio per te",
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
    bus = (event_data.get("busSchedule") or {}).get("andata") or {}
    return {
        "title": program.get("title") or "NeuroParty",
        "partyDate": format_date_it(sch.get("partyDate", "")),
        "partyDateIso": sch.get("partyDate", ""),
        "partyTime": sch.get("partyTime") or "",
        # luogo della festa (partyLocationLabel); locationLabel elenca anche l'aula della seduta
        "location": program.get("partyLocationLabel") or program.get("locationLabel") or "",
        "busDepartureTime": sch.get("busDepartureTime") or "",
        "busFrom": bus.get("from") or "",
        "busTo": bus.get("to") or "",
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


def graduate_link_mail(target, link: str, event_data: dict) -> tuple[str, str, str]:
    """Link personale del festeggiato: apre l'area 🎓 (chi viene, auguri, coordinate, avatar)."""
    ev = event_summary(event_data)
    name = first_name(target.short_name)
    subject = f"La tua area festeggiato per {ev['title']}"
    text = (
        f"Ciao {name},\n\n"
        "ecco il tuo link personale da festeggiato: apre la tua area nell'app, con chi viene alla festa, "
        "gli auguri che ti scrivono, le coordinate per i regali e il tuo avatar dell'assistente.\n\n"
        f"{link}\n\n"
        "Il link è personale: non inoltrarlo. Se non l'hai richiesto tu, ignora questa email.\n\nA presto!"
    )
    body = (
        f"<p>Ciao {html_lib.escape(name)},</p>"
        "<p>ecco il tuo link personale da festeggiato: apre la tua area nell'app, con chi viene alla festa, "
        "gli auguri che ti scrivono, le coordinate per i regali e il tuo avatar dell'assistente.</p>"
        + _button(link, "Apri la mia area 🎓")
        + f'<p style="font-size:13px;color:#5B6677;">Oppure copia questo indirizzo: <a href="{html_lib.escape(link)}">{html_lib.escape(link)}</a></p>'
    )
    return subject, text, _layout("La tua area festeggiato", body, "Il link è personale: non inoltrarlo. Se non l'hai richiesto tu, ignora questa email.")


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


def menu_reminder_mail(guest: Guest, link: str, event_data: dict, deadline_text: str) -> tuple[str, str, str]:
    ev = event_summary(event_data)
    missing = [guest.full_name] if not guest.menu else []
    missing += [c.get("name") or "accompagnatore" for c in guest.companions if not c.get("menu")]
    subject = "Manca il tuo menu per la festa" if len(missing) <= 1 else f"Mancano i menu per {len(missing)} persone"
    who = ", ".join(missing) or "te"
    text = (f"Ciao {first_name(guest.full_name)},\n\nper organizzare la cena del {ev['partyDate']} ci serve il menu di: {who}.\n"
            + (f"Puoi sceglierlo fino al {deadline_text}.\n" if deadline_text else "") + f"\nApri il tuo profilo: {link}\n\nGrazie!")
    e = html_lib.escape
    body = (f"<p>Ciao {e(first_name(guest.full_name))},</p><p>per organizzare la cena del <b>{e(ev['partyDate'])}</b> ci serve il menu di: <b>{e(who)}</b>.</p>"
            + (f"<p>Puoi sceglierlo fino al <b>{e(deadline_text)}</b>.</p>" if deadline_text else "") + _button(link, "Scegli il menu"))
    return subject, text, _layout("Manca il menu", body, "Promemoria dall'app della festa.")


def bus_ics(event_data: dict, booking, public_url: str = "") -> str:
    """File calendario per la partenza della navetta (orario dal blocco schedule; senza orario, tutto il giorno)."""
    from .schedule import VTIMEZONE_ROME, _ics_escape, _parse_date, _parse_time, _vevent

    ev = event_summary(event_data)
    day = _parse_date(ev["partyDateIso"])
    if day is None:
        return ""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//NeuroParty//Backend//IT", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:" + _ics_escape("Navetta festa di specializzazione")] + VTIMEZONE_ROME
    where = booking.pickup_stop or ev["busFrom"]
    desc = f"Prenotazione {booking.code}: {booking.seats_count} posti" + (f", fermata {booking.pickup_stop}" if booking.pickup_stop else "") + \
           (". Ritorno previsto." if booking.return_trip_wanted else "") + f" Arrivo: {ev['busTo']}."
    lines += _vevent(f"neuroparty-navetta-{booking.code or booking.id}@neurospec", "Navetta per la festa di specializzazione",
                     where, desc, day, _parse_time(ev["busDepartureTime"]), 1, public_url)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def bus_mail(kind: str, guest: Guest, booking, request) -> tuple[str, str, str, list[dict]]:
    """Email sulla navetta (conferma, lista d'attesa, posto confermato, annullata, promemoria)
    con il file calendario allegato quando la prenotazione è confermata."""
    import base64

    from .deps import base_url_for

    event_data = request.app.state.event_data
    ev = event_summary(event_data)
    link = f"{base_url_for(request)}/#profilo={guest.access_token}" if guest.access_token else base_url_for(request)
    when = ev["partyDate"] + (f" alle {ev['busDepartureTime']}" if ev["busDepartureTime"] else " (orario di partenza da confermare)")
    stop = booking.pickup_stop or ev["busFrom"] or "fermata da indicare"
    seats = f"{booking.seats_count} {'posto' if booking.seats_count == 1 else 'posti'}"
    e = html_lib.escape
    attachments: list[dict] = []
    if kind == "bus_waitlist":
        subject = f"Navetta: sei in lista d'attesa ({seats})"
        text = (f"Ciao {first_name(guest.full_name)},\n\nla navetta per la festa è al completo: ti abbiamo messo in lista d'attesa per {seats} "
                f"(codice {booking.code}). Se si liberano posti ti confermiamo subito via email.\n\nPartenza: {when}, da {stop}.\n"
                f"Modifica o annulla dal tuo profilo: {link}")
        body = (f"<p>Ciao {e(first_name(guest.full_name))},</p><p>la navetta per la festa è al completo: ti abbiamo messo in <b>lista d'attesa</b> per {seats} "
                f"(codice <b>{e(booking.code)}</b>). Se si liberano posti ti confermiamo subito via email.</p><p>Partenza: {e(when)}, da {e(stop)}.</p>" + _button(link, "Il mio profilo"))
        title = "Lista d'attesa navetta"
    elif kind == "bus_cancelled":
        subject = f"Navetta: prenotazione {booking.code} annullata"
        text = f"Ciao {first_name(guest.full_name)},\n\nla tua prenotazione sulla navetta ({seats}, codice {booking.code}) è stata annullata.\nSe cambi idea prenota di nuovo dal profilo: {link}"
        body = f"<p>Ciao {e(first_name(guest.full_name))},</p><p>la tua prenotazione sulla navetta ({seats}, codice <b>{e(booking.code)}</b>) è stata annullata.</p>" + _button(link, "Prenota di nuovo")
        title = "Prenotazione annullata"
    else:
        intro = {"bus_confirmation": "la tua prenotazione sulla navetta è confermata",
                 "bus_promoted": "si è liberato un posto: la tua prenotazione sulla navetta è confermata",
                 "bus_reminder": "ti ricordiamo la navetta per la festa"}.get(kind, "la tua prenotazione sulla navetta è confermata")
        subject = {"bus_promoted": f"Navetta: posto confermato ({seats})", "bus_reminder": f"Domani la navetta: {seats}, {stop}"}.get(kind, f"Navetta confermata: {seats} (codice {booking.code})")
        text = (f"Ciao {first_name(guest.full_name)},\n\n{intro}.\nCodice: {booking.code}\nPosti: {booking.seats_count}\nPartenza: {when}\nDa: {stop}\n"
                f"Arrivo: {ev['busTo']}\nRitorno: {'sì' if booking.return_trip_wanted else 'no'}\n\n"
                f"Alla partenza basta dire il tuo nome o mostrare il codice. In allegato l'evento per il calendario.\n"
                f"Modifica o annulla dal tuo profilo: {link}")
        body = (f"<p>Ciao {e(first_name(guest.full_name))},</p><p>{e(intro)}.</p>"
                f"<p><b>Codice:</b> {e(booking.code)}<br><b>Posti:</b> {booking.seats_count}<br><b>Partenza:</b> {e(when)}<br><b>Da:</b> {e(stop)}<br>"
                f"<b>Arrivo:</b> {e(ev['busTo'])}<br><b>Ritorno:</b> {'sì' if booking.return_trip_wanted else 'no'}</p>"
                f"<p>Alla partenza basta dire il tuo nome o mostrare il codice. In allegato l'evento per il calendario.</p>" + _button(link, "Il mio profilo"))
        title = {"bus_promoted": "Posto confermato", "bus_reminder": "Promemoria navetta"}.get(kind, "Navetta confermata")
        ics = bus_ics(event_data, booking, base_url_for(request))
        if ics:
            attachments.append({"filename": "navetta.ics", "contentType": "text/calendar", "contentBase64": base64.b64encode(ics.encode("utf-8")).decode("ascii")})
    return subject, text, _layout(title, body, "Email automatica dall'app della festa: per cambiare qualcosa usa il tuo profilo."), attachments


def pool_mail(kind: str, contribution, collector: dict, app_url: str) -> tuple[str, str, str]:
    """Al donatore: conferma della quota unica registrata (con IBAN/causale del cassiere) o promemoria se ancora in attesa."""
    e = html_lib.escape
    amount = f"{contribution.total_amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"
    reason = f"{collector.get('transferReason') or 'Regalo specializzazione'} - {contribution.donor_name}"
    parts = ", ".join(f"{a.graduate_name} {a.amount:.2f} €".replace(".", ",") for a in contribution.allocations)
    how = {
        "IBAN": f"Bonifico a {collector.get('ibanHolder') or collector.get('name')}, IBAN {collector.get('iban')}, causale: {reason}",
        "PayPal": f"PayPal: {collector.get('paypalMeUrl')} (indica il tuo nome nel messaggio)",
        "Contanti": f"Consegna i contanti a {collector.get('name')} (in reparto o alla festa)",
    }.get(contribution.payment_method, "")
    if kind == "pool_reminder":
        subject = f"Promemoria: la tua quota unica di {amount} risulta ancora in attesa"
        intro = f"la quota unica di {amount} che hai registrato per i neo-specialisti risulta ancora in attesa da parte di {collector.get('name')}."
        title = "Quota in attesa"
    else:
        subject = f"Quota unica registrata: {amount} a {collector.get('name')}"
        intro = f"abbiamo registrato la tua quota unica di {amount} a {collector.get('name')}, che la ripartirà fra i neo-specialisti."
        title = "Quota unica registrata"
    text = (f"Ciao {first_name(contribution.donor_name)},\n\n{intro}\n\nCome versarla ({contribution.payment_method}):\n{how}\n\n"
            f"Ripartizione: {parts}\n\nSe hai già versato, non devi fare altro: il cassiere la segnerà come ricevuta.\nApp della festa: {app_url}")
    body = (f"<p>Ciao {e(first_name(contribution.donor_name))},</p><p>{e(intro)}</p>"
            f"<p><b>Come versarla ({e(contribution.payment_method)}):</b><br>{e(how)}</p><p><b>Ripartizione:</b> {e(parts)}</p>"
            f"<p>Se hai già versato, non devi fare altro: il cassiere la segnerà come ricevuta.</p>" + _button(app_url, "Apri l'app della festa"))
    return subject, text, _layout(title, body, "Email automatica dall'app della festa.")


def wish_received_mail(target, wish, app_url: str) -> tuple[str, str, str]:
    """Al festeggiato: è arrivato un augurio per lui."""
    e = html_lib.escape
    subject = f"Nuovo augurio da {wish.author_name}"
    text = (f"Ciao {first_name(target.name)},\n\n{wish.author_name} ti ha scritto in bacheca:\n\n\u201c{wish.message}\u201d\n\n"
            f"Leggi tutti gli auguri nell'app: {app_url}")
    body = (f"<p>Ciao {e(first_name(target.name))},</p><p><b>{e(wish.author_name)}</b> ti ha scritto in bacheca:</p>"
            f"<blockquote style=\"border-left:3px solid #C9A227;margin:0;padding:6px 12px;color:#0F1B2D;\">{e(wish.message)}</blockquote>"
            + _button(app_url, "Leggi gli auguri"))
    return subject, text, _layout("Nuovo augurio per te", body, "Ricevi questa email perché sei uno dei festeggiati.")


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
