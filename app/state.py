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


# ---- Impostazioni della festa modificabili dal cruscotto (tabella meta), spente di default ----
PHOTOS_DOWNLOAD_KEY = "photos_download"  # i registrati scaricano tutte le foto in uno zip
PAIR_GAME_KEY = "pair_game"  # gioco delle coppie: card con le regole, foto etichettate "coppie", filtro in galleria


def setting_enabled(db: Session, key: str) -> bool:
    row = db.get(Meta, key)
    return bool(row and row.value == "1")


def set_setting(db: Session, key: str, enabled: bool) -> None:
    row = db.get(Meta, key)
    if row is None:
        db.add(Meta(key=key, value="1" if enabled else "0"))
    else:
        row.value = "1" if enabled else "0"
    bump_version(db)  # i telefoni ricaricano lo snapshot e mostrano/nascondono le funzioni


def photos_download_enabled(db: Session) -> bool:
    """Gli invitati registrati possono scaricare tutte le foto in un unico zip? Lo accende l'organizzatore."""
    return setting_enabled(db, PHOTOS_DOWNLOAD_KEY)


def set_photos_download(db: Session, enabled: bool) -> None:
    set_setting(db, PHOTOS_DOWNLOAD_KEY, enabled)


def pair_game_enabled(db: Session) -> bool:
    """Gioco delle coppie acceso: la PWA mostra la card con le regole e il filtro delle foto di coppia."""
    return setting_enabled(db, PAIR_GAME_KEY)
