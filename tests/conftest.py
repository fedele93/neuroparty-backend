import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed", "event-data.json")


def make_client(
    tmp_path,
    demo=True,
    admin_token="segreto-test",
    seed_file=SEED,
    update_gift_texts=False,
    automation_token="",
    treasurer_token="cassa-test",  # i test che vogliono "nessun cassiere" passano treasurer_token=""
    treasurer_tokens=None,
    graduate_tokens=None,
    assistant_fake=False,
    mistral_api_key="",
    webhook_url="",
    webhook_secret="",
    webhook_events="*",
    webhook_handler=None,
):
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        seed_file=seed_file,
        gift_sync_update_texts=update_gift_texts,
        seed_demo_data=demo,
        admin_token=admin_token,
        public_url="https://festa.example.org",
        automation_token=automation_token,
        treasurer_token=treasurer_token,
        treasurer_tokens=treasurer_tokens or {},
        graduate_tokens=graduate_tokens or {},
        assistant_fake=assistant_fake,
        mistral_api_key=mistral_api_key,
        n8n_webhook_url=webhook_url,
        n8n_webhook_secret=webhook_secret,
        n8n_webhook_events=webhook_events,
    )
    app = create_app(settings)
    if webhook_handler is not None:
        # niente rete nei test: le chiamate verso "n8n" finiscono in webhook_handler, in modo sincrono
        hooks = app.state.webhooks
        hooks.client = httpx.Client(transport=httpx.MockTransport(webhook_handler))
        hooks.sync = True
        hooks.retry_delay_s = 0
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


@pytest.fixture
def empty_client(tmp_path):
    with make_client(tmp_path, demo=False) as c:
        yield c


ADMIN = {"X-Admin-Token": "segreto-test"}


def guest_headers(client, full_name="Ospite Registrato", rsvp_status="CONFIRMED", token=None) -> dict:
    """Invitato registrato (come se avesse aperto il link personale): header X-Guest-Token."""
    from app.models import Guest

    token = token or "tok-" + full_name.lower().replace(" ", "-")
    with client.app.state.session_factory() as db:
        db.add(Guest(full_name=full_name, rsvp_status=rsvp_status, guests_count=1, email=f"{token}@example.org", access_token=token))
        db.commit()
    return {"X-Guest-Token": token}
TREASURER = {"X-Treasurer-Token": "cassa-test"}
