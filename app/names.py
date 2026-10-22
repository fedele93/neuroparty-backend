"""Confronto di nomi scritti a mano (prenotazioni navetta senza profilo ↔ invitati).

"Màrio Rossi", "mario rossi" e "Rossi Mario" sono la stessa persona: si confrontano le parole del nome
senza accenti, maiuscole e titoli (Dott., Sig.ra...), in qualunque ordine."""
from __future__ import annotations

import re
import unicodedata

_TITLES = re.compile(r"^(dott\.?(ssa)?|prof\.?(ssa)?|sig\.?(ra)?|dr\.?|ing\.?)\s+", re.IGNORECASE)


def name_tokens(name: str) -> frozenset[str]:
    """Le parole del nome, normalizzate: senza accenti, minuscole, senza titoli e punteggiatura."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower().strip()
    s = _TITLES.sub("", s)
    return frozenset(t for t in re.split(r"[^a-z0-9]+", s) if t)


def same_person(a: str, b: str) -> bool:
    """Stesso nome a meno di ordine, accenti, maiuscole e titoli."""
    ta, tb = name_tokens(a), name_tokens(b)
    return bool(ta) and ta == tb


def name_similarity(a: str, b: str) -> int:
    """2 = stesso nome; 1 = almeno una parola di 3+ lettere in comune (es. il cognome); 0 = niente."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0
    if ta == tb:
        return 2
    return 1 if any(len(t) >= 3 for t in ta & tb) else 0
