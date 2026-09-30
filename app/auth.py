"""Autorizzazione minimale: token organizzatore + identificativo del dispositivo.

- X-Admin-Token: abilita le azioni riservate agli organizzatori (notifiche push,
  cancellazione di qualsiasi record).
- X-Client-Id: stringa casuale generata da ogni app/dispositivo; chi crea un record
  (invitato, prenotazione bus) può poi modificarlo o cancellarlo.
- X-Automation-Token: token di sola lettura per le automazioni (n8n): report, riepiloghi,
  export CSV. Anche X-Admin-Token è accettato su questi endpoint.
- X-Treasurer-Token: token del cassiere delle quote uniche (TREASURER_TOKEN): elenco e stato
  delle quote, CSV. Anche X-Admin-Token è accettato su questi endpoint.
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


def is_treasurer(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_treasurer_token: str | None = Header(default=None),
) -> bool:
    settings = request.app.state.settings
    return _matches(x_admin_token, settings.admin_token) or _matches(x_treasurer_token, settings.treasurer_token)


def require_treasurer(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_treasurer_token: str | None = Header(default=None),
) -> None:
    """Endpoint del cassiere delle quote uniche: TREASURER_TOKEN oppure ADMIN_TOKEN."""
    settings = request.app.state.settings
    if not settings.admin_token and not settings.treasurer_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN/TREASURER_TOKEN non configurati sul server")
    if not is_treasurer(request, x_admin_token, x_treasurer_token):
        raise HTTPException(status_code=403, detail="Token cassiere o organizzatore non valido")


def require_treasurer_or_automation(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_treasurer_token: str | None = Header(default=None),
    x_automation_token: str | None = Header(default=None),
) -> None:
    """Sola lettura (CSV delle quote uniche): cassiere, organizzatore o automazioni."""
    settings = request.app.state.settings
    if is_treasurer(request, x_admin_token, x_treasurer_token):
        return
    if _matches(x_automation_token, settings.automation_token):
        return
    if not (settings.admin_token or settings.treasurer_token or settings.automation_token):
        raise HTTPException(status_code=503, detail="Nessun token configurato sul server")
    raise HTTPException(status_code=403, detail="Token cassiere, organizzatore o automazioni non valido")


def can_modify(owner_client_id: str | None, admin: bool, client_id: str | None) -> bool:
    if admin:
        return True
    return bool(owner_client_id) and owner_client_id == client_id
