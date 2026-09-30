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
    # Token del cassiere (header X-Treasurer-Token): vede e gestisce le quote uniche e ne scarica
    # il CSV, senza i poteri dell'organizzatore. Anche ADMIN_TOKEN è accettato su quegli endpoint.
    treasurer_token: str = field(default_factory=lambda: os.environ.get("TREASURER_TOKEN", ""))
    # Se true, al riavvio aggiorna anche testi/IBAN/link dei regali già in DB a partire dal JSON.
    # Di default aggiunge solo i regali mancanti.
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

    # ---- Automazioni (n8n) -----------------------------------------------------------
    # URL del nodo "Webhook" di n8n a cui inviare gli eventi (nuovo RSVP, prenotazione navetta, ...).
    # Vuoto = webhook disabilitati. Vedi app/webhooks.py e la sezione "Automazioni con n8n" del README.
    n8n_webhook_url: str = field(default_factory=lambda: os.environ.get("N8N_WEBHOOK_URL", "").strip())
    # Segreto condiviso inviato nell'header X-Automation-Secret (e usato per la firma HMAC).
    n8n_webhook_secret: str = field(default_factory=lambda: os.environ.get("N8N_WEBHOOK_SECRET", ""))
    # Eventi da inviare: "*" = tutti, oppure lista separata da virgole con jolly (es. "guest.*,bus.booked").
    n8n_webhook_events: str = field(default_factory=lambda: os.environ.get("N8N_WEBHOOK_EVENTS", "*"))
    # Timeout (secondi) di ogni chiamata verso n8n.
    n8n_webhook_timeout_s: float = field(default_factory=lambda: float(os.environ.get("N8N_WEBHOOK_TIMEOUT_S", "10")))
    # Token di sola lettura per n8n (header X-Automation-Token): report, riepiloghi ed export CSV,
    # senza dare a n8n il token organizzatore. Vuoto = si usa solo ADMIN_TOKEN.
    automation_token: str = field(default_factory=lambda: os.environ.get("AUTOMATION_TOKEN", ""))

    # ---- Assistente vocale (Mistral) ---------------------------------------------------
    # Chiave API Mistral: trascrizione (Voxtral), conversazione (chat con function calling) e
    # sintesi vocale con voci clonate. Vuota = assistente disattivato. Vedi app/mistral.py.
    mistral_api_key: str = field(default_factory=lambda: os.environ.get("MISTRAL_API_KEY", "").strip())
    mistral_base_url: str = field(default_factory=lambda: os.environ.get("MISTRAL_BASE_URL", "https://api.mistral.ai").rstrip("/"))
    mistral_chat_model: str = field(default_factory=lambda: os.environ.get("MISTRAL_CHAT_MODEL", "mistral-small-latest"))
    mistral_stt_model: str = field(default_factory=lambda: os.environ.get("MISTRAL_STT_MODEL", "voxtral-mini-latest"))
    mistral_tts_model: str = field(default_factory=lambda: os.environ.get("MISTRAL_TTS_MODEL", "voxtral-mini-tts-2603"))
    # Voce preimpostata di Mistral usata dagli avatar senza campione vocale ("voce di fantasia").
    # Vuota = la prima voce preimpostata restituita dall'API.
    assistant_fallback_voice_id: str = field(default_factory=lambda: os.environ.get("ASSISTANT_FALLBACK_VOICE_ID", "").strip())
    # true = simulatore locale al posto di Mistral (nessuna rete, nessun costo): per sviluppo e test e2e.
    assistant_fake: bool = field(default_factory=lambda: _bool(os.environ.get("ASSISTANT_FAKE"), False))

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "neuroparty.db")

    @property
    def upload_dir(self) -> str:
        return os.path.join(self.data_dir, "uploads")

    @property
    def vapid_key_path(self) -> str:
        return os.path.join(self.data_dir, "vapid_private.pem")

    @property
    def voices_dir(self) -> str:
        """Campioni vocali degli avatar caricati dagli organizzatori."""
        return os.path.join(self.data_dir, "voices")
