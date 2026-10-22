"""Prenotazioni navetta senza invitato: blocco della prenotazione anonima, riconciliazione dal cruscotto
(collega / crea invitato) e "È la mia" dal profilo."""
import re

from conftest import ADMIN, make_client
from test_automation import N8N_URL, FakeN8n


def _mails(n8n):
    return [n8n.payload(i)["data"] for i, e in enumerate(n8n.events) if e == "mail.send"]


def test_orphan_bookings_link_create_and_claim(tmp_path):
    n8n = FakeN8n()
    with make_client(tmp_path, demo=False, webhook_url=N8N_URL, webhook_secret="shh", webhook_handler=n8n) as c:
        c.app.state.mailer.sync = True
        # senza token: niente prenotazione anonima (il messaggio rimanda al profilo)
        r = c.post("/api/bus/bookings", json={"passengerName": "Chiunque", "seatsCount": 1}, headers={"X-Client-Id": "tel"})
        assert r.status_code == 403 and "profilo" in r.json()["detail"]
        # gli organizzatori inseriscono a mano (chi telefona): nascono senza invitato
        pina = c.post("/api/bus/bookings", json={"passengerName": "zia PINA", "seatsCount": 2, "contactPhone": "333 1"}, headers=ADMIN).json()
        mario = c.post("/api/bus/bookings", json={"passengerName": "Rossi Màrio", "seatsCount": 1}, headers=ADMIN).json()
        nuovo = c.post("/api/bus/bookings", json={"passengerName": "Persona Nuova", "seatsCount": 3, "contactPhone": "333 9"}, headers=ADMIN).json()
        dash = c.get("/api/admin/dashboard", headers=ADMIN).json()
        assert dash["bus"]["withoutGuest"] == 3 and any(t["key"] == "bus_orphans" for t in dash["todo"])
        # invitati esistenti (Zia Pina si è fatta il profilo con l'email)
        c.post("/api/profile/request-link", json={"email": "pina@example.org", "fullName": "Zia Pina"})
        token = re.search(r"#profilo=([A-Za-z0-9_-]+)", _mails(n8n)[-1]["text"]).group(1)
        g_mario = c.post("/api/guests", json={"fullName": "Mario Rossi"}, headers=ADMIN).json()
        c.post("/api/guests", json={"fullName": "Luca Rossi"}, headers=ADMIN)
        # elenco con i suggerimenti: stesso nome (2) prima di una parola in comune (1); mai collegamento automatico
        orphans = {o["passengerName"]: o for o in c.get("/api/admin/bus/orphans", headers=ADMIN).json()}
        assert set(orphans) == {"zia PINA", "Rossi Màrio", "Persona Nuova"}
        assert [s["fullName"] for s in orphans["Rossi Màrio"]["suggestions"]][:2] == ["Mario Rossi", "Luca Rossi"]
        assert orphans["Rossi Màrio"]["suggestions"][0]["score"] == 2 and orphans["Rossi Màrio"]["suggestions"][1]["score"] == 1
        assert orphans["Persona Nuova"]["suggestions"] == []
        assert c.get("/api/admin/bus/orphans").status_code == 403
        # collega (tocco di conferma dell'organizzatore): l'invitato senza email non riceve nulla
        r = c.post(f"/api/admin/bus/{mario['id']}/link", json={"guestId": g_mario["id"]}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["booking"]["guestId"] == g_mario["id"] and r.json()["mail"] is None
        assert c.post(f"/api/admin/bus/{mario['id']}/link", json={"guestId": g_mario["id"]}, headers=ADMIN).status_code == 409  # già legata
        assert c.post(f"/api/admin/bus/{pina['id']}/link", json={"guestId": g_mario["id"]}, headers=ADMIN).status_code == 409  # Mario ha già la sua
        assert c.post(f"/api/admin/bus/{pina['id']}/link", json={"guestId": 99999}, headers=ADMIN).status_code == 404
        # crea l'invitato dalla prenotazione: in attesa, con telefono e persone = posti
        r = c.post(f"/api/admin/bus/{nuovo['id']}/create-guest", headers=ADMIN)
        assert r.status_code == 201
        g = r.json()["guest"]
        assert g["fullName"] == "Persona Nuova" and g["rsvpStatus"] == "PENDING" and g["guestsCount"] == 3 and g["phone"] == "333 9"
        assert r.json()["booking"]["guestId"] == g["id"]
        assert c.post(f"/api/admin/bus/{nuovo['id']}/create-guest", headers=ADMIN).status_code == 409
        # "È la mia" dal profilo: Zia Pina trova la prenotazione a suo nome
        H = {"X-Guest-Token": token}
        prof = c.get("/api/profile", headers=H).json()
        assert prof["busBooking"] is None and prof["unclaimedBooking"]["id"] == pina["id"]
        assert c.post("/api/profile/bus/claim", json={"bookingId": mario["id"]}, headers=H).status_code == 404  # non è la sua
        r = c.post("/api/profile/bus/claim", json={"bookingId": pina["id"]}, headers=H)
        assert r.status_code == 200 and r.json()["busBooking"]["id"] == pina["id"] and r.json()["unclaimedBooking"] is None
        assert r.json()["mail"]["kind"] == "bus_confirmation" and _mails(n8n)[-1]["to"] == "pina@example.org"
        assert c.post("/api/profile/bus/claim", json={"bookingId": pina["id"]}, headers=H).status_code == 404  # già collegata
        # tutto riconciliato
        assert c.get("/api/admin/bus/orphans", headers=ADMIN).json() == []
        assert c.get("/api/admin/dashboard", headers=ADMIN).json()["bus"]["withoutGuest"] == 0
        # CSV: colonna Invitato
        lines = c.get("/api/export/bus.csv", headers=ADMIN).text.lstrip("\ufeff").splitlines()
        assert lines[0].split(";")[8] == "Invitato" and all(l.split(";")[8] == "Sì" for l in lines[1:-2])
