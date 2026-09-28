"""Webhook in uscita verso n8n (o qualsiasi altro ricevitore HTTP).

Ogni volta che succede qualcosa di rilevante (nuovo RSVP, prenotazione della navetta, augurio,
foto, quota regalo, notifica pubblicata) il backend invia un POST JSON all'URL configurato in
N8N_WEBHOOK_URL. In n8n basta un nodo "Webhook" per ricevere l'evento e costruire l'automazione
che si preferisce (mail, Telegram, foglio Google, ...).

Formato del corpo inviato:

    {
      "event": "guest.created",          # vedi EVENTS
      "timestamp": 1760000000000,        # ms
      "source": "neuroparty",
      "data": { ... }                    # il record coinvolto + un po' di contesto
    }

Header inviati:
    X-NeuroParty-Event:      nome dell'evento
    X-Automation-Secret:     N8N_WEBHOOK_SECRET (se configurato) -> in n8n si controlla con "Header Auth"
    X-NeuroParty-Signature:  sha256=<HMAC del corpo con lo stesso segreto> (per chi vuole verificare la firma)

La consegna avviene in un thread separato: una richiesta dell'app non resta mai in attesa di n8n
e un n8n spento non causa errori agli invitati. In caso di errore di rete o risposta 5xx si
riprova un paio di volte, poi si rinuncia scrivendo un avviso nel log.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from .models import now_ms

log = logging.getLogger("neuroparty.webhooks")
# httpx registra ogni chiamata a livello INFO: basta il nostro riepilogo per webhook
logging.getLogger("httpx").setLevel(logging.WARNING)

# Catalogo degli eventi: nome -> descrizione (esposto anche da GET /api/automation/status).
EVENTS: dict[str, str] = {
    "guest.created": "Nuovo invitato / RSVP inserito (data.guest, data.summary)",
    "guest.updated": "Invitato modificato, es. cambio stato RSVP (data.guest, data.changes, data.summary)",
    "guest.deleted": "Invitato rimosso (data.guest, data.summary)",
    "bus.booked": "Nuova prenotazione della navetta (data.booking, data.bus)",
    "bus.cancelled": "Prenotazione navetta cancellata (data.booking, data.bus)",
    "wish.created": "Nuovo augurio in bacheca (data.wish)",
    "photo.uploaded": "Nuova foto in galleria (data.photo, con imageUri assoluto)",
    "gift.contributed": "Nuova quota per un regalo (data.contribution, data.target)",
    "notification.scheduled": "Notifica programmata dagli organizzatori (data.notification)",
    "notification.published": "Notifica pubblicata e inviata in push (data.notification, data.scheduled)",
    "test": "Evento di prova inviato da POST /api/automation/webhook-test",
}

USER_AGENT = "NeuroParty-Webhook/1.0"


def parse_event_filter(spec: str | None) -> list[str]:
    """"*" o vuoto = tutti; altrimenti lista separata da virgole, con jolly stile shell (guest.*)."""
    items = [s.strip() for s in (spec or "").split(",") if s.strip()]
    return items or ["*"]


class WebhookDispatcher:
    def __init__(
        self,
        url: str = "",
        secret: str = "",
        events: str = "*",
        timeout_s: float = 10.0,
        *,
        client: httpx.Client | None = None,
        sync: bool = False,
        max_attempts: int = 3,
        retry_delay_s: float = 2.0,
    ):
        self.url = (url or "").strip()
        self.secret = secret or ""
        self.patterns = parse_event_filter(events)
        self.timeout_s = timeout_s
        self.client = client            # in test si inietta un httpx.Client con MockTransport
        self.sync = sync                # True = consegna nel thread chiamante (test / endpoint di prova)
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_s = retry_delay_s
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="webhook")
        self._lock = threading.Lock()
        self.stats = {"sent": 0, "failed": 0, "skipped": 0}
        self.last_delivery: dict | None = None

    # ---- stato -------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def accepts(self, event: str) -> bool:
        return self.enabled and any(fnmatch.fnmatchcase(event, p) for p in self.patterns)

    def status(self) -> dict:
        """Riepilogo (senza segreti) per GET /api/automation/status."""
        return {
            "enabled": self.enabled,
            "url": _mask_url(self.url),
            "secretConfigured": bool(self.secret),
            "events": self.patterns,
            "stats": dict(self.stats),
            "lastDelivery": self.last_delivery,
        }

    # ---- invio -------------------------------------------------------------------------

    def emit(self, event: str, data: dict, *, sync: bool | None = None) -> dict | None:
        """Accoda (o consegna subito se sync) l'evento. Ritorna il risultato solo in modalità sync."""
        if not self.accepts(event):
            with self._lock:
                self.stats["skipped"] += 1
            return None
        payload = {"event": event, "timestamp": now_ms(), "source": "neuroparty", "data": data}
        if sync if sync is not None else self.sync:
            return self.deliver(payload)
        self._executor.submit(self._deliver_logged, payload)
        return None

    def _deliver_logged(self, payload: dict) -> None:
        try:
            self.deliver(payload)
        except Exception:  # noqa: BLE001 - il thread non deve mai morire in silenzio
            log.exception("Errore inatteso nell'invio del webhook %s", payload.get("event"))

    def deliver(self, payload: dict) -> dict:
        """Invia il payload con tentativi ripetuti. Ritorna {delivered, status, attempts, error}."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
            "X-NeuroParty-Event": payload["event"],
        }
        if self.secret:
            headers["X-Automation-Secret"] = self.secret
            headers["X-NeuroParty-Signature"] = "sha256=" + hmac.new(
                self.secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()

        result = {"event": payload["event"], "delivered": False, "status": None, "attempts": 0, "error": None}
        for attempt in range(1, self.max_attempts + 1):
            result["attempts"] = attempt
            try:
                resp = self._http().post(self.url, content=body, headers=headers, timeout=self.timeout_s)
                result["status"] = resp.status_code
                if resp.status_code < 400:
                    result["delivered"] = True
                    result["error"] = None
                    break
                result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                # 4xx (tranne 408/429) = errore nostro o URL sbagliato: riprovare non serve
                if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                    break
            except httpx.HTTPError as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_attempts and self.retry_delay_s > 0:
                time.sleep(self.retry_delay_s * attempt)

        with self._lock:
            self.stats["sent" if result["delivered"] else "failed"] += 1
            self.last_delivery = {**result, "at": now_ms()}
        if result["delivered"]:
            log.info("Webhook %s consegnato a n8n (HTTP %s)", payload["event"], result["status"])
        else:
            log.warning("Webhook %s NON consegnato dopo %d tentativi: %s", payload["event"], result["attempts"], result["error"])
        return result

    def _http(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout_s, follow_redirects=False)
        return self.client

    def close(self) -> None:
        self._executor.shutdown(wait=False)
        if self.client is not None:
            self.client.close()


def _mask_url(url: str) -> str:
    """Nasconde l'ultimo segmento del percorso (spesso un UUID segreto del webhook n8n)."""
    if not url:
        return ""
    head, sep, tail = url.rstrip("/").rpartition("/")
    if not sep or head.endswith(":/") or len(tail) <= 4:
        return url
    return f"{head}/{tail[:2]}…{tail[-2:]}"
