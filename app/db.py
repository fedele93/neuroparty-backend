"""Connessione SQLite tramite SQLAlchemy."""
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(db_path: str):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


# Colonne aggiunte dopo la prima release: create_all non modifica le tabelle esistenti,
# quindi su un database già in uso le aggiungiamo con ALTER TABLE (SQLite lo supporta).
ADDED_COLUMNS = {
    "event_notifications": {"scheduled_at": "INTEGER"},
}


def ensure_schema(engine) -> list[str]:
    """Crea le tabelle mancanti e aggiunge le colonne nuove. Ritorna le colonne aggiunte."""
    Base.metadata.create_all(engine)
    added = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in ADDED_COLUMNS.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    added.append(f"{table}.{name}")
    return added
