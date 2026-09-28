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
