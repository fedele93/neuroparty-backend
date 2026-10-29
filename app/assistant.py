"""Motore dell'assistente vocale: prompt con i dati della festa, strumenti che agiscono
sull'app (RSVP, navetta, auguri, navigazione) e ciclo trascrizione -> conversazione -> voce.

Le azioni che scrivono dati riusano le stesse funzioni dei router (validazione, controllo dei
posti, webhook n8n), così l'assistente non può fare nulla che l'app non permetta già.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import graduate_for_token, lists_allowed
from .auth import is_admin as auth_is_admin
from .mistral import MistralClient, MistralError
from .models import AssistantAvatar, GiftTarget, Guest, Wish
from .routers import bus as bus_router
from .routers import guests as guests_router
from .routers import profile as profile_router
from .routers.photos import photos_total
from .routers import wishes as wishes_router
from .schemas import BookingIn, CompanionIn, GuestIn, GuestUpdate, ProfileBusIn, ProfileIn, ProfileLinkRequest, WishIn
from .seed import gift_collectors_info, public_event_info

log = logging.getLogger("neuroparty.assistant")

MAX_HISTORY = 12
MAX_TOOL_ROUNDS = 4
SECTIONS = {"program": "Programma", "rsvp": "Invitati", "bus": "Navetta", "wishes": "Bacheca auguri", "gifts": "Regali"}
RSVP_LABELS = {"CONFIRMED": "confermato", "PENDING": "in attesa", "DECLINED": "declinato"}

# ---------------------------------------------------------------------- strumenti

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_bus_availability",
            "description": "Posti liberi, prenotati e totali sulla navetta per la festa.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_guest",
            "description": "Cerca un invitato per nome (anche parziale) e restituisce il suo stato RSVP.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Nome o parte del nome"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_rsvp",
            "description": "Conferma, mette in attesa o declina la partecipazione di una persona alla festa (la crea se non esiste). "
                           "Chiamala SOLO dopo che l'utente ha confermato nome, numero di persone ed eventuali esigenze alimentari.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fullName": {"type": "string"},
                    "status": {"type": "string", "enum": ["CONFIRMED", "PENDING", "DECLINED"]},
                    "guestsCount": {"type": "integer", "minimum": 0, "maximum": 50, "description": "Persone in totale, compreso l'invitato"},
                    "category": {"type": "string", "description": "Es. Familiari, Colleghi Reparto, Amici Università"},
                    "dietaryNotes": {"type": "string", "description": "Esigenze alimentari, vuoto se nessuna"},
                },
                "required": ["fullName", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_bus",
            "description": "Prenota posti sulla navetta a nome di un'altra persona: SOLO per gli organizzatori (gli invitati prenotano dal "
                           "profilo con book_my_bus). Chiamala SOLO dopo la conferma esplicita dell'utente su nome, numero di posti e fermata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "passengerName": {"type": "string"},
                    "seatsCount": {"type": "integer", "minimum": 1, "maximum": 40},
                    "pickupStop": {"type": "string", "description": "Una delle fermate previste"},
                    "returnTripWanted": {"type": "boolean"},
                    "contactPhone": {"type": "string"},
                },
                "required": ["passengerName", "seatsCount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "post_wish",
            "description": "Pubblica un augurio in bacheca. Chiamala SOLO dopo aver riletto il testo all'utente e avuto il suo ok.",
            "parameters": {
                "type": "object",
                "properties": {
                    "authorName": {"type": "string"},
                    "targetGraduate": {"type": "string", "description": "Nome del neo-specialista oppure 'Tutti i Laureandi'"},
                    "message": {"type": "string"},
                },
                "required": ["authorName", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_gift_options",
            "description": "Come fare un regalo diretto a un neo-specialista (IBAN, PayPal con link o email, Satispay, BANCOMAT Pay) "
                           "e i cassieri della quota unica. Senza 'graduate' restituisce tutti. Non leggere l'IBAN intero ad alta voce "
                           "se non te lo chiedono: dì quali metodi ci sono e apri la sezione Regali, dove si copia con un tocco.",
            "parameters": {
                "type": "object",
                "properties": {"graduate": {"type": "string", "description": "Nome o cognome del neo-specialista (facoltativo)"}},
            },
        },
    },
]

# Solo per chi vede foto e auguri (registrati e ruoli): agli altri il prompt spiega come registrarsi
MEMBER_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_wishes",
            "description": "Gli auguri più recenti in bacheca (anche solo quelli per un neo-specialista) e quante foto ci sono in galleria. "
                           "Funziona solo per chi è registrato o ha un ruolo: altrimenti spiega come registrarsi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "forGraduate": {"type": "string", "description": "Nome del neo-specialista (facoltativo)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                },
            },
        },
    },
]

TOOLS += [
    {
        "type": "function",
        "function": {
            "name": "send_profile_link",
            "description": "Manda all'email indicata il link personale del profilo (serve per vedere e pubblicare foto e auguri, "
                           "confermare la presenza con menu e accompagnatori, prenotare la navetta). Se la persona non è in lista serve anche "
                           "il nome. Chiamala SOLO dopo aver ripetuto l'email all'utente e avuto il suo sì.",
            "parameters": {
                "type": "object",
                "properties": {"email": {"type": "string"}, "fullName": {"type": "string", "description": "Nome e cognome, se non è in lista"}},
                "required": ["email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_section",
            "description": "Apre una sezione dell'app sul telefono dell'utente.",
            "parameters": {
                "type": "object",
                "properties": {"section": {"type": "string", "enum": list(SECTIONS.keys())}},
                "required": ["section"],
            },
        },
    },
]

MENU_LABELS = {"": "da scegliere", "STANDARD": "menu completo", "VEGETARIANO": "vegetariano", "VEGANO": "vegano", "SENZA_GLUTINE": "senza glutine", "ALTRO": "altro"}
MENU_VALUES = ["STANDARD", "VEGETARIANO", "VEGANO", "SENZA_GLUTINE", "ALTRO"]

# Strumenti disponibili solo quando l'utente ha aperto il proprio profilo (X-Guest-Token)
PROFILE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_my_profile",
            "description": "Il profilo dell'utente che sta parlando: partecipazione, menu e allergie, accompagnatori con il loro menu, "
                           "prenotazione della navetta, date limite. Usalo per rispondere a domande su 'il mio' profilo/menu/navetta.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_my_profile",
            "description": "Aggiorna il profilo dell'utente: partecipazione, menu, allergie, accompagnatori (nome e menu). "
                           "Passa solo i campi da cambiare. Chiamala SOLO dopo aver riassunto le modifiche e avuto il sì dell'utente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rsvpStatus": {"type": "string", "enum": ["CONFIRMED", "PENDING", "DECLINED"]},
                    "menu": {"type": "string", "enum": MENU_VALUES, "description": "Menu dell'utente"},
                    "dietaryNotes": {"type": "string", "description": "Allergie o esigenze, vuoto se nessuna"},
                    "companions": {
                        "type": "array", "description": "Elenco COMPLETO degli accompagnatori (sostituisce quello attuale)",
                        "items": {"type": "object", "properties": {"name": {"type": "string"}, "menu": {"type": "string", "enum": MENU_VALUES}, "notes": {"type": "string"}}, "required": ["name"]},
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_my_bus",
            "description": "Prenota o modifica la navetta dal profilo dell'utente (una sola prenotazione per profilo; se i posti sono finiti "
                           "entra in lista d'attesa). Chiamala SOLO dopo conferma esplicita di posti e fermata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seatsCount": {"type": "integer", "minimum": 1, "maximum": 40, "description": "Se omesso: le persone del profilo"},
                    "pickupStop": {"type": "string", "description": "Una delle fermate previste"},
                    "returnTripWanted": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_my_bus",
            "description": "Annulla la prenotazione della navetta dell'utente. Chiamala SOLO dopo conferma esplicita.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# Solo quando parla un festeggiato (X-Graduate-Token valido, anche dal link via email)
GRADUATE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_my_graduate_summary",
            "description": "Per il festeggiato che sta parlando: quanti hanno confermato e quante persone in tutto, chi è ancora in attesa, "
                           "quanti auguri ha ricevuto (gli ultimi messaggi), i posti prenotati sulla navetta.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

WRITE_TOOLS = {"set_rsvp", "book_bus", "post_wish", "update_my_profile", "book_my_bus", "cancel_my_bus", "send_profile_link"}


@dataclass(frozen=True)
class Caller:
    """Chi sta parlando, risolto una volta per turno dagli header: profilo invitato, festeggiato, accesso a foto e auguri."""
    guest: Guest | None = None
    graduate_id: str | None = None
    can_see_memories: bool = False
    is_admin: bool = False

    @classmethod
    def resolve(cls, request: Request, db: Session, guest: Guest | None) -> "Caller":
        can_see = guest is not None or lists_allowed(request, db)
        return cls(guest=guest, graduate_id=graduate_for_token(request, request.headers.get("x-graduate-token"), db), can_see_memories=can_see,
                   is_admin=auth_is_admin(request, request.headers.get("x-admin-token")))


def tools_for(guest: Guest | None, graduate_id: str | None = None, can_see_memories: bool = True) -> list[dict]:
    return (TOOLS + (MEMBER_TOOLS if can_see_memories else []) + (PROFILE_TOOLS if guest is not None else [])
            + (GRADUATE_TOOLS if graduate_id else []))


def _fmt_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or "data da definire"
    days = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    months = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    return f"{days[d.weekday()]} {d.day} {months[d.month - 1]} {d.year}"


def event_facts(event_data: dict, max_seats: int) -> str:
    """I fatti della festa per il prompt di sistema (testi con gli orari già risolti)."""
    ev = public_event_info(event_data)
    sch = ev["schedule"]
    prog = ev["program"]
    busd = ev["busSchedule"]
    collectors = gift_collectors_info(event_data)
    collectors_text = " oppure ".join(
        f"{c.get('name') or ''} ({', '.join(c.get('paymentMethods', []))})" for c in collectors
    ) or "un cassiere"
    lines = [
        f"Evento: {prog.get('title', '')} - {prog.get('subtitle', '')}.",
        f"Neo-specialisti festeggiati: {', '.join(ev.get('graduates', []))}.",
        f"Seduta di specializzazione: {_fmt_date(sch.get('ceremonyDate', ''))}"
        + (f" alle {sch['ceremonyTime']}" if sch.get("ceremonyTime") else " (orario da definire)") + ".",
        f"Festa: {_fmt_date(sch.get('partyDate', ''))}"
        + (f" dalle {sch['partyTime']}" if sch.get("partyTime") else " (orario da definire)")
        + (f" alle {sch['partyEndTime']}" if sch.get("partyEndTime") else "") + ".",
        f"Luoghi: {prog.get('locationLabel', '')}.",
        "Programma:",
    ]
    for t in prog.get("timeline", []):
        lines.append(f"- {t.get('time', '')}: {t.get('title', '')} @ {t.get('location', '')}. {t.get('details', '')}")
    lines.append("Punti di ritrovo:")
    for p in ev.get("mapPoints", []):
        lines.append(f"- {p.get('title', '')} ({p.get('timeLabel', '')}): {p.get('address', '')}. {p.get('description', '')}")
    lines += [
        f"Navetta gratuita ({max_seats} posti): {busd.get('subtitle', '')}.",
        f"- Andata {busd.get('andata', {}).get('timeLabel', '')}: da {busd.get('andata', {}).get('from', '')} a "
        f"{busd.get('andata', {}).get('to', '')}. {busd.get('andata', {}).get('notes', '')}",
        f"- Ritorno {busd.get('ritorno', {}).get('timeLabel', '')}: da {busd.get('ritorno', {}).get('from', '')} a "
        f"{busd.get('ritorno', {}).get('to', '')}. {busd.get('ritorno', {}).get('notes', '')}",
        f"- Fermate di salita: {', '.join(busd.get('pickupStops', []))}.",
        "Regali: nella sezione Regali dell'app ogni neo-specialista ha IBAN e, dove ci sono, PayPal (link o email a cui inviare), Satispay o BANCOMAT Pay (numero di telefono) per un regalo diretto. "
        f"Chi preferisce un solo versamento per tutti registra una quota unica a uno dei cassieri, {collectors_text}, "
        "che la ripartisce fra i neo-specialisti. "
        "Non esistono cifre suggerite né totali raccolti: se te lo chiedono, spiega che ognuno decide liberamente.",
    ]
    return "\n".join(lines)


def profile_facts(guest: Guest | None) -> str:
    """Riga di contesto sul profilo di chi parla (se ha aperto il link personale)."""
    if guest is None:
        return ("L'utente NON ha aperto il proprio profilo personale: se chiede del suo menu, dei suoi accompagnatori o della sua navetta, "
                "spiega che deve aprire il profilo dal pulsante con l'omino in alto (o dal link ricevuto via email) e offri di aprire la sezione Invitati.")
    comps = ", ".join(f"{c.get('name') or 'accompagnatore'} ({MENU_LABELS.get(c.get('menu') or '', 'da scegliere')})" for c in guest.companions) or "nessuno"
    return (f"L'utente che parla ha il profilo di {guest.full_name} ({RSVP_LABELS.get(guest.rsvp_status, guest.rsvp_status)}, "
            f"menu {MENU_LABELS.get(guest.menu or '', 'da scegliere')}, accompagnatori: {comps}). Rivolgiti a lui per nome. "
            "Per il SUO profilo, menu, accompagnatori e navetta usa get_my_profile, update_my_profile, book_my_bus e cancel_my_bus "
            "(non set_rsvp o book_bus, che servono per altre persone).")


def access_facts(can_see_memories: bool, graduate_name: str | None) -> str:
    """Cosa può fare chi parla: foto e auguri (solo registrati e ruoli) e se è uno dei festeggiati."""
    lines = []
    if graduate_name:
        lines.append(f"Chi parla è {graduate_name}, uno dei festeggiati: per i suoi numeri (chi viene, auguri ricevuti) usa get_my_graduate_summary. "
                     "Ricordagli che la sua area 🎓 è in alto nella scheda Programma.")
    if can_see_memories:
        lines.append("L'utente può vedere e pubblicare foto e auguri.")
    else:
        lines.append("L'utente NON è registrato: foto e auguri sono riservati a chi ha aperto il link personale ricevuto via email "
                     "(vale anche se non viene alla festa). Se vuole vederli o pubblicarne, offri di mandargli il link con send_profile_link "
                     "(chiedi l'email, e il nome se non è in lista) oppure di aprire la Bacheca, dove può richiederlo. "
                     "I festeggiati entrano dalla Bacheca o da ⚙️ con 'Sono un festeggiato'.")
    return " ".join(lines)


def system_prompt(avatar: AssistantAvatar, event_data: dict, max_seats: int, now: datetime | None = None, guest: Guest | None = None,
                  can_see_memories: bool = True, graduate_name: str | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    persona = avatar.persona.strip() or f"Sei {avatar.short_name}, uno dei neo-specialisti festeggiati."
    return (
        f"Sei {avatar.name}, che parla in prima persona come {avatar.short_name}. {persona}\n"
        "Sei l'assistente vocale dell'app NeuroParty per la festa di specializzazione in Neurologia a Bari. "
        "Rispondi SEMPRE in italiano, con tono gentile, caloroso e informale, come a un amico o a un parente. "
        "Le risposte vengono lette ad alta voce: usa frasi brevi (di norma 1-3 frasi), niente elenchi puntati, "
        "niente markdown, niente emoji, scrivi i numeri e gli orari in modo naturale (es. 'alle nove e mezza').\n"
        "Usa solo le informazioni qui sotto e gli strumenti: se non sai una cosa, dillo con garbo e suggerisci di chiedere agli organizzatori. "
        "Non inventare orari, indirizzi o nomi. Se un orario è 'da definire', dillo.\n"
        "Puoi agire sull'app con gli strumenti: leggere i posti liberi sulla navetta, cercare un invitato, confermare o modificare "
        "un RSVP, prenotare la navetta, dire come fare un regalo a un neo-specialista, leggere gli ultimi auguri, pubblicare un augurio, "
        "mandare il link personale via email, aprire una sezione. Prima di ogni azione che scrive dati o manda email "
        "(set_rsvp, book_bus, post_wish, send_profile_link) riassumi i dati e chiedi conferma; chiama lo strumento solo dopo un sì esplicito. "
        "In galleria le foto sono dalla più recente: si toccano per ingrandirle e il pulsante in fondo carica le precedenti. "
        "Dopo l'azione conferma in una frase cosa hai fatto. Se manca un dato indispensabile (es. il nome), chiedilo.\n"
        f"Oggi è {_fmt_date(now.date().isoformat())}.\n"
        + profile_facts(guest) + "\n" + access_facts(can_see_memories, graduate_name) + "\n\n"
        "DATI DELLA FESTA\n" + event_facts(event_data, max_seats)
    )


# ---------------------------------------------------------------------- esecuzione strumenti


def _guest_dict(g: Guest) -> dict:
    return {"id": g.id, "fullName": g.full_name, "category": g.category, "rsvpStatus": g.rsvp_status,
            "rsvpLabel": RSVP_LABELS.get(g.rsvp_status, g.rsvp_status), "guestsCount": g.guests_count, "dietaryNotes": g.dietary_notes}


def _find_guests(db: Session, name: str) -> list[Guest]:
    q = (name or "").strip().lower()
    if not q:
        return []
    return db.scalars(select(Guest).where(func.lower(Guest.full_name).contains(q)).order_by(Guest.full_name.asc()).limit(5)).all()


def _profile_dict(guest: Guest, db: Session, request: Request) -> dict:
    """Il profilo in forma leggibile per il modello (etichette in italiano, niente token)."""
    p = profile_router._profile_payload(guest, db, request)
    g = p["guest"]
    booking = p["busBooking"]
    return {
        "fullName": g["fullName"], "rsvpStatus": g["rsvpStatus"], "rsvpLabel": RSVP_LABELS.get(g["rsvpStatus"], g["rsvpStatus"]),
        "guestsCount": g["guestsCount"], "menu": g["menu"], "menuLabel": MENU_LABELS.get(g["menu"] or "", "da scegliere"),
        "dietaryNotes": g["dietaryNotes"],
        "companions": [{"name": c.get("name"), "menu": c.get("menu") or "", "menuLabel": MENU_LABELS.get(c.get("menu") or "", "da scegliere"), "notes": c.get("notes") or ""} for c in g["companions"]],
        "consentGiven": g["consentAt"] is not None,
        "bus": None if booking is None else {
            "seatsCount": booking["seatsCount"], "pickupStop": booking["pickupStop"], "returnTripWanted": booking["returnTripWanted"],
            "status": booking["status"], "statusLabel": "in lista d'attesa" if booking["status"] == "WAITLIST" else "confermata", "code": booking["code"],
        },
        "deadlines": p["deadlines"],
    }


def _profile_summary(d: dict) -> str:
    comps = ", ".join(f"{c['name']} ({c['menuLabel']})" for c in d["companions"]) or "nessuno"
    bus = f"navetta {d['bus']['statusLabel']} per {d['bus']['seatsCount']} posti" + (f" da {d['bus']['pickupStop']}" if d['bus'] and d['bus']['pickupStop'] else "") if d["bus"] else "nessuna prenotazione navetta"
    return f"{d['fullName']}: {d['rsvpLabel']}, {d['guestsCount']} persone, menu {d['menuLabel']}, accompagnatori: {comps}; {bus}."


def execute_tool(name: str, args: dict, request: Request, db: Session, client_id: str | None, guest: Guest | None = None,
                 caller: Caller | None = None) -> dict:
    """Esegue uno strumento e ritorna un dizionario serializzabile per il modello."""
    caller = caller or Caller.resolve(request, db, guest)
    try:
        if name in {t["function"]["name"] for t in PROFILE_TOOLS}:
            if guest is None:
                return {"error": "L'utente non ha aperto il proprio profilo: deve aprirlo dal link personale (pulsante con l'omino in alto)", "navigate": "rsvp"}
            if name == "get_my_profile":
                d = _profile_dict(guest, db, request)
                return {"profile": d, "summary": _profile_summary(d)}
            if name == "update_my_profile":
                if guest.consent_at is None:
                    return {"error": "Prima di modificare il profilo serve il consenso al trattamento dei dati: va dato una volta aprendo il profilo", "navigate": "rsvp"}
                comps_in = args.get("companions")
                companions = [CompanionIn(name=str(c.get("name") or ""), menu=c.get("menu") or "", notes=str(c.get("notes") or "")) for c in (comps_in or []) if isinstance(c, dict)] \
                    if comps_in is not None else [CompanionIn(**c) for c in guest.companions]
                body = ProfileIn(
                    fullName=guest.full_name, category=guest.category, rsvpStatus=args.get("rsvpStatus") or guest.rsvp_status,
                    email=guest.email, phone=guest.phone, menu=args.get("menu") if args.get("menu") is not None else guest.menu,
                    dietaryNotes=args.get("dietaryNotes") if args.get("dietaryNotes") is not None else guest.dietary_notes,
                    companions=companions, consent=True,
                )
                profile_router.update_profile(body, request, guest, db)
                d = _profile_dict(guest, db, request)
                return {"profile": d, "summary": "Profilo aggiornato. " + _profile_summary(d), "navigate": "rsvp"}
            if name == "book_my_bus":
                body = ProfileBusIn(seatsCount=args.get("seatsCount"), pickupStop=args.get("pickupStop") or "", returnTripWanted=bool(args.get("returnTripWanted", True)))
                profile_router.set_bus(body, request, guest, db)
                d = _profile_dict(guest, db, request)
                b = d["bus"]
                return {"profile": d, "summary": f"Navetta {b['statusLabel']}: {b['seatsCount']} posti" + (f" da {b['pickupStop']}" if b["pickupStop"] else "") + f", codice {b['code']}.", "navigate": "bus"}
            if name == "cancel_my_bus":
                profile_router.cancel_bus(request, guest, db)
                return {"summary": "Prenotazione della navetta annullata.", "navigate": "bus"}
        if name == "get_bus_availability":
            return bus_router.bus_summary(request, db)
        if name == "find_guest":
            found = _find_guests(db, args.get("name", ""))
            if not lists_allowed(request, db):  # l'elenco è riservato: un invitato ritrova solo sé stesso
                found = [g for g in found if (client_id and g.owner_client_id == client_id) or (guest and g.id == guest.id)]
            return {"guests": [_guest_dict(g) for g in found], "count": len(found)}
        if name == "set_rsvp":
            full_name = (args.get("fullName") or "").strip()
            status = args.get("status", "CONFIRMED")
            if not full_name:
                return {"error": "Serve il nome della persona"}
            existing = [g for g in _find_guests(db, full_name) if g.full_name.strip().lower() == full_name.lower()]
            if existing:
                g = existing[0]
                patch = GuestUpdate(rsvpStatus=status, guestsCount=args.get("guestsCount"), dietaryNotes=args.get("dietaryNotes"))
                out = guests_router.update_guest(g.id, patch, request, db, client_id, False)
                return {"summary": f"RSVP di {out['fullName']} aggiornato: {RSVP_LABELS.get(status, status)}, {out['guestsCount']} persone.", "guest": out, "created": False}
            body = GuestIn(
                fullName=full_name, category=args.get("category") or "Invitato", rsvpStatus=status,
                guestsCount=int(args.get("guestsCount") or 1), dietaryNotes=args.get("dietaryNotes") or "",
            )
            out = guests_router.create_guest(body, request, db, client_id)
            return {"summary": f"{out['fullName']} inserito: {RSVP_LABELS.get(status, status)}, {out['guestsCount']} persone.", "guest": out, "created": True}
        if name == "book_bus":
            # la navetta si prenota dal profilo (book_my_bus): un nome libero non entra fra gli invitati
            if not caller.is_admin:
                return {"error": "La navetta si prenota dal proprio profilo, così chi sale è anche fra gli invitati: se l'utente ha il profilo usa book_my_bus, "
                                 "altrimenti proponi di aprire il link personale (posso mandarlo io con l'email: send_profile_link). Solo gli organizzatori prenotano per altri.",
                        "navigate": "rsvp"}
            body = BookingIn(
                passengerName=(args.get("passengerName") or "").strip(), seatsCount=int(args.get("seatsCount") or 1),
                pickupStop=args.get("pickupStop") or "", returnTripWanted=bool(args.get("returnTripWanted", True)),
                contactPhone=args.get("contactPhone") or "",
            )
            out = bus_router.create_booking(body, request, db, client_id, admin=True)
            summary = bus_router.bus_summary(request, db)
            return {"summary": f"Prenotati {out['seatsCount']} posti per {out['passengerName']}"
                    + (f" da {out['pickupStop']}" if out["pickupStop"] else "") + f". Restano {summary['availableSeats']} posti.",
                    "booking": out, "bus": summary}
        if name == "post_wish":
            if not caller.can_see_memories:
                return {"error": "Gli auguri li pubblica solo chi è registrato: l'utente deve aprire il link personale ricevuto via email (pulsante con l'omino in alto)", "navigate": "rsvp"}
            body = WishIn(authorName=args.get("authorName") or "Amico/a", targetGraduate=args.get("targetGraduate") or "Tutti i Laureandi",
                          message=(args.get("message") or "").strip())
            out = wishes_router.create_wish(body, request, db, caller.guest)
            return {"summary": f"Augurio di {out['authorName']} per {out['targetGraduate']} pubblicato in bacheca.", "wish": out, "navigate": "wishes"}
        if name == "get_gift_options":
            return _gift_options(db, request, args.get("graduate") or "")
        if name == "read_wishes":
            if not caller.can_see_memories:
                return {"error": "Gli auguri e le foto li vede solo chi è registrato: serve il link personale ricevuto via email (posso mandarlo io con l'email)", "navigate": "wishes"}
            return _recent_wishes(db, args.get("forGraduate") or "", int(args.get("limit") or 3))
        if name == "send_profile_link":
            body = ProfileLinkRequest(email=args.get("email") or "", fullName=args.get("fullName") or "")
            out = profile_router.request_link(body, request, db)
            return {"summary": out["message"], "queued": out["queued"]} if out.get("queued") else {"error": out["message"]}
        if name == "get_my_graduate_summary":
            if not caller.graduate_id:
                return {"error": "Questa informazione è solo per i festeggiati"}
            return _graduate_summary(request, db, caller.graduate_id)
        if name == "open_section":
            section = args.get("section")
            if section not in SECTIONS:
                return {"error": "Sezione sconosciuta"}
            return {"navigate": section, "summary": f"Sezione {SECTIONS[section]} aperta."}
        return {"error": f"Strumento sconosciuto: {name}"}
    except HTTPException as e:  # es. posti esauriti
        return {"error": str(e.detail)}
    except ValueError as e:  # validazione pydantic
        msg = str(e)
        return {"error": msg.split("\n")[-1][:200] if msg else "Dati non validi"}


# ---------------------------------------------------------------------- conversazione


def clean_history(history: list | None) -> list[dict]:
    out = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content.strip()[:2000]})
    return out[-MAX_HISTORY:]


_TITLE = re.compile(r"^dott\.(ssa)?\s*")


def _name_matches(name: str, query: str) -> bool:
    """"Cusmai", "francesco cusmai", "Dott. Francesco Cusmai" trovano lo stesso neo-specialista."""
    q = _TITLE.sub("", (query or "").strip().lower())
    n = _TITLE.sub("", (name or "").strip().lower())
    return not q or q in n or any(part in n.split() for part in q.split() if part)


def _gift_options(db: Session, request: Request, graduate: str) -> dict:
    targets = db.scalars(select(GiftTarget).order_by(GiftTarget.sort_order.asc())).all()
    found = [t for t in targets if _name_matches(t.name, graduate)]
    if graduate and not found:
        return {"error": f"Non trovo un neo-specialista che si chiami {graduate}"}

    def methods(t: GiftTarget) -> dict:
        return {
            "name": t.name, "ibanHolder": t.iban_holder, "iban": t.iban,
            "paypal": t.paypal_me_url or (f"email {t.paypal_email} (in PayPal: Invia)" if t.paypal_email else ""),
            "satispay": bool(t.satispay_url), "bancomatPay": t.bancomat_pay,
        }
    collectors = [{"name": c.get("name"), "paymentMethods": c.get("paymentMethods", [])} for c in gift_collectors_info(request.app.state.event_data)]
    return {"graduates": [methods(t) for t in found], "collectors": collectors, "navigate": "gifts",
            "summary": "Metodi per il regalo diretto: " + "; ".join(
                f"{t.name}: IBAN" + (", PayPal" if (t.paypal_me_url or t.paypal_email) else "") + (", Satispay" if t.satispay_url else "")
                + (", BANCOMAT Pay" if t.bancomat_pay else "") for t in found)}


def _recent_wishes(db: Session, for_graduate: str, limit: int) -> dict:
    """Gli ultimi auguri (al massimo 5), filtrati in SQL se si chiede un neo-specialista: il destinatario
    è salvato per nome, quindi basta cercare il cognome (o l'ultima parola della richiesta) nel campo."""
    q = select(Wish)
    count_q = select(func.count()).select_from(Wish)
    key = _TITLE.sub("", (for_graduate or "").strip().lower()).split()
    if key:
        cond = func.lower(Wish.target_graduate).contains(key[-1])
        q, count_q = q.where(cond), count_q.where(cond)
    rows = db.scalars(q.order_by(Wish.created_at.desc(), Wish.id.desc()).limit(max(1, min(limit, 5)))).all()
    return {"count": int(db.scalar(count_q) or 0), "photos": photos_total(db), "navigate": "wishes",
            "wishes": [{"author": w.author_name, "to": w.target_graduate, "message": w.message[:280], "hearts": w.heart_count} for w in rows]}


def _graduate_summary(request: Request, db: Session, graduate_id: str) -> dict:
    from .routers.graduate import me as graduate_me

    d = graduate_me(request, graduate_id, db, {graduate_id})
    g = d["guests"]
    mine = [w for w in d["wishes"] if (w["targetGraduate"] or "").strip().lower() == d["graduate"]["shortName"].lower()]
    return {
        "name": d["graduate"]["shortName"], "confirmedGuests": g["confirmedGuests"], "covers": g["covers"],
        "pendingGuests": g["pendingGuests"], "wishesForMe": d["wishesForMe"], "wishesForAll": len(d["wishes"]) - d["wishesForMe"],
        "latestForMe": [{"author": w["authorName"], "message": w["message"][:200]} for w in mine[:3]],
        "busSeatsBooked": d["bus"]["bookedSeats"],
    }


def run_turn(
    client: MistralClient, avatar: AssistantAvatar, user_text: str, history: list[dict],
    request: Request, db: Session, client_id: str | None, guest: Guest | None = None,
) -> dict:
    """Un turno di conversazione: ritorna testo di risposta, azioni eseguite e sezione da aprire."""
    max_seats = bus_router.max_seats(request)
    caller = Caller.resolve(request, db, guest)
    graduate = db.get(GiftTarget, caller.graduate_id) if caller.graduate_id else None
    tools = tools_for(guest, caller.graduate_id, caller.can_see_memories)
    prompt = system_prompt(avatar, request.app.state.event_data, max_seats, guest=guest,
                           can_see_memories=caller.can_see_memories, graduate_name=graduate.short_name if graduate else None)
    messages: list[dict] = [{"role": "system", "content": prompt}]
    messages += history
    messages.append({"role": "user", "content": user_text})
    actions: list[dict] = []
    navigate: str | None = None
    reply = ""
    for _ in range(MAX_TOOL_ROUNDS + 1):
        msg = client.chat(messages, tools=tools)
        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content")
        if isinstance(content, list):  # contenuto a blocchi: teniamo il testo
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        if not tool_calls:
            reply = (content or "").strip()
            break
        messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {}
            result = execute_tool(name, args, request, db, client_id, guest, caller)
            if result.get("navigate"):
                navigate = result["navigate"]
            actions.append({"tool": name, "args": args, "ok": "error" not in result, "summary": result.get("summary") or result.get("error", "")})
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps(result, ensure_ascii=False)})
    else:
        reply = "Scusa, mi sono perso tra le azioni: puoi ripetere?"
    if not reply:
        reply = "Scusa, non ho capito bene: puoi ripetere?"
    new_history = (history + [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}])[-MAX_HISTORY:]
    return {"reply": reply, "actions": actions, "navigate": navigate, "history": new_history}


class VoiceResolver:
    """Sceglie la voce di un avatar: clonata, preimpostata scelta, oppure la voce di riserva
    (ASSISTANT_FALLBACK_VOICE_ID o la prima preimpostata di Mistral, letta una volta sola)."""

    def __init__(self, client: MistralClient, fallback_voice_id: str = ""):
        self.client = client
        self.fallback_voice_id = fallback_voice_id
        self._presets: list[dict] | None = None
        self._preset_error = ""

    def presets(self, refresh: bool = False) -> list[dict]:
        if self._presets is None or refresh:
            try:
                self._presets = self.client.list_preset_voices()
                self._preset_error = ""
            except MistralError as e:
                self._presets = []
                self._preset_error = str(e)
                log.warning("Voci preimpostate Mistral non disponibili: %s", e)
        return self._presets

    def fallback(self) -> str | None:
        if self.fallback_voice_id:
            return self.fallback_voice_id
        presets = self.presets()
        italian = [v for v in presets if "it" in [str(x).lower()[:2] for x in v.get("languages", [])]]
        pick = (italian or presets)[:1]
        return pick[0]["id"] if pick else None

    @property
    def fallback_available(self) -> bool:
        return self.fallback() is not None

    def voice_for(self, avatar: AssistantAvatar) -> str | None:
        return avatar.voice_id or avatar.preset_voice_id or self.fallback()
