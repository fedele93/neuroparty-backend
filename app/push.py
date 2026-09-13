"""Web Push (standard W3C, funziona su Chrome/Firefox/Edge e su iOS 16.4+ con PWA installata)."""
import base64
import json
import logging
import os

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import PushSubscription

log = logging.getLogger("neuroparty.push")


class PushService:
    def __init__(self, key_path: str, subject: str):
        self.key_path = key_path
        self.subject = subject
        self.vapid = self._load_or_create(key_path)

    @staticmethod
    def _load_or_create(key_path: str) -> Vapid:
        if os.path.exists(key_path):
            return Vapid.from_file(key_path)
        os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
        v = Vapid()
        v.generate_keys()
        v.save_key(key_path)
        log.info("Generate nuove chiavi VAPID in %s", key_path)
        return v

    @property
    def public_key_b64url(self) -> str:
        """Chiave pubblica nel formato che il browser si aspetta (applicationServerKey)."""
        raw = self.vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def broadcast(self, session_factory, title: str, body: str, category: str, notification_id: int) -> dict:
        """Invia la notifica a tutte le sottoscrizioni; rimuove quelle scadute (404/410)."""
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "category": category,
                "id": notification_id,
                "url": "./index.html",
            }
        )
        sent, removed, failed = 0, 0, 0
        with session_factory() as db:
            subs = db.scalars(select(PushSubscription)).all()
            for sub in subs:
                info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
                try:
                    webpush(
                        subscription_info=info,
                        data=payload,
                        vapid_private_key=self.key_path,
                        vapid_claims={"sub": self.subject},
                        ttl=3600,
                        timeout=10,
                    )
                    sent += 1
                except WebPushException as exc:
                    status = getattr(exc.response, "status_code", None) if exc.response is not None else None
                    if status in (404, 410):
                        db.execute(delete(PushSubscription).where(PushSubscription.id == sub.id))
                        removed += 1
                    else:
                        failed += 1
                        log.warning("Web push fallita (%s): %s", status, exc)
                except Exception as exc:  # rete assente, DNS, ecc.
                    failed += 1
                    log.warning("Web push errore: %s", exc)
            db.commit()
        result = {"sent": sent, "removed": removed, "failed": failed}
        log.info("Broadcast notifica #%s: %s", notification_id, result)
        return result


def subscription_count(db: Session) -> int:
    return len(db.scalars(select(PushSubscription)).all())
