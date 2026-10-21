"""Profilo personale dell'invitato (link via email) ed email tramite n8n (evento mail.send)."""


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
        # senza token si vede solo il proprio record (qui con il link personale), senza email; gli organizzatori vedono tutto
        assert c.get("/api/guests").json() == []
        pub = next(g for g in c.get("/api/guests", headers={"X-Guest-Token": token}).json() if g["fullName"] == "Zia Pina")
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
        # luogo della festa: solo il Giardino dei Tempi, non l'aula della seduta
        assert "Giardino dei Tempi" in conf["text"] and "Aula Magna" not in conf["text"] and "Aula Magna" not in conf["html"]
        assert "guest.updated" in n8n.events and n8n.payload(-2)["data"]["viaProfile"] is True
        # riepilogo per il catering con i menu
        summary = c.get("/api/guests/summary", headers=ADMIN).json()
        assert {m["menu"]: m["covers"] for m in summary["byMenu"]}["VEGETARIANO"] == 1
        assert {m["menu"]: m["covers"] for m in summary["byMenu"]}["SENZA_GLUTINE"] == 1
        # declinare azzera le persone
        r = c.put("/api/profile", json={**body, "rsvpStatus": "DECLINED", "consent": True}, headers=H)
        assert r.json()["guest"]["guestsCount"] == 0 and "dispiace" in _mail_events(n8n)[-1]["subject"]

        # 3) nuovo link per un'email già in lista (senza nome) e logout che invalida il token
        r = c.post("/api/profile/request-link", json={"email": "zia.pina@example.org"})
        assert r.status_code == 202 and r.json()["created"] is False and r.json()["queued"] is True
        assert "#profilo=" + token in _mail_events(n8n)[-1]["text"]  # stesso token finché non si esce
        # codice a 6 cifre nell'email (per l'app installata su iPhone): un nuovo link ne manda uno nuovo
        code = _mail_events(n8n)[-1]["text"].split("codice: ")[1].split()[0]
        first_code = mails[0]["text"].split("codice: ")[1].split()[0]
        assert len(code) == 6 and code.isdigit() and code in _mail_events(n8n)[-1]["html"]
        V = "/api/profile/verify-code"
        assert c.post(V, json={"email": "zia.pina@example.org", "code": first_code}).status_code == 401 or first_code == code
        assert c.post(V, json={"email": "nessuno@example.org", "code": code}).status_code == 401  # email sconosciuta: stessa risposta
        assert c.post(V, json={"email": "zia.pina@example.org", "code": "12"}).status_code == 401
        r = c.post(V, json={"email": "Zia.Pina@example.org", "code": code[:3] + " " + code[3:]})  # spazi e maiuscole tollerati
        assert r.status_code == 200 and r.json()["token"] == token
        assert c.post(V, json={"email": "zia.pina@example.org", "code": code}).status_code == 401  # vale una volta sola
        # cinque tentativi sbagliati bruciano il codice
        c.post("/api/profile/request-link", json={"email": "zia.pina@example.org"})
        code = _mail_events(n8n)[-1]["text"].split("codice: ")[1].split()[0]
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            assert c.post(V, json={"email": "zia.pina@example.org", "code": wrong}).status_code == 401
        assert c.post(V, json={"email": "zia.pina@example.org", "code": code}).status_code == 401
        # scaduto
        c.post("/api/profile/request-link", json={"email": "zia.pina@example.org"})
        code = _mail_events(n8n)[-1]["text"].split("codice: ")[1].split()[0]
        with c.app.state.session_factory() as db:
            from app.models import Guest
            g = db.query(Guest).filter(Guest.email == "zia.pina@example.org").one()
            g.login_code_expires_at = 1
            db.commit()
        assert c.post(V, json={"email": "zia.pina@example.org", "code": code}).status_code == 401
        # il logout annulla anche un codice in corso
        c.post("/api/profile/request-link", json={"email": "zia.pina@example.org"})
        code = _mail_events(n8n)[-1]["text"].split("codice: ")[1].split()[0]
        assert c.post("/api/profile/logout", headers=H).json()["ok"] is True
        assert c.get("/api/profile", headers=H).status_code == 401
        assert c.post(V, json={"email": "zia.pina@example.org", "code": code}).status_code == 401
        # email sconosciuta senza nome: risposta neutra, nessuna email
        before = len(_mail_events(n8n))
        r = c.post("/api/profile/request-link", json={"email": "nessuno@example.org"})
        assert r.status_code == 202 and r.json()["created"] is False and len(_mail_events(n8n)) == before
        assert c.post("/api/profile/request-link", json={"email": "non-valida"}).status_code == 422

        # 4) organizzatori: link e invito per un invitato demo
        gatti = next(g for g in c.get("/api/guests", headers=ADMIN).json() if g["fullName"].endswith("Gatti"))
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
    anna = next(g for g in client.get("/api/guests", headers=ADMIN).json() if g["fullName"] == "Anna")
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
        g = c.get("/api/guests", headers=ADMIN).json()[0]
        assert g["fullName"] == "Vecchio" and g["companions"] == [] and g["menu"] == "" and g["hasProfile"] is False
        assert c.get("/api/guests/summary", headers=ADMIN).json()["byMenu"] == [{"menu": "", "covers": 2}]


def _token_for(c, name):
    g = next(x for x in c.get("/api/guests", headers=ADMIN).json() if x["fullName"] == name)
    return c.get(f"/api/guests/{g['id']}/link", headers=ADMIN).json()["link"].split("#profilo=")[1], g["id"]


def test_bus_from_profile_waitlist_promotion_and_mails(tmp_path):
    n8n = FakeN8n()
    with make_client(tmp_path, webhook_url=N8N_URL, webhook_handler=n8n) as c:
        c.app.state.mailer.sync = True
        # due profili con email
        for name, email in (("Dott. Luca Gatti", "gatti@example.org"), ("Dott.ssa Sofia Valenti", "sofia@example.org")):
            token, _ = _token_for(c, name)
            c.put("/api/profile", json={"fullName": name, "rsvpStatus": "CONFIRMED", "email": email, "consent": True,
                                        "companions": [{"name": "Ospite", "menu": "STANDARD"}] if "Gatti" in name else []},
                  headers={"X-Guest-Token": token})
        gatti_token, gatti_id = _token_for(c, "Dott. Luca Gatti")
        sofia_token, _ = _token_for(c, "Dott.ssa Sofia Valenti")
        G, S = {"X-Guest-Token": gatti_token}, {"X-Guest-Token": sofia_token}

        prof = c.get("/api/profile", headers=G).json()
        assert prof["busBooking"] is None and prof["bus"]["availableSeats"] == 44 and prof["deadlines"]["busDeadline"] == "2026-11-11"

        # prenotazione dal profilo: posti = persone (2), email di conferma con il file calendario
        r = c.put("/api/profile/bus", json={"pickupStop": "Piazza Giulio Cesare (Policlinico di Bari)", "returnTripWanted": True}, headers=G)
        assert r.status_code == 200, r.text
        b = r.json()["busBooking"]
        assert b["seatsCount"] == 2 and b["status"] == "CONFIRMED" and b["guestId"] == gatti_id and b["code"].startswith("NP-")
        assert b["passengerName"] == "Dott. Luca Gatti" and r.json()["bus"]["bookedSeats"] == 12
        mail = _mail_events(n8n)[-1]
        assert mail["kind"] == "bus_confirmation" and mail["to"] == "gatti@example.org" and b["code"] in mail["subject"]
        assert "Piazza Giulio Cesare" in mail["text"] and mail["attachments"][0]["filename"] == "navetta.ics"
        import base64
        ics = base64.b64decode(mail["attachments"][0]["contentBase64"]).decode()
        assert "BEGIN:VEVENT" in ics and "DTSTART;VALUE=DATE:20261113" in ics and b["code"] in ics  # orario navetta da definire: tutto il giorno
        # una sola prenotazione per profilo: la seconda chiamata la aggiorna
        r = c.put("/api/profile/bus", json={"seatsCount": 3, "pickupStop": "Piazza Giulio Cesare (Policlinico di Bari)"}, headers=G)
        assert r.json()["busBooking"]["seatsCount"] == 3 and r.json()["busBooking"]["id"] == b["id"]
        assert len([x for x in c.get("/api/bus/bookings", headers=ADMIN).json() if x["guestId"] == gatti_id]) == 1

        # Sofia chiede 42 posti: ne restano 41 -> lista d'attesa + email dedicata
        r = c.put("/api/profile/bus", json={"seatsCount": 42}, headers=S)
        assert r.status_code == 200 and r.json()["busBooking"]["status"] == "WAITLIST"
        assert _mail_events(n8n)[-1]["kind"] == "bus_waitlist" and "lista d'attesa" in _mail_events(n8n)[-1]["subject"]
        assert c.get("/api/bus/summary").json()["waitlistSeats"] == 42
        # Gatti annulla: Sofia viene promossa e riceve "posto confermato" con il calendario
        r = c.delete("/api/profile/bus", headers=G)
        assert r.status_code == 200 and r.json()["busBooking"] is None
        kinds = [m["kind"] for m in _mail_events(n8n)]
        assert kinds[-2:] == ["bus_promoted", "bus_cancelled"] or kinds[-2:] == ["bus_cancelled", "bus_promoted"]
        promoted = next(m for m in _mail_events(n8n) if m["kind"] == "bus_promoted")
        assert promoted["to"] == "sofia@example.org" and promoted["attachments"]
        assert "bus.promoted" in n8n.events
        assert c.get("/api/profile", headers=S).json()["busBooking"]["status"] == "CONFIRMED"
        assert c.get("/api/bus/summary").json() == {**c.get("/api/bus/summary").json(), "bookedSeats": 52, "waitlistSeats": 0}
        assert c.delete("/api/profile/bus", headers=G).status_code == 404

        # check-in degli organizzatori e CSV
        sofia_b = c.get("/api/profile", headers=S).json()["busBooking"]
        assert c.post(f"/api/bus/bookings/{sofia_b['id']}/checkin", json={"checkedIn": True}).status_code == 403
        assert c.post(f"/api/bus/bookings/{sofia_b['id']}/checkin", json={"checkedIn": True}, headers=ADMIN).json()["checkedInAt"]
        assert c.get("/api/bus/summary").json()["checkedInSeats"] == 42
        lines = c.get("/api/export/bus.csv", headers=ADMIN).text.lstrip("\ufeff").splitlines()
        row = next(ln for ln in lines if ln.startswith("Dott.ssa Sofia Valenti;"))
        assert row.split(";")[2:4] == ["Confermata", sofia_b["code"]] and row.split(";")[7:9] == ["sofia@example.org", "Sì"]

        # promemoria navetta (token automazioni/organizzatore) e promemoria menu
        r = c.post("/api/mail/reminders/bus", headers=ADMIN)
        assert r.status_code == 200 and r.json()["sent"] == 1 and _mail_events(n8n)[-1]["kind"] == "bus_reminder"
        assert "Domani la navetta" in _mail_events(n8n)[-1]["subject"]
        r = c.post("/api/mail/reminders/menu", headers=ADMIN)
        # Gatti e Sofia non hanno scelto il menu (Gatti: suo; Sofia: suo) -> 2 promemoria
        assert r.json()["sent"] == 2 and _mail_events(n8n)[-1]["kind"] == "menu_reminder" and "fino al venerdì 6 novembre 2026" in _mail_events(n8n)[-1]["text"]
        assert c.post("/api/mail/reminders/menu").status_code == 403
        # chi non partecipa non può prenotare
        c.put("/api/profile", json={"fullName": "Dott.ssa Sofia Valenti", "rsvpStatus": "DECLINED", "email": "sofia@example.org", "consent": True}, headers=S)
        assert c.put("/api/profile/bus", json={}, headers=S).status_code == 409


def test_catering_summary_allergies_missing_menu_and_guest_csv(client):
    token, _ = _token_for(client, "Dott. Luca Gatti")
    client.put("/api/profile", json={"fullName": "Dott. Luca Gatti", "rsvpStatus": "CONFIRMED", "menu": "VEGANO", "dietaryNotes": "allergia ai crostacei",
                                     "companions": [{"name": "Marta", "menu": "", "notes": "intollerante al lattosio"}], "consent": True, "phone": "333 1"},
               headers={"X-Guest-Token": token})
    s = client.get("/api/guests/summary", headers=ADMIN).json()
    assert {a["name"]: a["notes"] for a in s["allergies"]} == {**{a["name"]: a["notes"] for a in s["allergies"]}, "Dott. Luca Gatti": "allergia ai crostacei", "Marta": "intollerante al lattosio"}
    assert "Marta (con Dott. Luca Gatti)" in s["missingMenu"]
    lines = client.get("/api/export/guests.csv", headers=ADMIN).text.lstrip("\ufeff").splitlines()
    row = next(ln for ln in lines if ln.startswith("Dott. Luca Gatti;"))
    assert row.split(";")[4:6] == ["vegano", "Marta: da scegliere (intollerante al lattosio)"] and row.split(";")[8] == "333 1"


def test_menu_and_bus_deadlines_lock_profile_changes(tmp_path):
    import json as _json

    from conftest import SEED

    with open(SEED, encoding="utf-8") as f:
        data = _json.load(f)
    data["meta"]["menuDeadline"] = "2020-01-01"
    data["meta"]["busDeadline"] = "2020-01-01"
    seed = tmp_path / "past.json"
    seed.write_text(_json.dumps(data), encoding="utf-8")
    with make_client(tmp_path, seed_file=str(seed)) as c:
        c.app.state.mailer.sync = True
        token, _ = _token_for(c, "Dott. Luca Gatti")
        H = {"X-Guest-Token": token}
        prof = c.get("/api/profile", headers=H).json()
        assert prof["deadlines"]["menuLocked"] is True and prof["deadlines"]["busLocked"] is True
        assert c.get("/api/bus/summary").json()["busLocked"] is True
        base = {"fullName": "Dott. Luca Gatti", "rsvpStatus": prof["guest"]["rsvpStatus"], "consent": True, "menu": prof["guest"]["menu"], "dietaryNotes": prof["guest"]["dietaryNotes"]}
        assert c.put("/api/profile", json={**base, "phone": "333"}, headers=H).status_code == 200  # contatti ancora modificabili
        assert c.put("/api/profile", json={**base, "menu": "VEGANO"}, headers=H).status_code == 409
        assert c.put("/api/profile/bus", json={}, headers=H).status_code == 409
        assert c.post("/api/mail/reminders/menu", headers=ADMIN).json()["sent"] == 0
