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
