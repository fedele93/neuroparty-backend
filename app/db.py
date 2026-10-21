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
    "shared_photos": {"thumb_path": "VARCHAR(300) NOT NULL DEFAULT ''"},
    "gift_pool_contributions": {
        "collector_id": "VARCHAR(64) NOT NULL DEFAULT ''", "collector_name": "VARCHAR(200) NOT NULL DEFAULT ''",
        "email": "VARCHAR(200) NOT NULL DEFAULT ''", "treasurer_note": "TEXT NOT NULL DEFAULT ''",
    },
    "gift_targets": {
        "email": "VARCHAR(200) NOT NULL DEFAULT ''", "access_token": "VARCHAR(64)",
        "bancomat_pay": "VARCHAR(30) NOT NULL DEFAULT ''", "paypal_email": "VARCHAR(200) NOT NULL DEFAULT ''",
    },
    "bus_bookings": {
        "guest_id": "INTEGER", "status": "VARCHAR(20) NOT NULL DEFAULT 'CONFIRMED'", "code": "VARCHAR(16) NOT NULL DEFAULT ''",
        "checked_in_at": "INTEGER",
    },
    "guests": {
        "email": "VARCHAR(200) NOT NULL DEFAULT ''", "phone": "VARCHAR(100) NOT NULL DEFAULT ''",
        "menu": "VARCHAR(30) NOT NULL DEFAULT ''", "companions_json": "TEXT NOT NULL DEFAULT '[]'",
        "access_token": "VARCHAR(64)", "token_created_at": "INTEGER", "consent_at": "INTEGER", "last_login_at": "INTEGER",
        "login_code_hash": "VARCHAR(64) NOT NULL DEFAULT ''", "login_code_expires_at": "INTEGER",
        "login_code_attempts": "INTEGER NOT NULL DEFAULT 0",
    },
}

# Indici per le query che girano a ogni poll (galleria dalla più recente, a cursore).
ADDED_INDEXES = {"ix_shared_photos_created_id": "shared_photos(created_at DESC, id DESC)"}

# Residui della prima versione dei regali (importi raccolti e quote per singolo destinatario),
# eliminati con il passaggio alla quota unica: gli utenti non devono vedere le cifre.
DROPPED_TABLES = ("gift_contributions",)
DROPPED_COLUMNS = {
    "gift_targets": ("target_amount", "collected_amount"),
}


def ensure_schema(engine) -> list[str]:
    """Crea le tabelle mancanti, aggiunge le colonne nuove e rimuove quelle dismesse.
    Ritorna l'elenco delle modifiche applicate."""
    Base.metadata.create_all(engine)
    changes = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in ADDED_COLUMNS.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    changes.append(f"+{table}.{name}")
        for name, ddl in ADDED_INDEXES.items():
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {ddl}"))
        for table in DROPPED_TABLES:
            if insp.has_table(table):
                conn.execute(text(f"DROP TABLE {table}"))
                changes.append(f"-{table}")
        for table, cols in DROPPED_COLUMNS.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for name in cols:
                if name in existing:
                    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))  # SQLite >= 3.35
                    changes.append(f"-{table}.{name}")
    return changes
