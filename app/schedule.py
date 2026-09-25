"""Orari dell'evento (blocco "schedule" di event-data.json) e segnaposto nei testi.

Nei testi del JSON si può scrivere:
  {partyTime}                       -> l'orario, oppure "da definire" se vuoto
  {partyTime|ora da definire}       -> l'orario, oppure il testo dopo la barra se vuoto
  {partyTime|Inizio ore $.|Orario da confermare.}
                                    -> se l'orario c'è: il secondo pezzo con "$" sostituito
                                       dall'orario; se manca: il terzo pezzo
Così quando l'orario viene deciso basta compilare "schedule" e tutte le frasi si aggiornano.
"""
import re
from datetime import date, datetime, time, timedelta

PLACEHOLDER = re.compile(r"\{(\w+)(?:\|([^|}]*))?(?:\|([^}]*))?\}")
DEFAULT_MISSING = "da definire"

SCHEDULE_KEYS = ("ceremonyDate", "ceremonyTime", "partyDate", "partyTime", "busDepartureTime", "busReturnTime")


def get_schedule(data: dict) -> dict:
    raw = data.get("schedule") or {}
    return {k: str(raw.get(k) or "").strip() for k in SCHEDULE_KEYS}


def resolve_text(text: str, schedule: dict) -> str:
    def sub(m: re.Match) -> str:
        key, a, b = m.group(1), m.group(2), m.group(3)
        value = schedule.get(key, "")
        if b is not None:  # forma a tre pezzi: {chiave|se presente ($)|se assente}
            return a.replace("$", value) if value else b
        if a is not None:  # forma a due pezzi: {chiave|se assente}
            return value or a
        return value or DEFAULT_MISSING

    return PLACEHOLDER.sub(sub, text)


def resolve_placeholders(obj, schedule: dict):
    """Applica resolve_text a tutte le stringhe di una struttura (dict/list) annidata."""
    if isinstance(obj, str):
        return resolve_text(obj, schedule)
    if isinstance(obj, list):
        return [resolve_placeholders(x, schedule) for x in obj]
    if isinstance(obj, dict):
        return {k: resolve_placeholders(v, schedule) for k, v in obj.items()}
    return obj


# ---- Calendario (.ics) ---------------------------------------------------------------

def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_time(s: str) -> time | None:
    for fmt in ("%H:%M", "%H.%M", "%H"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: righe al massimo di 75 byte, le successive iniziano con uno spazio."""
    out, chunk = [], ""
    for ch in line:
        if len((chunk + ch).encode("utf-8")) > 74:
            out.append(chunk)
            chunk = " " + ch
        else:
            chunk += ch
    out.append(chunk)
    return "\r\n".join(out)


VTIMEZONE_ROME = [
    "BEGIN:VTIMEZONE", "TZID:Europe/Rome",
    "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST",
    "DTSTART:19700329T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
    "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET",
    "DTSTART:19701025T030000", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD",
    "END:VTIMEZONE",
]


def _vevent(uid: str, summary: str, location: str, description: str, day: date, start: time | None, hours: int, url: str) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:" + datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")]
    if start is None:  # orario non ancora deciso: evento "tutto il giorno"
        lines.append("DTSTART;VALUE=DATE:" + day.strftime("%Y%m%d"))
        lines.append("DTEND;VALUE=DATE:" + (day + timedelta(days=1)).strftime("%Y%m%d"))
    else:
        begin = datetime.combine(day, start)
        lines.append("DTSTART;TZID=Europe/Rome:" + begin.strftime("%Y%m%dT%H%M%S"))
        lines.append("DTEND;TZID=Europe/Rome:" + (begin + timedelta(hours=hours)).strftime("%Y%m%dT%H%M%S"))
    lines.append("SUMMARY:" + _ics_escape(summary))
    if location:
        lines.append("LOCATION:" + _ics_escape(location))
    if description:
        lines.append("DESCRIPTION:" + _ics_escape(description))
    if url:
        lines.append("URL:" + url)
    lines.append("END:VEVENT")
    return lines


def build_ics(data: dict, public_url: str = "") -> str:
    """Due eventi: seduta (ceremonyDate/ceremonyTime) e festa (partyDate/partyTime).
    Senza orario l'evento è di tutto il giorno; ricaricando il file, il calendario aggiorna
    gli eventi grazie agli UID fissi."""
    sch = get_schedule(data)
    points = {p.get("id"): p for p in data.get("mapPoints", [])}
    title = (data.get("program") or {}).get("title") or "NeuroParty"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//NeuroParty//Backend//IT", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:" + _ics_escape(title)] + VTIMEZONE_ROME
    ceremony_day = _parse_date(sch["ceremonyDate"])
    party_day = _parse_date(sch["partyDate"])
    if ceremony_day:
        p = points.get("seduta", {})
        lines += _vevent("neuroparty-seduta@neurospec", "Seduta di Specializzazione in Neurologia",
                         p.get("address", ""), resolve_text(p.get("description", ""), sch),
                         ceremony_day, _parse_time(sch["ceremonyTime"]), 3, public_url)
    if party_day:
        p = points.get("festa", {})
        lines += _vevent("neuroparty-festa@neurospec", "Festa di Specializzazione in Neurologia",
                         p.get("address", ""), resolve_text(p.get("description", ""), sch),
                         party_day, _parse_time(sch["partyTime"]), 5, public_url)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
