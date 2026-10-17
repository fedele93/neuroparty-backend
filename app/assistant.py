"""Motore dell'assistente vocale: prompt con i dati della festa, strumenti che agiscono
sull'app (RSVP, navetta, auguri, navigazione) e ciclo trascrizione -> conversazione -> voce.

Le azioni che scrivono dati riusano le stesse funzioni dei router (validazione, controllo dei
posti, webhook n8n), così l'assistente non può fare nulla che l'app non permetta già.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import lists_allowed, memories_access
from .mistral import MistralClient, MistralError
from .models import AssistantAvatar, Guest
from .routers import bus as bus_router
from .routers import guests as guests_router
from .routers import profile as profile_router
from .routers import wishes as wishes_router
from .schemas import BookingIn, CompanionIn, GuestIn, GuestUpdate, ProfileBusIn, ProfileIn, WishIn
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
                    "category": {"type": "string", "description": "Es. Famigliari, Colleghi Reparto, Amici Università"},
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
            "description": "Prenota posti sulla navetta per la festa. Chiamala SOLO dopo la conferma esplicita dell'utente su nome, "
                           "numero di posti e fermata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "passengerName": {"type": "string"},
                    "seatsCount": {"type": "integer", "minimum": 1, "maximum": 54},
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
                    "seatsCount": {"type": "integer", "minimum": 1, "maximum": 54, "description": "Se omesso: le persone del profilo"},
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

WRITE_TOOLS = {"set_rsvp", "book_bus", "post_wish", "update_my_profile", "book_my_bus", "cancel_my_bus"}


def tools_for(guest: Guest | None) -> list[dict]:
    return TOOLS + (PROFILE_TOOLS if guest is not None else [])


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


def system_prompt(avatar: AssistantAvatar, event_data: dict, max_seats: int, now: datetime | None = None, guest: Guest | None = None) -> str:
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
        "un RSVP, prenotare la navetta, pubblicare un augurio, aprire una sezione. Prima di ogni azione che scrive dati "
        "(set_rsvp, book_bus, post_wish) riassumi i dati e chiedi conferma; chiama lo strumento solo dopo un sì esplicito. "
        "Dopo l'azione conferma in una frase cosa hai fatto. Se manca un dato indispensabile (es. il nome), chiedilo.\n"
        f"Oggi è {_fmt_date(now.date().isoformat())}.\n"
        + profile_facts(guest) + "\n\n"
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


def execute_tool(name: str, args: dict, request: Request, db: Session, client_id: str | None, guest: Guest | None = None) -> dict:
    """Esegue uno strumento e ritorna un dizionario serializzabile per il modello."""
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
            if not lists_allowed(request):  # l'elenco è riservato: un invitato ritrova solo sé stesso
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
            body = BookingIn(
                passengerName=(args.get("passengerName") or "").strip(), seatsCount=int(args.get("seatsCount") or 1),
                pickupStop=args.get("pickupStop") or "", returnTripWanted=bool(args.get("returnTripWanted", True)),
                contactPhone=args.get("contactPhone") or "",
            )
            out = bus_router.create_booking(body, request, db, client_id)
            summary = bus_router.bus_summary(request, db)
            return {"summary": f"Prenotati {out['seatsCount']} posti per {out['passengerName']}"
                    + (f" da {out['pickupStop']}" if out["pickupStop"] else "") + f". Restano {summary['availableSeats']} posti.",
                    "booking": out, "bus": summary}
        if name == "post_wish":
            allowed, member = memories_access(request, db)
            if not allowed:
                return {"error": "Gli auguri li pubblica solo chi è registrato: l'utente deve aprire il link personale ricevuto via email (pulsante con l'omino in alto)", "navigate": "rsvp"}
            body = WishIn(authorName=args.get("authorName") or "Amico/a", targetGraduate=args.get("targetGraduate") or "Tutti i Laureandi",
                          message=(args.get("message") or "").strip())
            out = wishes_router.create_wish(body, request, db, member)
            return {"summary": f"Augurio di {out['authorName']} per {out['targetGraduate']} pubblicato in bacheca.", "wish": out, "navigate": "wishes"}
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


def run_turn(
    client: MistralClient, avatar: AssistantAvatar, user_text: str, history: list[dict],
    request: Request, db: Session, client_id: str | None, guest: Guest | None = None,
) -> dict:
    """Un turno di conversazione: ritorna testo di risposta, azioni eseguite e sezione da aprire."""
    max_seats = bus_router.max_seats(request)
    tools = tools_for(guest)
    messages: list[dict] = [{"role": "system", "content": system_prompt(avatar, request.app.state.event_data, max_seats, guest=guest)}]
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
            result = execute_tool(name, args, request, db, client_id, guest)
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
