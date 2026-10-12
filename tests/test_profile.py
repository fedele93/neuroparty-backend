"""Profilo personale dell'invitato (link via email) ed email tramite n8n (evento mail.send)."""
import json

import httpx

from conftest import ADMIN, make_client
from test_automation import N8N_URL, FakeN8n


def _mail_events(n8n: FakeN8n) -> list[dict]:
    return [n8n.payload(i)["data"] for i, e in enumerate(n8n.events) if e == "mail.send"]


def test_link_request_profile_update_and_confirmation_mail(tmp_path):
    n8n = FakeN8n()
    with make_client(tmp_path, webhook_url=N8N_URL, webhook_secret="shh", webhook_handler=n8n) as c:
        c.app.state.mailer.sync = True
        # 1) un invitato non in lista chiede il link con email + nome: viene creato (in attesa) e riceve il link
        r = c.post("/api/profile/request-link", json={"email": "Zia.Pina@Example.org", "fullName": "Zia Pina"})
        assert r.status_code == 202 and r.json()["created"] is True and r.json()["queued"] is True
        mails = _mail_events(n8n)
        assert len(mails) == 1 and mails[0]["kind"] == "access_link" and mails[0]["to"] == "zia.pina@example.org"
        assert "link personale" in mails[0]["subject"] and "#profilo=" in mails[0]["text"] and "<a href" in mails[0]["html"]
        token = mails[0]["text"].split("#profilo=")[1].split()[0]
        assert mails[0]["outboxId"] >= 1
        # la lista pubblica non mostra l'email; gli organizzatori sì
        pub = next(g for g in c.get("/api/guests").json() if g["fullName"] == "Zia Pina")
        assert "email" not in pub and pub["hasProfile"] is True and pub["rsvpStatus"] == "PENDING"
        adm = next(g for g in c.get("/api/guests", headers=ADMIN).json() if g["fullName"] == "Zia Pina")
        assert adm["email"] == "zia.pina@example.org" and adm["consentAt"] is None
        # outbox consegnata
        box = c.get("/api/mail/outbox", headers=ADMIN).json()
        assert box[0]["status"] == "DELIVERED" and box[0]["kind"] == "access_link" and box[0]["guestId"] == pub["id"]
        assert c.get("/api/mail/status", headers=ADMIN).json()["counts"] == {"DELIVERED": 1}

        # 2) profilo con il token
        assert c.get("/api/profile").status_code == 401
        assert c.get("/api/profile", headers={"X-Guest-Token": "x"}).status_code == 401
        H = {"X-Guest-Token": token}
        me = c.get("/api/profile", headers=H).json()["guest"]
        assert me["fullName"] == "Zia Pina" and me["email"] == "zia.pina@example.org"
        body = {
            "fullName": "Zia Pina", "category": "Famigliari", "rsvpStatus": "CONFIRMED", "email": "zia.pina@example.org",
            "phone": "333 1234567", "menu": "VEGETARIANO", "dietaryNotes": "allergia alle noci",
            "companions": [{"name": "Zio Tonio", "menu": "STANDARD"}, {"name": "Nipotina", "menu": "SENZA_GLUTINE", "notes": "celiaca"}, {"name": "  ", "menu": ""}],
        }
        assert c.put("/api/profile", json=body, headers=H).status_code == 422  # manca il consenso
        r = c.put("/api/profile", json={**body, "consent": True}, headers=H)
        assert r.status_code == 200, r.text
        g = r.json()["guest"]
        assert g["guestsCount"] == 3 and g["menu"] == "VEGETARIANO" and [x["name"] for x in g["companions"]] == ["Zio Tonio", "Nipotina"]
        assert g["consentAt"] and g["contactInfo"] == "zia.pina@example.org"
        assert r.json()["mail"]["kind"] == "rsvp_confirmation" and r.json()["mail"]["status"] == "DELIVERED"
        conf = _mail_events(n8n)[-1]
        assert conf["to"] == "zia.pina@example.org" and "Confermata" in conf["subject"] and "3 persone" in conf["subject"]
        assert "Zio Tonio: menu completo" in conf["text"] and "Nipotina: senza glutine (celiaca)" in conf["text"] and "allergia alle noci" in conf["text"]
        assert "guest.updated" in n8n.events and n8n.payload(-2)["data"]["viaProfile"] is True
        # riepilogo per il catering con i menu
        summary = c.get("/api/guests/summary").json()
        assert {m["menu"]: m["covers"] for m in summary["byMenu"]}["VEGETARIANO"] == 1
        assert {m["menu"]: m["covers"] for m in summary["byMenu"]}["SENZA_GLUTINE"] == 1
        # declinare azzera le persone
        r = c.put("/api/profile", json={**body, "rsvpStatus": "DECLINED", "consent": True}, headers=H)
        assert r.json()["guest"]["guestsCount"] == 0 and "dispiace" in _mail_events(n8n)[-1]["subject"]

        # 3) nuovo link per un'email già in lista (senza nome) e logout che invalida il token
        r = c.post("/api/profile/request-link", json={"email": "zia.pina@example.org"})
        assert r.status_code == 202 and r.json()["created"] is False and r.json()["queued"] is True
        assert "#profilo=" + token in _mail_events(n8n)[-1]["text"]  # stesso token finché non si esce
        assert c.post("/api/profile/logout", headers=H).json()["ok"] is True
        assert c.get("/api/profile", headers=H).status_code == 401
        # email sconosciuta senza nome: risposta neutra, nessuna email
        before = len(_mail_events(n8n))
        r = c.post("/api/profile/request-link", json={"email": "nessuno@example.org"})
        assert r.status_code == 202 and r.json()["created"] is False and len(_mail_events(n8n)) == before
        assert c.post("/api/profile/request-link", json={"email": "non-valida"}).status_code == 422

        # 4) organizzatori: link e invito per un invitato demo
        gatti = next(g for g in c.get("/api/guests").json() if g["fullName"].endswith("Gatti"))
        link = c.get(f"/api/guests/{gatti['id']}/link", headers=ADMIN).json()
        assert link["link"].startswith("https://festa.example.org/#profilo=") and link["guest"]["email"] == ""
        assert c.get(f"/api/guests/{gatti['id']}/link").status_code == 403
        assert c.post(f"/api/guests/{gatti['id']}/invite", headers=ADMIN).status_code == 422  # senza email
        c.put(f"/api/guests/{gatti['id']}", json={"contactInfo": "x"}, headers=ADMIN)
        gatti_token = link["link"].split("#profilo=")[1]
        c.put("/api/profile", json={"fullName": gatti["fullName"], "rsvpStatus": "CONFIRMED", "email": "gatti@example.org", "consent": True},
              headers={"X-Guest-Token": gatti_token})
        r = c.post(f"/api/guests/{gatti['id']}/invite", headers=ADMIN)
        assert r.status_code == 202 and r.json()["mail"]["status"] == "DELIVERED" and _mail_events(n8n)[-1]["to"] == "gatti@example.org"

        # 5) email di prova dal pannello
        r = c.post("/api/mail/test", json={"to": "me@example.org"}, headers=ADMIN)
        assert r.status_code == 202 and r.json()["status"] == "DELIVERED" and _mail_events(n8n)[-1]["kind"] == "test"


def test_mail_skipped_without_n8n(client):
    client.app.state.mailer.sync = True
    r = client.post("/api/profile/request-link", json={"email": "a@b.it", "fullName": "Anna"})
    assert r.status_code == 202 and r.json()["queued"] is False and "organizzatori" in r.json()["message"]
    box = client.get("/api/mail/outbox", headers=ADMIN).json()
    assert box[0]["status"] == "SKIPPED" and "n8n" in box[0]["error"]
    assert client.get("/api/mail/status", headers=ADMIN).json()["enabled"] is False
    assert client.post(f"/api/mail/outbox/{box[0]['id']}/resend", headers=ADMIN).json()["status"] == "SKIPPED"
    assert client.post("/api/mail/outbox/999/resend", headers=ADMIN).status_code == 404
    assert client.get("/api/mail/outbox").status_code == 403
    # il profilo funziona lo stesso tramite il link copiato dagli organizzatori
    anna = next(g for g in client.get("/api/guests").json() if g["fullName"] == "Anna")
    token = client.get(f"/api/guests/{anna['id']}/link", headers=ADMIN).json()["link"].split("#profilo=")[1]
    assert client.get("/api/profile", headers={"X-Guest-Token": token}).json()["guest"]["fullName"] == "Anna"


def test_mail_failure_then_resend(tmp_path):
    n8n = FakeN8n(status=500)
    with make_client(tmp_path, webhook_url=N8N_URL, webhook_handler=n8n) as c:
        c.app.state.mailer.sync = True
        row = c.post("/api/mail/test", json={"to": "me@example.org"}, headers=ADMIN).json()
        assert row["status"] == "FAILED" and row["attempts"] == 3 and "HTTP 500" in row["error"]
        n8n.status = 200
        row = c.post(f"/api/mail/outbox/{row['id']}/resend", headers=ADMIN).json()
        assert row["status"] == "DELIVERED" and row["attempts"] == 4 and row["error"] == ""
        assert c.get("/api/mail/status", headers=ADMIN).json()["counts"] == {"DELIVERED": 1}


def test_mail_send_event_can_be_filtered_out(tmp_path):
    n8n = FakeN8n()
    with make_client(tmp_path, webhook_url=N8N_URL, webhook_events="guest.*,bus.*", webhook_handler=n8n) as c:
        c.app.state.mailer.sync = True
        assert c.get("/api/mail/status", headers=ADMIN).json()["enabled"] is False
        assert c.post("/api/mail/test", json={"to": "me@example.org"}, headers=ADMIN).json()["status"] == "SKIPPED"
        assert "mail.send" not in n8n.events


def test_schema_migration_adds_profile_columns(tmp_path):
    import sqlite3

    from app.db import ensure_schema, make_engine

    (tmp_path / "data").mkdir()
    path = tmp_path / "data" / "neuroparty.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE guests (id INTEGER PRIMARY KEY, full_name VARCHAR(200), category VARCHAR(100), rsvp_status VARCHAR(20), "
                "guests_count INTEGER, dietary_notes TEXT, contact_info VARCHAR(200), updated_at INTEGER, owner_client_id VARCHAR(64))")
    con.execute("INSERT INTO guests (full_name, category, rsvp_status, guests_count, dietary_notes, contact_info, updated_at) VALUES ('Vecchio', 'Amici', 'CONFIRMED', 2, '', '', 1)")
    con.commit(); con.close()
    added = ensure_schema(make_engine(str(path)))
    assert {"+guests.email", "+guests.menu", "+guests.companions_json", "+guests.access_token", "+guests.consent_at"} <= set(added)
    with make_client(tmp_path, demo=False) as c:
        g = c.get("/api/guests").json()[0]
        assert g["fullName"] == "Vecchio" and g["companions"] == [] and g["menu"] == "" and g["hasProfile"] is False
        assert c.get("/api/guests/summary").json()["byMenu"] == [{"menu": "", "covers": 2}]
