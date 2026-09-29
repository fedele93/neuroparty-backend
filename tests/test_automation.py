"""Integrazione con n8n: webhook in uscita, token automazioni, report e stato."""
import hashlib
import hmac
import json

import httpx

from conftest import ADMIN, make_client

AUTOMATION = {"X-Automation-Token": "n8n-test"}
N8N_URL = "https://n8n.example.org/webhook/neuroparty-abc123"


class FakeN8n:
    """Riceve le chiamate del backend come farebbe il nodo Webhook di n8n."""

    def __init__(self, status=200, fail_first=0):
        self.calls: list[httpx.Request] = []
        self.status = status
        self.fail_first = fail_first

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.fail_first and len(self.calls) <= self.fail_first:
            raise httpx.ConnectError("n8n spento")
        return httpx.Response(self.status, json={"ok": True})

    @property
    def events(self) -> list[str]:
        return [json.loads(c.content)["event"] for c in self.calls]

    def payload(self, i: int = -1) -> dict:
        return json.loads(self.calls[i].content)


def test_webhooks_disabled_by_default(client):
    st = client.get("/api/automation/status", headers=ADMIN).json()
    assert st["webhook"]["enabled"] is False and st["automationTokenEnabled"] is False
    assert {e["name"] for e in st["events"]} >= {"guest.created", "bus.booked", "notification.published", "test"}
    assert client.post("/api/automation/webhook-test", headers=ADMIN).status_code == 503
    # senza URL non si tenta nessuna chiamata (contatore "skipped")
    client.post("/api/wishes", json={"message": "Auguri!"})
    assert client.app.state.webhooks.stats == {"sent": 0, "failed": 0, "skipped": 1}


def test_webhook_events_and_signature(tmp_path):
    n8n = FakeN8n()
    with make_client(tmp_path, webhook_url=N8N_URL, webhook_secret="shh", webhook_handler=n8n) as c:
        # invitato: creazione, cambio RSVP, cancellazione
        g = c.post("/api/guests", json={"fullName": "Mario Rossi", "guestsCount": 2}, headers={"X-Client-Id": "a"}).json()
        c.put(f"/api/guests/{g['id']}", json={"rsvpStatus": "DECLINED"}, headers={"X-Client-Id": "b"})
        c.delete(f"/api/guests/{g['id']}", headers={"X-Client-Id": "a"})
        # navetta, augurio, quota regalo, notifica immediata
        b = c.post("/api/bus/bookings", json={"passengerName": "Anna", "seatsCount": 3}, headers={"X-Client-Id": "a"}).json()
        c.delete(f"/api/bus/bookings/{b['id']}", headers={"X-Client-Id": "a"})
        c.post("/api/wishes", json={"authorName": "Zia", "message": "Bravi!"})
        target = c.get("/api/gifts/targets").json()[0]["id"]
        c.post("/api/gifts/contributions", json={"donorName": "Nonna", "targetGraduateId": target, "amount": 50})
        c.post("/api/notifications", json={"title": "Ciao", "message": "a tutti"}, headers=ADMIN)

        assert n8n.events == [
            "guest.created", "guest.updated", "guest.deleted",
            "bus.booked", "bus.cancelled", "wish.created", "gift.contributed", "notification.published",
        ]
        created = n8n.payload(0)
        assert created["source"] == "neuroparty" and created["data"]["guest"]["fullName"] == "Mario Rossi"
        assert created["data"]["summary"]["covers"] >= 2
        assert n8n.payload(1)["data"]["changes"] == {"rsvpStatus": "DECLINED"}
        assert n8n.payload(3)["data"]["bus"]["bookedSeats"] == 13  # 10 demo + 3
        assert n8n.payload(4)["data"]["bus"]["bookedSeats"] == 10
        assert n8n.payload(6)["data"]["target"]["collectedAmount"] > 0
        assert n8n.payload(7)["data"]["scheduled"] is False

        # header: evento, segreto condiviso e firma HMAC del corpo
        req = n8n.calls[0]
        assert str(req.url) == N8N_URL
        assert req.headers["X-NeuroParty-Event"] == "guest.created"
        assert req.headers["X-Automation-Secret"] == "shh"
        expected = "sha256=" + hmac.new(b"shh", req.content, hashlib.sha256).hexdigest()
        assert req.headers["X-NeuroParty-Signature"] == expected
        assert c.app.state.webhooks.stats["sent"] == 8

        st = c.get("/api/automation/status", headers=ADMIN).json()["webhook"]
        assert st["enabled"] and st["secretConfigured"] and st["lastDelivery"]["delivered"] is True
        assert "abc123" not in st["url"]  # il percorso segreto del webhook non viene esposto


def test_webhook_scheduled_notification_and_scheduler(tmp_path):
    from app.scheduler import publish_due_notifications

    n8n = FakeN8n()
    with make_client(tmp_path, webhook_url=N8N_URL, webhook_handler=n8n) as c:
        future = c.get("/api/state").json()["serverTime"] + 3_600_000
        c.post("/api/notifications", json={"title": "Navetta", "message": "Si parte", "sendAt": future}, headers=ADMIN)
        assert n8n.events == ["notification.scheduled"]
        app = c.app
        publish_due_notifications(app.state.session_factory, app.state.push, now=future + 1, webhooks=app.state.webhooks)
        assert n8n.events == ["notification.scheduled", "notification.published"]
        assert n8n.payload()["data"]["scheduled"] is True and n8n.payload()["data"]["notification"]["title"] == "Navetta"


def test_webhook_event_filter(tmp_path):
    n8n = FakeN8n()
    with make_client(tmp_path, webhook_url=N8N_URL, webhook_events="guest.*, bus.booked", webhook_handler=n8n) as c:
        c.post("/api/wishes", json={"message": "ignorato"})
        c.post("/api/guests", json={"fullName": "Lia"})
        c.post("/api/bus/bookings", json={"passengerName": "Ugo"})
        assert n8n.events == ["guest.created", "bus.booked"]
        assert c.app.state.webhooks.stats["skipped"] == 1
        # "test" non rientra nel filtro: l'endpoint di prova lo dice chiaramente
        assert c.post("/api/automation/webhook-test", headers=ADMIN).status_code == 409


def test_webhook_retries_and_failures_do_not_break_requests(tmp_path):
    flaky = FakeN8n(fail_first=1)
    with make_client(tmp_path, webhook_url=N8N_URL, webhook_handler=flaky) as c:
        r = c.post("/api/automation/webhook-test", headers=ADMIN)
        assert r.status_code == 200
        assert r.json()["delivered"] is True and r.json()["attempts"] == 2
        assert flaky.payload()["event"] == "test"

    broken = FakeN8n(status=404)  # URL sbagliato: nessun tentativo ripetuto, l'app continua a funzionare
    with make_client(tmp_path / "b", webhook_url=N8N_URL, webhook_handler=broken) as c:
        assert c.post("/api/guests", json={"fullName": "Eva"}).status_code == 201
        assert len(broken.calls) == 1
        r = c.post("/api/automation/webhook-test", headers=ADMIN).json()
        assert r["delivered"] is False and r["status"] == 404 and r["attempts"] == 1
        assert c.app.state.webhooks.stats["failed"] == 2


def test_automation_token_is_read_only(tmp_path):
    with make_client(tmp_path, automation_token="n8n-test") as c:
        assert c.get("/api/automation/report").status_code == 403
        assert c.get("/api/automation/report", headers={"X-Automation-Token": "sbagliato"}).status_code == 403
        assert c.get("/api/automation/report", headers=AUTOMATION).status_code == 200
        assert c.get("/api/automation/report", headers=ADMIN).status_code == 200
        # export CSV: anche con il token automazioni (sola lettura)
        assert c.get("/api/export/guests.csv", headers=AUTOMATION).status_code == 200
        # ma niente azioni da organizzatore
        assert c.post("/api/notifications", json={"title": "a", "message": "b"}, headers=AUTOMATION).status_code == 403
        assert c.get("/api/notifications/scheduled", headers=AUTOMATION).status_code == 403
        assert c.get("/api/automation/status", headers=AUTOMATION).json()["automationTokenEnabled"] is True

    with make_client(tmp_path / "b", admin_token="", automation_token="") as c:
        assert c.get("/api/automation/report", headers=AUTOMATION).status_code == 503


def test_report_json_text_html(client):
    future = client.get("/api/state").json()["serverTime"] + 3_600_000
    client.post("/api/notifications", json={"title": "Promemoria", "message": "x", "sendAt": future}, headers=ADMIN)
    client.post("/api/guests", json={"fullName": "Nuovo Arrivato", "guestsCount": 3, "dietaryNotes": "Senza glutine"})

    rep = client.get("/api/automation/report", headers=ADMIN).json()
    assert rep["event"]["partyDate"] == "2026-11-13" and rep["event"]["title"]
    assert rep["guests"]["totalGuests"] == 8 and rep["guests"]["covers"] == client.get("/api/guests/summary").json()["covers"]
    assert rep["bus"] == {**rep["bus"], "maxSeats": 54, "bookedSeats": 10, "availableSeats": 44, "bookings": 3}
    assert sum(s["seats"] for s in rep["bus"]["byStop"]) == 10
    assert len(rep["gifts"]["targets"]) == 10 and rep["gifts"]["contributions"] == 4
    assert rep["gifts"]["totalCollected"] == round(sum(t["collectedAmount"] for t in rep["gifts"]["targets"]), 2)
    assert rep["wishes"]["count"] == 10 and rep["photos"]["count"] == 3
    assert [n["title"] for n in rep["notifications"]["scheduled"]] == ["Promemoria"]
    # i dati demo vengono creati "adesso", quindi contano anch'essi come novità delle ultime 24 ore
    assert rep["recent"]["counts"]["guests"] == 8 and len(rep["recent"]["guests"]) == 8
    assert "Nuovo Arrivato" in [g["fullName"] for g in rep["recent"]["guests"]]
    assert rep["sinceHours"] == 24

    # finestra "novità" personalizzabile (in ore, anche frazionarie); zero o negativa non ammessa
    small = client.get("/api/automation/report", params={"sinceHours": 0.001}, headers=ADMIN).json()
    assert small["sinceHours"] == 0.001 and small["recent"]["counts"]["guests"] <= 8
    assert client.get("/api/automation/report", params={"sinceHours": 0}, headers=ADMIN).status_code == 422

    txt = client.get("/api/automation/report.txt", headers=ADMIN)
    assert txt.headers["content-type"].startswith("text/plain")
    assert "INVITATI" in txt.text and "Nuovo Arrivato" in txt.text and "Senza glutine" in txt.text
    assert "Promemoria" in txt.text and "App: https://festa.example.org" in txt.text

    page = client.get("/api/automation/report.html", headers=ADMIN)
    assert page.headers["content-type"].startswith("text/html")
    assert page.text.startswith("<!doctype html>") and "<h2" in page.text and "Nuovo Arrivato" in page.text


def test_report_html_escapes_user_content(client):
    client.post("/api/wishes", json={"authorName": "<script>alert(1)</script>", "message": "ciao"})
    page = client.get("/api/automation/report.html", headers=ADMIN).text
    assert "<script>" not in page and "&lt;script&gt;" in page
