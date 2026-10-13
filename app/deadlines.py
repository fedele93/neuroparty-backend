"""Date limite (blocco "meta" di event-data.json): menuDeadline e busDeadline, formato AAAA-MM-GG.
Dopo la data limite gli invitati non modificano più menu/accompagnatori e navetta dal profilo
(gli organizzatori sì, con il token). Vuote = nessun limite."""
from datetime import date


def deadline(event_data: dict, key: str) -> str:
    return str((event_data.get("meta") or {}).get(key) or "").strip()


def deadline_passed(event_data: dict, key: str, today: date | None = None) -> bool:
    raw = deadline(event_data, key)
    if not raw:
        return False
    try:
        limit = date.fromisoformat(raw)
    except ValueError:
        return False
    return (today or date.today()) > limit


def deadlines_info(event_data: dict, today: date | None = None) -> dict:
    return {
        "menuDeadline": deadline(event_data, "menuDeadline"),
        "menuLocked": deadline_passed(event_data, "menuDeadline", today),
        "busDeadline": deadline(event_data, "busDeadline"),
        "busLocked": deadline_passed(event_data, "busDeadline", today),
    }
