"""Fase 5: assistente sul profilo, import invitati, cruscotto organizzatori."""


from conftest import ADMIN, make_client
from test_automation import N8N_URL, FakeN8n


def _mails(n8n: FakeN8n) -> list[dict]:
    return [n8n.payload(i)["data"] for i, e in enumerate(n8n.events) if e == "mail.send"]


def _client(tmp_path, n8n: FakeN8n):
    c = make_client(tmp_path, webhook_url=N8N_URL, webhook_secret="shh", webhook_handler=n8n, assistant_fake=True)
    c.app.state.mailer.sync = True
    return c


def _token_for(c, n8n, email, name):
    c.post("/api/profile/request-link", json={"email": email, "fullName": name})
    return _mails(n8n)[-1]["text"].split("#profilo=")[1].split()[0]


def _talk(c, text, headers=None):
    r = c.post("/api/assistant/talk", data={"avatarId": "luisi", "text": text, "wantAudio": "false"}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def test_assistant_reads_and_updates_profile(tmp_path):
    n8n = FakeN8n()
    with _client(tmp_path, n8n) as c:
        # senza profilo: lo strumento non c'è e l'avatar rimanda al profilo
        out = _talk(c, "Qual è il mio menu?")
        assert out["profile"] is False and out["actions"] == [] and "omino" in out["reply"]
        token = _token_for(c, n8n, "anna@example.org", "Anna Verdi")
        H = {"X-Guest-Token": token}
        # profilo senza consenso: lettura ok, modifica rifiutata con invito ad aprire il profilo
        out = _talk(c, "Qual è il mio profilo?", H)
        assert out["profile"] is True and out["actions"][0]["tool"] == "get_my_profile" and out["actions"][0]["ok"]
        assert "Anna Verdi" in out["reply"] and "da scegliere" in out["reply"] and "nessuna prenotazione" in out["reply"]
        out = _talk(c, "Cambia il mio menu in vegetariano", H)
        assert out["actions"][0]["tool"] == "update_my_profile" and out["actions"][0]["ok"] is False and out["navigate"] == "rsvp"
        assert "consenso" in out["reply"]
        # con il consenso dato dal profilo, l'assistente aggiorna il menu e arriva l'email di conferma
        r = c.put("/api/profile", json={"fullName": "Anna Verdi", "rsvpStatus": "CONFIRMED", "email": "anna@example.org", "consent": True,
                                        "companions": [{"name": "Luca", "menu": "VEGANO"}]}, headers=H)
        assert r.status_code == 200
        n_mails = len(_mails(n8n))
        out = _talk(c, "Cambia il mio menu in vegetariano", H)
        assert out["actions"][0]["ok"], out
        assert "vegetariano" in out["reply"] and "Luca (vegano)" in out["reply"] and out["navigate"] == "rsvp"
        me = c.get("/api/profile", headers=H).json()["guest"]
        assert me["menu"] == "VEGETARIANO" and me["companions"][0]["menu"] == "VEGANO" and me["guestsCount"] == 2
        assert len(_mails(n8n)) == n_mails + 1 and _mails(n8n)[-1]["kind"] == "rsvp_confirmation"
        # navetta dal profilo tramite l'assistente: posti = persone del profilo, poi annullamento
        out = _talk(c, "Prenota la mia navetta", H)
        assert out["actions"][0]["tool"] == "book_my_bus" and out["actions"][0]["ok"] and out["navigate"] == "bus"
        assert "2 posti" in out["reply"] and "NP-" in out["reply"]
        booking = c.get("/api/profile", headers=H).json()["busBooking"]
        assert booking and booking["seatsCount"] == 2 and booking["status"] == "CONFIRMED"
        assert _mails(n8n)[-1]["kind"] == "bus_confirmation"
        out = _talk(c, "Annulla la mia navetta", H)
        assert out["actions"][0]["tool"] == "cancel_my_bus" and out["actions"][0]["ok"]
        assert c.get("/api/profile", headers=H).json()["busBooking"] is None
        # token sbagliato = nessun profilo (nessun errore)
        out = _talk(c, "Qual è il mio profilo?", {"X-Guest-Token": "sbagliato"})
        assert out["profile"] is False and out["actions"] == []
        # la cronologia conserva il contesto del profilo (facts nel system prompt): strumento solo con token
        assert all(t["function"]["name"] != "get_my_profile" for t in __import__("app.assistant", fromlist=["TOOLS"]).TOOLS)


def test_import_guests_and_dashboard(tmp_path):
    n8n = FakeN8n()
    with _client(tmp_path, n8n) as c:
        before = c.get("/api/guests", headers=ADMIN).json()
        existing = before[0]["fullName"]
        rows = [
            {"fullName": existing, "email": "primo@example.org", "category": "Famigliari"},   # abbinato per nome: aggiornato
            {"fullName": "Nuova Importata", "email": "Nuova@Example.org", "phone": "333", "guestsCount": 3},  # creata
            {"fullName": "Senza Email"},                                                      # creata, nessun invito
            {"fullName": "", "email": "x@example.org"},                                        # saltata
        ]
        assert c.post("/api/guests/import", json={"rows": rows}).status_code == 403
        # anteprima: nessuna scrittura
        r = c.post("/api/guests/import", json={"rows": rows, "sendLinks": True, "dryRun": True}, headers=ADMIN)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["dryRun"] and d["created"] == 2 and d["updated"] == 1 and d["skipped"] == 1 and d["invited"] == 0
        assert [x["action"] for x in d["rows"]] == ["updated", "created", "created", "skipped"]
        assert d["rows"][0]["changes"] == ["category", "email"] and d["rows"][1]["willInvite"] and not d["rows"][2]["willInvite"]
        assert len(c.get("/api/guests", headers=ADMIN).json()) == len(before) and _mails(n8n) == []
        # import vero con invio dei link
        r = c.post("/api/guests/import", json={"rows": rows, "sendLinks": True}, headers=ADMIN)
        d = r.json()
        assert d["created"] == 2 and d["updated"] == 1 and d["invited"] == 2 and d["inviteFailed"] == 0
        after = {g["fullName"]: g for g in c.get("/api/guests", headers=ADMIN).json()}
        assert len(after) == len(before) + 2
        assert after[existing]["email"] == "primo@example.org" and after[existing]["category"] == "Famigliari"
        nuova = after["Nuova Importata"]
        assert nuova["email"] == "nuova@example.org" and nuova["guestsCount"] == 3 and nuova["rsvpStatus"] == "PENDING" and nuova["hasProfile"]
        mails = _mails(n8n)
        assert sorted(m["to"] for m in mails) == ["nuova@example.org", "primo@example.org"] and all(m["kind"] == "access_link" for m in mails)
        assert "guest.imported" in n8n.events
        # secondo import identico: nessuna modifica
        d = c.post("/api/guests/import", json={"rows": rows}, headers=ADMIN).json()
        assert d["created"] == 0 and d["updated"] == 0 and d["unchanged"] == 3

        # cruscotto organizzatori
        assert c.get("/api/admin/dashboard").status_code == 403
        dash = c.get("/api/admin/dashboard", headers=ADMIN).json()
        g = dash["guests"]
        assert g["total"] == len(before) + 2 and g["withEmail"] >= 2 and g["withProfile"] >= 2 and g["pending"] >= 2
        assert dash["bus"]["maxSeats"] == 54 and "waitlistBookings" in dash["bus"]
        assert dash["mail"]["enabled"] is True and dash["mail"]["counts"].get("DELIVERED") == 2 and dash["mail"]["failed"] == []
        assert dash["pool"]["contributions"] >= 1 and [x["id"] for x in dash["pool"]["collectors"]] == ["roberto", "aki"]
        assert dash["assistant"] == {"configured": True, "avatars": 9, "ready": 1}
        assert dash["wishes"] >= 1 and dash["photos"] >= 1 and "menuDeadline" in dash["deadlines"]
        keys = [t["key"] for t in dash["todo"]]
        assert "pending" in keys and "pool" in keys and all("text" in t for t in dash["todo"])
