"""Configurazione letta dalle variabili d'ambiente (vedi .env.example)."""
import os
from dataclasses import dataclass, field


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "si", "sì")


@dataclass
class Settings:
    # Cartella persistente (DB SQLite, foto caricate, chiavi VAPID). In Docker è il volume /data.
    data_dir: str = field(default_factory=lambda: os.environ.get("DATA_DIR", "./data"))
    # File JSON con i dati dell'evento (programma, laureandi, regali...) e seed demo.
    seed_file: str = field(default_factory=lambda: os.environ.get("SEED_FILE", "./seed/event-data.json"))
    # Se true, al primo avvio popola il DB anche con invitati/auguri/foto di esempio.
    seed_demo_data: bool = field(default_factory=lambda: _bool(os.environ.get("SEED_DEMO_DATA"), False))
    # Token segreto per le azioni da organizzatore (invio notifiche, cancellazioni).
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", ""))
    # Se true, al riavvio aggiorna anche testi/IBAN/obiettivo dei regali già in DB a partire dal JSON
    # (le quote raccolte non vengono mai toccate). Di default aggiunge solo i regali mancanti.
    gift_sync_update_texts: bool = field(default_factory=lambda: _bool(os.environ.get("GIFT_SYNC_UPDATE_TEXTS"), False))
    # Se impostata, l'API serve anche i file statici della PWA da questa cartella (utile in sviluppo).
    pwa_dir: str = field(default_factory=lambda: os.environ.get("PWA_DIR", ""))
    # URL pubblico (es. https://festa.tuodominio.it) usato per costruire i link alle foto.
    public_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_URL", "").rstrip("/"))
    # Contatto associato alle chiavi VAPID (richiesto dallo standard Web Push).
    vapid_subject: str = field(default_factory=lambda: os.environ.get("VAPID_SUBJECT", "mailto:admin@example.org"))
    # Origini autorizzate per CORS ("*" = tutte; utile se la PWA è su GitHub Pages).
    cors_origins: str = field(default_factory=lambda: os.environ.get("CORS_ORIGINS", "*"))
    # Ogni quanti secondi lo scheduler controlla le notifiche programmate da pubblicare.
    scheduler_interval_s: float = field(default_factory=lambda: float(os.environ.get("SCHEDULER_INTERVAL_S", "20")))
    max_upload_mb: int = field(default_factory=lambda: int(os.environ.get("MAX_UPLOAD_MB", "10")))
    max_bus_seats_override: int | None = field(
        default_factory=lambda: int(os.environ["MAX_BUS_SEATS"]) if os.environ.get("MAX_BUS_SEATS") else None
    )

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "neuroparty.db")

    @property
    def upload_dir(self) -> str:
        return os.path.join(self.data_dir, "uploads")

    @property
    def vapid_key_path(self) -> str:
        return os.path.join(self.data_dir, "vapid_private.pem")
