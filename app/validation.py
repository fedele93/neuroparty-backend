"""Controlli sui dati dell'evento prima di andare online.

Gli IBAN del file di esempio sono segnaposto e non superano il controllo di validità
(resto della divisione per 97, ISO 7064): così ci accorgiamo se il server parte in
produzione con IBAN finti. La stessa verifica è in spec2026app/tools/check-event-data.py.
"""
import re

IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


def iban_is_valid(iban: str) -> bool:
    s = re.sub(r"\s+", "", iban or "").upper()
    if not IBAN_RE.match(s):
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def placeholder_gift_issues(data: dict) -> list[str]:
    """Elenco (vuoto se tutto ok) dei regali con IBAN non valido o mancante."""
    issues = []
    for t in data.get("giftTargets", []):
        iban = (t.get("iban") or "").strip()
        if not iban:
            issues.append(f"regalo '{t.get('id')}': IBAN mancante")
        elif not iban_is_valid(iban):
            issues.append(f"regalo '{t.get('id')}': IBAN non valido (segnaposto?) {iban}")
    return issues
