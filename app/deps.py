"""Dipendenze FastAPI condivise dai router."""
from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session


def get_db(request: Request) -> Generator[Session, None, None]:
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def base_url_for(request: Request) -> str:
    """URL assoluto da anteporre ai percorsi delle foto (PUBLIC_URL se configurato)."""
    settings = request.app.state.settings
    if settings.public_url:
        return settings.public_url
    # dietro Caddy arrivano gli header X-Forwarded-*: ricostruiamo l'origine reale
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"
