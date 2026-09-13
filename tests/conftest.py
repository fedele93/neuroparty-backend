import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed", "event-data.json")


def make_client(tmp_path, demo=True, admin_token="segreto-test"):
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        seed_file=SEED,
        seed_demo_data=demo,
        admin_token=admin_token,
        public_url="https://festa.example.org",
    )
    app = create_app(settings)
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
