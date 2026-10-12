"""Autorizzazione minimale: token organizzatore + identificativo del dispositivo.

- X-Admin-Token: abilita le azioni riservate agli organizzatori (notifiche push,
  cancellazione di qualsiasi record).
- X-Client-Id: stringa casuale generata da ogni app/dispositivo; chi crea un record
  (invitato, prenotazione bus) può poi modificarlo o cancellarlo.
- X-Automation-Token: token di sola lettura per le automazioni (n8n): report, riepiloghi,
  export CSV. Anche X-Admin-Token è accettato su questi endpoint.
- X-Treasurer-Token: token dei cassieri delle quote uniche (TREASURER_TOKEN per tutti, oppure
  TREASURER_TOKEN_<ID> per un solo cassiere): elenco e stato delle quote, CSV. Anche
  X-Admin-Token è accettato su questi endpoint.
"""
import secrets

from fastapi import Header, HTTPException, Request


def get_client_id(x_client_id: str | None = Header(default=None)) -> str | None:
    if x_client_id and 1 <= len(x_client_id) <= 64:
        return x_client_id
    return None


def is_admin(request: Request, x_admin_token: str | None = Header(default=None)) -> bool:
    expected = request.app.state.settings.admin_token
    if not expected or not x_admin_token:
        return False
    return secrets.compare_digest(x_admin_token, expected)


def require_admin(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.admin_token
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN non configurato sul server")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="Token organizzatore non valido")


def require_automation(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_automation_token: str | None = Header(default=None),
) -> None:
    """Endpoint di sola lettura per le automazioni: basta AUTOMATION_TOKEN oppure ADMIN_TOKEN."""
    settings = request.app.state.settings
    if not settings.admin_token and not settings.automation_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN/AUTOMATION_TOKEN non configurati sul server")
    if x_admin_token and settings.admin_token and secrets.compare_digest(x_admin_token, settings.admin_token):
        return
    if (
        x_automation_token
        and settings.automation_token
        and secrets.compare_digest(x_automation_token, settings.automation_token)
    ):
        return
    raise HTTPException(status_code=403, detail="Token automazioni o organizzatore non valido")


def _matches(given: str | None, expected: str) -> bool:
    return bool(given) and bool(expected) and secrets.compare_digest(given, expected)


def treasurer_scope(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_treasurer_token: str | None = Header(default=None),
) -> set[str] | None | bool:
    """Chi è il chiamante per le quote uniche:
    - None  = organizzatore o TREASURER_TOKEN: vede tutti i cassieri;
    - set   = TREASURER_TOKEN_<ID>: vede solo quei cassieri;
    - False = nessun token valido."""
    settings = request.app.state.settings
    if _matches(x_admin_token, settings.admin_token) or _matches(x_treasurer_token, settings.treasurer_token):
        return None
    for collector_id, token in settings.treasurer_tokens.items():
        if _matches(x_treasurer_token, token):
            return {collector_id}
    return False


def is_treasurer(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_treasurer_token: str | None = Header(default=None),
) -> bool:
    return treasurer_scope(request, x_admin_token, x_treasurer_token) is not False


def _any_treasurer_token(settings) -> bool:
    return bool(settings.admin_token or settings.treasurer_token or settings.treasurer_tokens)


def require_treasurer(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_treasurer_token: str | None = Header(default=None),
) -> set[str] | None:
    """Endpoint del cassiere delle quote uniche: TREASURER_TOKEN, TREASURER_TOKEN_<ID> oppure ADMIN_TOKEN.
    Ritorna i cassieri visibili (None = tutti)."""
    settings = request.app.state.settings
    if not _any_treasurer_token(settings):
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN/TREASURER_TOKEN non configurati sul server")
    scope = treasurer_scope(request, x_admin_token, x_treasurer_token)
    if scope is False:
        raise HTTPException(status_code=403, detail="Token cassiere o organizzatore non valido")
    return scope


def require_treasurer_or_automation(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_treasurer_token: str | None = Header(default=None),
    x_automation_token: str | None = Header(default=None),
) -> set[str] | None:
    """Sola lettura (CSV delle quote uniche): cassiere, organizzatore o automazioni. Ritorna i cassieri visibili."""
    settings = request.app.state.settings
    scope = treasurer_scope(request, x_admin_token, x_treasurer_token)
    if scope is not False:
        return scope
    if _matches(x_automation_token, settings.automation_token):
        return None
    if not (_any_treasurer_token(settings) or settings.automation_token):
        raise HTTPException(status_code=503, detail="Nessun token configurato sul server")
    raise HTTPException(status_code=403, detail="Token cassiere, organizzatore o automazioni non valido")


def can_modify(owner_client_id: str | None, admin: bool, client_id: str | None) -> bool:
    if admin:
        return True
    return bool(owner_client_id) and owner_client_id == client_id
