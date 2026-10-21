"""Codice di accesso a 6 cifre spedito insieme ai link personali (invitati, festeggiati, cassieri).

Serve a chi ha aggiunto l'app alla schermata Home dell'iPhone: lì il link dell'email si apre in Safari,
che non condivide la memoria con l'app installata, quindi il token non arriverebbe mai dentro l'app.
Il codice si digita nell'app e il server lo scambia con lo stesso token del link.

Chi lo usa ha tre colonne (login_code_hash, login_code_expires_at, login_code_attempts): nel database
resta solo l'hash; il codice vale LOGIN_CODE_MINUTES minuti, una volta sola, per pochi tentativi."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from .models import now_ms

LOGIN_CODE_MINUTES = 15
LOGIN_CODE_MAX_ATTEMPTS = 5


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def issue_login_code(row) -> str:
    """Nuovo codice per la riga (invitato, festeggiato o accesso del cassiere): azzera scadenza e tentativi."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    row.login_code_hash = _code_hash(code)
    row.login_code_expires_at = now_ms() + LOGIN_CODE_MINUTES * 60_000
    row.login_code_attempts = 0
    return code


def clear_login_code(row) -> None:
    row.login_code_hash = ""
    row.login_code_expires_at = None
    row.login_code_attempts = 0


def check_login_code(row, code: str) -> bool:
    """True se il codice è quello della riga, ancora valido: in quel caso lo consuma. Altrimenti conta il
    tentativo (e dopo LOGIN_CODE_MAX_ATTEMPTS lo cancella) o lo cancella se scaduto. Non fa commit."""
    if row is None or not row.login_code_hash or len(code) != 6:
        return False
    if (row.login_code_expires_at or 0) < now_ms():
        clear_login_code(row)
        return False
    if not hmac.compare_digest(row.login_code_hash, _code_hash(code)):
        row.login_code_attempts += 1
        if row.login_code_attempts >= LOGIN_CODE_MAX_ATTEMPTS:
            clear_login_code(row)
        return False
    clear_login_code(row)
    return True
