"""Autorizzazione minimale: token organizzatore + identificativo del dispositivo.

- X-Admin-Token: abilita le azioni riservate agli organizzatori (notifiche push,
  cancellazione di qualsiasi record).
- X-Client-Id: stringa casuale generata da ogni app/dispositivo; chi crea un record
  (invitato, prenotazione bus) può poi modificarlo o cancellarlo.
- X-Automation-Token: token di sola lettura per le automazioni (n8n): report, riepiloghi,
  export CSV. Anche X-Admin-Token è accettato su questi endpoint.
- X-Graduate-Token: token di un festeggiato (GRADUATE_TOKEN_<ID> nel .env, oppure quello del link
  personale mandato alla sua email, #festeggiato=<token>): area festeggiato e il proprio avatar
  dell'assistente. Anche X-Admin-Token è accettato.
- X-Treasurer-Token: token dei cassieri delle quote uniche (TREASURER_TOKEN per tutti, oppure
  TREASURER_TOKEN_<ID> per un solo cassiere): elenco e stato delle quote, CSV. Le cifre delle
  quote le vede SOLO chi ha questo token: il token organizzatore non basta.
- Elenchi di invitati e prenotazioni navetta: completi solo con un token (organizzatore,
  festeggiato o cassiere); gli altri vedono soltanto i propri record (X-Client-Id o X-Guest-Token).
- Foto e auguri: solo per chi è registrato (X-Guest-Token valido, cioè ha aperto il link personale
  ricevuto via email, qualunque sia la sua risposta) o ha un token organizzatore, festeggiato o cassiere.
"""
import secrets

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .deps import get_db
from .models import GiftTarget, Guest


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
    - None  = TREASURER_TOKEN: vede tutti i cassieri;
    - set   = TREASURER_TOKEN_<ID>: vede solo quei cassieri;
    - False = nessun token cassiere valido (il token organizzatore NON vale: le cifre restano ai cassieri)."""
    settings = request.app.state.settings
    if _matches(x_treasurer_token, settings.treasurer_token):
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
    return bool(settings.treasurer_token or settings.treasurer_tokens)


def require_treasurer(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_treasurer_token: str | None = Header(default=None),
) -> set[str] | None:
    """Endpoint del cassiere delle quote uniche: TREASURER_TOKEN oppure TREASURER_TOKEN_<ID>.
    Ritorna i cassieri visibili (None = tutti)."""
    settings = request.app.state.settings
    if not _any_treasurer_token(settings):
        raise HTTPException(status_code=503, detail="TREASURER_TOKEN non configurato sul server")
    scope = treasurer_scope(request, x_admin_token, x_treasurer_token)
    if scope is False:
        raise HTTPException(status_code=403, detail="Token cassiere non valido")
    return scope


def require_treasurer_or_automation(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_treasurer_token: str | None = Header(default=None),
    x_automation_token: str | None = Header(default=None),
) -> set[str] | None:
    """Sola lettura (CSV delle quote uniche): cassiere o automazioni (n8n). Ritorna i cassieri visibili."""
    settings = request.app.state.settings
    scope = treasurer_scope(request, x_admin_token, x_treasurer_token)
    if scope is not False:
        return scope
    if _matches(x_automation_token, settings.automation_token):
        return None
    if not (_any_treasurer_token(settings) or settings.automation_token):
        raise HTTPException(status_code=503, detail="Nessun token configurato sul server")
    raise HTTPException(status_code=403, detail="Token cassiere o automazioni non valido")


def graduate_for_token(request: Request, token: str | None, db: Session | None = None) -> str | None:
    """Id del festeggiato a cui appartiene il token: GRADUATE_TOKEN_<ID> del .env oppure il token del
    link personale ricevuto via email (salvato nel database). None se non vale.
    Il risultato è memorizzato sulla richiesta: chi lo chiede più volte (ruoli, elenchi, foto e auguri,
    assistente) non rifà la query."""
    token = (token or "").strip()
    if not token:
        return None
    cache = request.state.__dict__.setdefault("_graduate_by_token", {})
    if token in cache:
        return cache[token]
    graduate_id = None
    for gid, expected in request.app.state.settings.graduate_tokens.items():
        if _matches(token, expected):
            graduate_id = gid
            break
    if graduate_id is None and len(token) >= 20:  # i token dei link sono lunghi: niente query per valori palesemente sbagliati
        if db is not None:
            graduate_id = db.scalar(select(GiftTarget.id).where(GiftTarget.access_token == token))
        else:
            with request.app.state.session_factory() as own:
                graduate_id = own.scalar(select(GiftTarget.id).where(GiftTarget.access_token == token))
    cache[token] = graduate_id
    return graduate_id


def graduate_scope(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_graduate_token: str | None = Header(default=None),
) -> set[str] | None | bool:
    """None = organizzatore (tutti i festeggiati); set = GRADUATE_TOKEN_<ID>; False = nessun token valido.
    Il token festeggiato, se inviato, prevale su quello organizzatore (chi li ha entrambi vede la propria area)
    e se è sbagliato non vale nemmeno l'organizzatore: così la PWA può scartarlo subito."""
    settings = request.app.state.settings
    if x_graduate_token:
        graduate_id = graduate_for_token(request, x_graduate_token)
        return {graduate_id} if graduate_id else False
    if _matches(x_admin_token, settings.admin_token):
        return None
    return False


def require_graduate(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_graduate_token: str | None = Header(default=None),
) -> set[str] | None:
    """Area festeggiato: GRADUATE_TOKEN_<ID>, link personale del festeggiato oppure ADMIN_TOKEN.
    Ritorna i festeggiati visibili (None = tutti)."""
    settings = request.app.state.settings
    scope = graduate_scope(request, x_admin_token, x_graduate_token)
    if scope is False:
        if not settings.admin_token and not settings.graduate_tokens and not _any_graduate_email(request):
            raise HTTPException(status_code=503, detail="ADMIN_TOKEN/GRADUATE_TOKEN non configurati sul server")
        raise HTTPException(status_code=403, detail="Token festeggiato o organizzatore non valido")
    return scope


def _any_graduate_email(request: Request) -> bool:
    """C'è almeno un festeggiato con l'email privata (quindi può accedere col link via email)?"""
    with request.app.state.session_factory() as db:
        return db.scalar(select(GiftTarget.id).where(GiftTarget.email != "").limit(1)) is not None


def can_modify(owner_client_id: str | None, admin: bool, client_id: str | None) -> bool:
    if admin:
        return True
    return bool(owner_client_id) and owner_client_id == client_id


# ---------------------------------------------------------------- elenchi riservati


def lists_allowed(request: Request, db: Session | None = None) -> bool:
    """True se il chiamante può vedere gli elenchi completi di invitati e prenotazioni navetta:
    organizzatore (ADMIN_TOKEN), festeggiato (GRADUATE_TOKEN_<ID>) o cassiere (TREASURER_TOKEN[_<ID>])."""
    settings = request.app.state.settings
    h = request.headers
    if _matches(h.get("x-admin-token"), settings.admin_token):
        return True
    if treasurer_scope(request, None, h.get("x-treasurer-token")) is not False:
        return True
    return graduate_for_token(request, h.get("x-graduate-token"), db) is not None


def can_see_lists(request: Request) -> bool:
    return lists_allowed(request)


def require_lists(request: Request) -> None:
    if not lists_allowed(request):
        raise HTTPException(status_code=403, detail="Elenco riservato a organizzatori, festeggiati e cassieri")


def roles_for(request: Request) -> dict:
    """Quali ruoli attivano i token inviati in questa chiamata: la PWA lo usa per mostrare la guida
    del ruolo appena sbloccato e per segnalare subito un token sbagliato.
    - admin: ADMIN_TOKEN valido;
    - treasurer / treasurerIds: cassiere valido; la lista degli id è vuota con TREASURER_TOKEN (tutti);
    - graduate: id del festeggiato (GRADUATE_TOKEN_<ID>) oppure None."""
    settings = request.app.state.settings
    h = request.headers
    scope = treasurer_scope(request, None, h.get("x-treasurer-token"))
    graduate = graduate_for_token(request, h.get("x-graduate-token"))
    return {
        "admin": _matches(h.get("x-admin-token"), settings.admin_token),
        "treasurer": scope is not False,
        "treasurerIds": sorted(scope) if isinstance(scope, set) else [],
        "graduate": graduate,
    }


# ---------------------------------------------------------------- foto e auguri (solo registrati)

MEMBERS_ONLY = "Foto e auguri sono riservati agli invitati registrati: apri il link personale che ti abbiamo mandato via email"


def memories_access(request: Request, db: Session) -> tuple[bool, Guest | None]:
    """Chi può vedere e pubblicare foto e auguri: (accesso, invitato).
    - invitato registrato: X-Guest-Token valido (ha aperto il link personale; vale anche chi ha detto "non vengo");
    - organizzatore, festeggiato o cassiere: token del ruolo (gli stessi degli elenchi riservati)."""
    token = (request.headers.get("x-guest-token") or "").strip()
    guest = db.scalars(select(Guest).where(Guest.access_token == token)).first() if token else None
    if guest is not None:
        return True, guest
    return lists_allowed(request, db), None


def require_member(request: Request, db: Session = Depends(get_db)) -> Guest | None:
    """Dipendenza degli endpoint di foto e auguri: 401 a chi non è registrato. Ritorna l'invitato (None per i ruoli)."""
    allowed, guest = memories_access(request, db)
    if not allowed:
        raise HTTPException(status_code=401, detail=MEMBERS_ONLY)
    return guest
