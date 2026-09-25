"""Punto d'ingresso dell'API. Avvio locale:  uvicorn app.main:app --reload"""
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .db import Base, make_engine, make_session_factory
from .push import PushService
from .routers import bus, event, gifts, guests, notifications, photos, push, wishes
from .seed import load_event_file, seed_database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("neuroparty")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    os.makedirs(settings.data_dir, exist_ok=True)
    os.makedirs(settings.upload_dir, exist_ok=True)

    app = FastAPI(
        title="NeuroParty API",
        version="1.0.0",
        description="Backend condiviso per l'app Android e la PWA della festa di specializzazione in Neurologia.",
    )
    app.state.settings = settings

    engine = make_engine(settings.db_path)
    Base.metadata.create_all(engine)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    app.state.event_data = load_event_file(settings.seed_file)
    with app.state.session_factory() as db:
        seed_database(
            db, app.state.event_data,
            include_demo=settings.seed_demo_data,
            update_gift_texts=settings.gift_sync_update_texts,
        )

    app.state.push = PushService(settings.vapid_key_path, settings.vapid_subject)

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()] or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for r in (event, guests, bus, wishes, photos, gifts, notifications, push):
        app.include_router(r.router)

    # Foto caricate dagli invitati
    app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

    # In sviluppo (o senza Caddy) l'API può servire direttamente la PWA.
    if settings.pwa_dir and os.path.isdir(settings.pwa_dir):
        app.mount("/", StaticFiles(directory=settings.pwa_dir, html=True), name="pwa")
        log.info("PWA servita da %s", settings.pwa_dir)

    if not settings.admin_token:
        log.warning("ADMIN_TOKEN non impostato: invio notifiche e cancellazioni da organizzatore disabilitati")
    return app


app = create_app()
