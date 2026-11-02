"""Versione incrementale dei dati: i client la interrogano per sapere se ricaricare."""
from sqlalchemy.orm import Session

from .models import Meta


def get_version(db: Session) -> int:
    row = db.get(Meta, "version")
    return int(row.value) if row and row.value else 0


def bump_version(db: Session) -> int:
    row = db.get(Meta, "version")
    if row is None:
        row = Meta(key="version", value="0")
        db.add(row)
    row.value = str(int(row.value or 0) + 1)
    return int(row.value)


# ---- Impostazioni della festa modificabili dal cruscotto (tabella meta) ----
PHOTOS_DOWNLOAD_KEY = "photos_download"


def photos_download_enabled(db: Session) -> bool:
    """Gli invitati registrati possono scaricare tutte le foto in un unico zip? Lo accende l'organizzatore."""
    row = db.get(Meta, PHOTOS_DOWNLOAD_KEY)
    return bool(row and row.value == "1")


def set_photos_download(db: Session, enabled: bool) -> None:
    row = db.get(Meta, PHOTOS_DOWNLOAD_KEY)
    if row is None:
        db.add(Meta(key=PHOTOS_DOWNLOAD_KEY, value="1" if enabled else "0"))
    else:
        row.value = "1" if enabled else "0"
    bump_version(db)  # i telefoni ricaricano lo snapshot e mostrano/nascondono il pulsante
