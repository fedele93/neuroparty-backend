import io
import json
import re

from PIL import Image

from conftest import ADMIN, SEED, TREASURER, guest_headers, make_client


def test_health_and_event(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    ev = client.get("/api/event").json()
    assert len(ev["graduates"]) == 9
    assert ev["meta"]["maxBusSeats"] == 40
    assert len(ev["program"]["timeline"]) == 5
    assert ev["giftCollector"]["name"] == "Dott. Paolo Roberto"  # primo cassiere (compatibilità Android)
    assert [c["id"] for c in ev["giftCollectors"]] == ["roberto", "aki"]
    assert ev["giftCollectors"][1]["name"] == "Dott. Akilesh Bhunjun" and ev["giftCollectors"][1]["paymentMethods"] == ["IBAN", "PayPal", "Contanti"]
    roberto = ev["giftCollectors"][0]
    assert roberto["iban"].startswith("IT89") and roberto["paypalMeUrl"] == "" and roberto["paypalContact"] == "+39 348 276 7437"


def test_demo_seed_matches_json(client):
    snap = client.get("/api/snapshot", headers=ADMIN).json()
    assert len(snap["guests"]) == 7
    assert len(snap["busBookings"]) == 3
    assert len(snap["wishes"]) == 10
    assert len(snap["photos"]) == 3
    assert len(snap["giftTargets"]) == 9
    assert "giftContributions" not in snap  # le quote uniche non sono pubbliche
    assert snap["event"]["giftCollector"]["ibanHolder"] == "Paolo Roberto"
    assert len(snap["notifications"]) == 4
    assert snap["version"] >= 1
    # le quote demo esistono ma le vede solo il cassiere
    pool = client.get("/api/gifts/pool", headers=TREASURER).json()
    assert pool["summary"]["contributions"] == 3 and pool["summary"]["received"] == 1
    assert pool["summary"]["totalAmount"] == 180 + 150 + 90


def test_empty_seed_keeps_only_gift_targets(empty_client):
    snap = empty_client.get("/api/snapshot").json()
    assert snap["guests"] == [] and snap["wishes"] == [] and snap["notifications"] == []
    assert len(snap["giftTargets"]) == 9
    assert empty_client.get("/api/gifts/pool", headers=TREASURER).json()["contributions"] == []


def test_guest_crud_and_ownership(client):
    v0 = client.get("/api/state").json()["version"]
    r = client.post(
        "/api/guests",
        json={"fullName": "  Mario Rossi ", "category": "Amici", "rsvpStatus": "PENDING", "guestsCount": 2},
        headers={"X-Client-Id": "dev-A"},
    )
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["fullName"] == "Mario Rossi" and g["ownerClientId"] == "dev-A"
    assert client.get("/api/state").json()["version"] == v0 + 1

    # chiunque può cambiare lo stato RSVP
    r = client.put(f"/api/guests/{g['id']}", json={"rsvpStatus": "CONFIRMED"}, headers={"X-Client-Id": "dev-B"})
    assert r.status_code == 200 and r.json()["rsvpStatus"] == "CONFIRMED"
    # ma non gli altri campi
    r = client.put(f"/api/guests/{g['id']}", json={"fullName": "Hacker"}, headers={"X-Client-Id": "dev-B"})
    assert r.status_code == 403
    # il creatore sì
    r = client.put(f"/api/guests/{g['id']}", json={"dietaryNotes": "Vegano"}, headers={"X-Client-Id": "dev-A"})
    assert r.status_code == 200 and r.json()["dietaryNotes"] == "Vegano"

    # cancellazione: negata a estranei, permessa al creatore e all'organizzatore
    assert client.delete(f"/api/guests/{g['id']}", headers={"X-Client-Id": "dev-B"}).status_code == 403
    assert client.delete(f"/api/guests/{g['id']}", headers={"X-Client-Id": "dev-A"}).status_code == 204
    assert client.delete(f"/api/guests/{g['id']}").status_code == 404

    seeded = client.get("/api/guests", headers=ADMIN).json()[0]  # record senza owner: solo l'organizzatore
    assert client.delete(f"/api/guests/{seeded['id']}", headers={"X-Client-Id": "dev-A"}).status_code == 403
    assert client.delete(f"/api/guests/{seeded['id']}", headers=ADMIN).status_code == 204


def test_guest_validation(client):
    assert client.post("/api/guests", json={"fullName": "   "}).status_code == 422
    assert client.post("/api/guests", json={"fullName": "X", "rsvpStatus": "MAYBE"}).status_code == 422


def test_bus_capacity(client):
    s = client.get("/api/bus/summary").json()
    assert s == {**s, "maxSeats": 40, "bookedSeats": 10, "availableSeats": 30, "waitlistSeats": 0, "busDeadline": "2026-11-11"}
    # senza profilo né token organizzatore non si prenota: la navetta si prenota dal profilo (niente nomi fantasma)
    r = client.post("/api/bus/bookings", json={"passengerName": "Anonimo", "seatsCount": 1})
    assert r.status_code == 403 and "profilo" in r.json()["detail"]
    r = client.post("/api/bus/bookings", json={"passengerName": "Gruppo", "seatsCount": 31}, headers=ADMIN)
    assert r.status_code == 409
    # con allowWaitlist si finisce in lista d'attesa invece dell'errore; i posti confermati non cambiano
    w = client.post("/api/bus/bookings", json={"passengerName": "Attesa", "seatsCount": 31, "allowWaitlist": True}, headers={**ADMIN, "X-Client-Id": "w"})
    assert w.status_code == 201 and w.json()["status"] == "WAITLIST" and w.json()["code"].startswith("NP-")
    assert client.get("/api/bus/summary").json()["waitlistSeats"] == 31 and client.get("/api/bus/summary").json()["bookedSeats"] == 10
    assert client.delete(f"/api/bus/bookings/{w.json()['id']}", headers={"X-Client-Id": "w"}).status_code == 204
    r = client.post("/api/bus/bookings", json={"passengerName": "Gruppo", "seatsCount": 30}, headers={**ADMIN, "X-Client-Id": "c1"})
    assert r.status_code == 201
    assert client.get("/api/bus/summary").json()["availableSeats"] == 0
    assert client.post("/api/bus/bookings", json={"passengerName": "Tardo", "seatsCount": 1}, headers=ADMIN).status_code == 409
    bid = r.json()["id"]
    assert client.delete(f"/api/bus/bookings/{bid}").status_code == 403
    assert client.delete(f"/api/bus/bookings/{bid}", headers={"X-Client-Id": "c1"}).status_code == 204
    assert client.get("/api/bus/summary").json()["availableSeats"] == 30


def test_wishes(client):
    r = client.post("/api/wishes", json={"authorName": "", "message": " Auguri! ", "targetGraduate": "Fedele Luisi"}, headers=ADMIN)
    assert r.status_code == 201
    w = r.json()
    assert w["authorName"] == "Amico/a" and w["message"] == "Auguri!" and w["heartCount"] == 1
    assert client.post(f"/api/wishes/{w['id']}/heart", headers=ADMIN).json()["heartCount"] == 2
    assert client.post("/api/wishes", json={"message": "   "}, headers=ADMIN).status_code == 422
    assert client.get("/api/wishes", headers=ADMIN).json()[0]["id"] == w["id"]  # ordinamento: più recente per primo
    assert client.delete(f"/api/wishes/{w['id']}").status_code == 403
    assert client.delete(f"/api/wishes/{w['id']}", headers=ADMIN).status_code == 204


def test_wishes_and_photos_only_for_registered_guests_and_roles(tmp_path):
    with make_client(tmp_path, graduate_tokens={"luisi": "grad-luisi"}) as c:
        anna = guest_headers(c, "Anna Bianchi")
        nope = guest_headers(c, "Ugo Assente", rsvp_status="DECLINED")  # anche chi non viene
        # senza registrazione: niente lettura, niente pubblicazione, niente cuori/like; snapshot senza foto e auguri
        for h in ({}, {"X-Client-Id": "tel"}, {"X-Guest-Token": "sbagliato"}, {"X-Graduate-Token": "sbagliato"}):
            assert c.get("/api/wishes", headers=h).status_code == 401, h
            assert c.get("/api/photos", headers=h).status_code == 401, h
            assert c.post("/api/wishes", json={"message": "ciao"}, headers=h).status_code == 401, h
            assert c.post("/api/wishes/1/heart", headers=h).status_code == 401, h
            assert c.post("/api/photos/1/like", headers=h).status_code == 401, h
            snap = c.get("/api/snapshot", headers=h).json()
            assert snap["wishes"] == [] and snap["photos"] == [] and snap["canSeeMemories"] is False, h
            assert c.get("/api/state", headers=h).json()["canSeeMemories"] is False, h
        assert "link personale" in c.get("/api/wishes").json()["detail"]
        # invitati registrati (qualunque RSVP) e ruoli: vedono e pubblicano
        for h in (anna, nope, ADMIN, TREASURER, {"X-Graduate-Token": "grad-luisi"}):
            assert len(c.get("/api/wishes", headers=h).json()) >= 10, h
            assert len(c.get("/api/photos", headers=h).json()) == 3, h
            snap = c.get("/api/snapshot", headers=h).json()
            assert snap["canSeeMemories"] is True and len(snap["wishes"]) >= 10 and len(snap["photos"]) == 3, h
            assert c.get("/api/state", headers=h).json()["canSeeMemories"] is True, h
        # l'invitato firma col nome del profilo, non con quello che scrive
        w = c.post("/api/wishes", json={"authorName": "Il Rettore", "message": "Bravi!"}, headers=anna).json()
        assert w["authorName"] == "Anna Bianchi"
        assert c.post(f"/api/wishes/{w['id']}/heart", headers=nope).json()["heartCount"] == 2
        assert c.post("/api/wishes", json={"authorName": "Zia", "message": "Evviva"}, headers=TREASURER).json()["authorName"] == "Zia"
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (0, 90, 200)).save(buf, "PNG")
        p = c.post("/api/photos", files={"file": ("a.png", buf.getvalue(), "image/png")}, data={"authorName": "Altro nome"}, headers=anna)
        assert p.status_code == 201 and p.json()["authorName"] == "Anna Bianchi"
        assert c.post("/api/photos", files={"file": ("a.png", buf.getvalue(), "image/png")}).status_code == 401


def test_gift_targets_hide_amounts_and_pool_is_private(client):
    targets = client.get("/api/gifts/targets").json()
    assert [t["id"] for t in targets][:2] == ["luisi", "carlone"] and "gruppo" not in [t["id"] for t in targets]
    for t in targets:
        assert "collectedAmount" not in t and "targetAmount" not in t
        assert t["iban"] and "paypalMeUrl" in t and "satispayUrl" in t and "bancomatPay" in t  # PayPal, Satispay e BANCOMAT Pay facoltativi
    assert targets[0]["bancomatPay"] == "3469556702" and targets[1]["bancomatPay"] == ""  # BANCOMAT Pay solo per chi lo usa
    assert next(t for t in targets if t["id"] == "prezioso")["bancomatPay"] == "3889861472"
    cusmai = next(t for t in targets if t["id"] == "cusmai")
    assert cusmai["paypalEmail"] == "francescocusmai96@gmail.com" and cusmai["paypalMeUrl"] == ""  # PayPal via email
    collector = client.get("/api/gifts/collector").json()
    assert collector["name"] == "Dott. Paolo Roberto" and collector["iban"]
    assert "Satispay" not in collector["paymentMethods"]
    assert [c["name"] for c in client.get("/api/gifts/collectors").json()] == ["Dott. Paolo Roberto", "Dott. Akilesh Bhunjun"]
    # elenco e stato riservati al cassiere/organizzatori
    assert client.get("/api/gifts/pool").status_code == 403
    assert client.get("/api/gifts/pool", headers={"X-Treasurer-Token": "sbagliato"}).status_code == 403
    assert client.patch("/api/gifts/pool/1/status", json={"status": "RECEIVED"}).status_code == 403
    assert client.get("/api/export/gift-pool.csv").status_code == 403


def test_pool_equal_split(client):
    v0 = client.get("/api/state").json()["version"]
    r = client.post(
        "/api/gifts/pool",
        json={"donorName": "  Zio Peppe ", "totalAmount": 100, "paymentMethod": "PayPal", "note": "Auguri!"},
        headers={"X-Client-Id": "dev-a"},
    )
    assert r.status_code == 201
    c = r.json()
    assert c["donorName"] == "Zio Peppe" and c["status"] == "PENDING" and c["splitMode"] == "EQUAL"
    assert c["totalAmount"] == 100 and len(c["allocations"]) == 9
    # 100 € / 9 = 11,11 con 1 centesimo di resto sul primo; la somma torna esatta
    assert c["allocations"][0]["amount"] == 11.12 and c["allocations"][1]["amount"] == 11.11
    assert round(sum(a["amount"] for a in c["allocations"]), 2) == 100
    assert c["allocations"][0] == {"graduateId": "luisi", "graduateName": "Dott. Fedele Luisi", "amount": 11.12}
    assert c["collectorId"] == "roberto" and c["collectorName"] == "Dott. Paolo Roberto"  # cassiere di default
    assert client.get("/api/state").json()["version"] == v0 + 1

    # quota versata al secondo cassiere
    r = client.post("/api/gifts/pool", json={"donorName": "Cugina", "totalAmount": 45, "collectorId": "aki", "paymentMethod": "PayPal"})
    assert r.status_code == 201 and r.json()["collectorName"] == "Dott. Akilesh Bhunjun"
    assert client.post("/api/gifts/pool", json={"donorName": "X", "totalAmount": 45, "collectorId": "nessuno"}).status_code == 404

    # solo alcuni neo-specialisti (es. escludo me stesso)
    r = client.post("/api/gifts/pool", json={"donorName": "Fedele", "totalAmount": 80, "graduateIds": ["carlone", "totaro"]})
    assert r.status_code == 201
    assert [(a["graduateId"], a["amount"]) for a in r.json()["allocations"]] == [("carlone", 40), ("totaro", 40)]


def test_pool_custom_split(client):
    r = client.post(
        "/api/gifts/pool",
        json={
            "donorName": "Colleghi", "splitMode": "CUSTOM", "paymentMethod": "Contanti",
            "allocations": [{"graduateId": "prezioso", "amount": 30.5}, {"graduateId": "ruta", "amount": 19.5}],
        },
    )
    assert r.status_code == 201
    c = r.json()
    assert c["totalAmount"] == 50 and c["paymentMethod"] == "Contanti" and c["splitMode"] == "CUSTOM"
    assert [a["graduateName"] for a in c["allocations"]] == ["Dott. Roberto Spiridione Prezioso", "Dott.ssa Giorgia Ruta"]


def test_pool_validation(client):
    post = lambda body: client.post("/api/gifts/pool", json=body).status_code  # noqa: E731
    assert post({"donorName": "", "totalAmount": 10}) == 422  # nome obbligatorio
    assert post({"donorName": "A", "totalAmount": 10, "paymentMethod": "Satispay"}) == 422  # non ammesso per la cassa
    assert post({"donorName": "A"}) == 422  # manca l'importo
    assert post({"donorName": "A", "totalAmount": 0}) == 422
    assert post({"donorName": "A", "totalAmount": 0.05}) == 422  # non divisibile fra 9
    assert post({"donorName": "A", "totalAmount": 10, "graduateIds": ["nessuno"]}) == 404
    assert post({"donorName": "A", "totalAmount": 10, "graduateIds": ["luisi", "luisi"]}) == 422
    assert post({"donorName": "A", "splitMode": "CUSTOM"}) == 422
    assert post({"donorName": "A", "splitMode": "CUSTOM", "allocations": [{"graduateId": "luisi", "amount": 5}], "totalAmount": 6}) == 422
    assert post({"donorName": "A", "splitMode": "CUSTOM", "allocations": [{"graduateId": "x", "amount": 5}]}) == 404


def test_pool_mine_and_delete_ownership(client):
    a, b = {"X-Client-Id": "dev-a"}, {"X-Client-Id": "dev-b"}
    mine = client.post("/api/gifts/pool", json={"donorName": "Anna", "totalAmount": 90}, headers=a).json()
    assert [c["id"] for c in client.get("/api/gifts/pool/mine", headers=a).json()] == [mine["id"]]
    assert client.get("/api/gifts/pool/mine", headers=b).json() == []
    assert client.get("/api/gifts/pool/mine").json() == []
    # un altro dispositivo non può cancellarla; il proprietario sì finché è in attesa
    assert client.delete(f"/api/gifts/pool/{mine['id']}", headers=b).status_code == 403
    assert client.delete(f"/api/gifts/pool/{mine['id']}").status_code == 403
    client.patch(f"/api/gifts/pool/{mine['id']}/status", json={"status": "RECEIVED"}, headers=TREASURER)
    assert client.delete(f"/api/gifts/pool/{mine['id']}", headers=a).status_code == 409  # già ricevuta
    client.patch(f"/api/gifts/pool/{mine['id']}/status", json={"status": "PENDING"}, headers=TREASURER)
    assert client.delete(f"/api/gifts/pool/{mine['id']}", headers=a).status_code == 204
    assert client.delete(f"/api/gifts/pool/{mine['id']}", headers=a).status_code == 404
    # il cassiere/organizzatore cancella sempre
    other = client.post("/api/gifts/pool", json={"donorName": "Bruno", "totalAmount": 9}, headers=b).json()
    assert client.delete(f"/api/gifts/pool/{other['id']}", headers=TREASURER).status_code == 204


def test_pool_treasurer_dashboard_and_csv(tmp_path):
    with make_client(tmp_path, treasurer_token="cassa-test", automation_token="n8n-test") as c:
        assert c.get("/api/state").json()["treasurerEnabled"] is True
        board = c.get("/api/gifts/pool", headers=TREASURER).json()
        s = board["summary"]
        assert s["contributions"] == 3 and s["pending"] == 2 and s["receivedAmount"] == 180
        luisi = next(g for g in s["byGraduate"] if g["graduateId"] == "luisi")
        assert luisi == {"graduateId": "luisi", "graduateName": "Dott. Fedele Luisi", "amount": 80, "receivedAmount": 20, "contributions": 3}
        assert {m["paymentMethod"]: m["amount"] for m in s["byMethod"]} == {"IBAN": 180, "PayPal": 240}
        assert board["contributions"][0]["donorName"] == "Amici del corso"  # la più recente per prima
        # sezioni per cassiere: Paolo ha 2 quote (330 €), Aki 1 (90 € via PayPal)
        assert [x["id"] for x in board["collectors"]] == ["roberto", "aki"]
        by_c = {x["collectorId"]: x for x in s["byCollector"]}
        assert by_c["roberto"]["totalAmount"] == 330 and by_c["aki"]["totalAmount"] == 90 and by_c["aki"]["contributions"] == 1
        assert next(g for g in by_c["aki"]["byGraduate"] if g["graduateId"] == "luisi")["amount"] == 10

        # il cassiere segna come ricevuta la quota PayPal
        paypal = next(k for k in board["contributions"] if k["paymentMethod"] == "PayPal" and k["collectorId"] == "roberto")
        v0 = c.get("/api/state").json()["version"]
        r = c.patch(f"/api/gifts/pool/{paypal['id']}/status", json={"status": "RECEIVED"}, headers=TREASURER)
        assert r.status_code == 200 and r.json()["status"] == "RECEIVED" and r.json()["receivedAt"]
        assert c.get("/api/state").json()["version"] == v0 + 1
        assert c.patch(f"/api/gifts/pool/{paypal['id']}/status", json={"status": "RECEIVED"}, headers=TREASURER).status_code == 200
        assert c.get("/api/state").json()["version"] == v0 + 1  # nessun cambiamento: versione ferma
        assert c.patch("/api/gifts/pool/9999/status", json={"status": "RECEIVED"}, headers=TREASURER).status_code == 404
        assert c.get("/api/gifts/pool", headers=TREASURER).json()["summary"]["receivedAmount"] == 330
        # il token cassiere non dà i poteri dell'organizzatore
        assert c.post("/api/notifications", json={"title": "a", "message": "b"}, headers=TREASURER).status_code == 403
        assert c.get("/api/export/guests.csv", headers=TREASURER).status_code == 403

        # CSV: cassiere in seconda colonna, una colonna per neo-specialista, totali per cassiere e complessivi; virgola decimale
        for headers in (TREASURER, {"X-Automation-Token": "n8n-test"}):
            r = c.get("/api/export/gift-pool.csv", headers=headers)
            assert r.status_code == 200
        assert 'filename="quote-uniche.csv"' in r.headers["content-disposition"]
        lines = r.text.lstrip("\ufeff").splitlines()
        head = lines[0].split(";")
        assert head[:12] == ["Data", "Cassiere", "Donatore", "Contatto", "Email", "Metodo", "Ripartizione", "Totale", "Stato", "Ricevuta il", "Note", "Appunti cassiere"]
        assert head[12:] == [t["name"] for t in c.get("/api/gifts/targets").json()]
        assert len(lines) == 1 + 3 + 2 + 2  # intestazione, 3 quote, 2 totali per cassiere, totale e ricevuto
        rows = {ln.split(";")[2]: ln.split(";") for ln in lines[1:4]}
        assert rows["Colleghi Reparto Stroke"][1] == "Dott. Paolo Roberto"
        assert rows["Colleghi Reparto Stroke"][5:9] == ["PayPal", "Personalizzata", "150,00", "Ricevuta"]
        assert rows["Colleghi Reparto Stroke"][12] == "50,00" and rows["Colleghi Reparto Stroke"][13] == ""  # luisi, carlone
        assert rows["Amici del corso"][1] == "Dott. Akilesh Bhunjun" and rows["Amici del corso"][7:9] == ["90,00", "In attesa"] and rows["Amici del corso"][12] == "10,00"
        assert lines[4].split(";")[0] == "TOTALE DOTT. PAOLO ROBERTO" and lines[4].split(";")[7] == "330,00"
        assert lines[5].split(";")[0] == "TOTALE DOTT. AKILESH BHUNJUN" and lines[5].split(";")[7] == "90,00" and lines[5].split(";")[12] == "10,00"
        assert lines[6].split(";")[:8] == ["TOTALE", "", "", "", "", "", "", "420,00"] and lines[6].split(";")[12] == "80,00"
        assert lines[7].split(";")[:8] == ["DI CUI RICEVUTO", "", "", "", "", "", "", "330,00"] and lines[7].split(";")[12] == "70,00"

    with make_client(tmp_path / "b", admin_token="", treasurer_token="") as c:
        assert c.get("/api/gifts/pool", headers=TREASURER).status_code == 503
        assert c.get("/api/state").json()["treasurerEnabled"] is False


def test_per_collector_treasurer_token_sees_only_own_quotas(tmp_path, monkeypatch):
    """TREASURER_TOKEN_AKI: Aki vede, segna e scarica solo le quote versate a lui."""
    from app.config import Settings

    monkeypatch.setenv("TREASURER_TOKEN_AKI", "aki-secret")
    assert Settings().treasurer_tokens == {"aki": "aki-secret"}
    AKI = {"X-Treasurer-Token": "aki-secret"}
    with make_client(tmp_path) as c:  # TREASURER (globale) vede tutto, AKI solo le sue
        c.app.state.settings.treasurer_tokens = {"aki": "aki-secret"}
        assert c.get("/api/state").json()["treasurerEnabled"] is True
        board = c.get("/api/gifts/pool", headers=AKI).json()
        assert [x["id"] for x in board["collectors"]] == ["aki"]
        assert [k["donorName"] for k in board["contributions"]] == ["Amici del corso"]
        assert board["summary"]["totalAmount"] == 90 and [x["collectorId"] for x in board["summary"]["byCollector"]] == ["aki"]
        paolo_quota = next(k for k in c.get("/api/gifts/pool", headers=TREASURER).json()["contributions"] if k["collectorId"] == "roberto")
        assert c.patch(f"/api/gifts/pool/{paolo_quota['id']}/status", json={"status": "RECEIVED"}, headers=AKI).status_code == 403
        assert c.delete(f"/api/gifts/pool/{paolo_quota['id']}", headers=AKI).status_code == 403
        mine = board["contributions"][0]
        assert c.patch(f"/api/gifts/pool/{mine['id']}/status", json={"status": "RECEIVED"}, headers=AKI).status_code == 200
        lines = c.get("/api/export/gift-pool.csv", headers=AKI).text.lstrip("\ufeff").splitlines()
        assert len(lines) == 1 + 1 + 2 and all("Dott. Akilesh Bhunjun" in ln or ln.startswith(("Data;", "TOTALE", "DI CUI")) for ln in lines)
        assert c.get("/api/gifts/pool", headers={"X-Treasurer-Token": "sbagliato"}).status_code == 403
        assert c.delete(f"/api/gifts/pool/{mine['id']}", headers=AKI).status_code == 204  # il suo cassiere può sempre


def test_legacy_pool_rows_belong_to_first_collector(tmp_path):
    """Quote registrate prima dei cassieri multipli (collector_id vuoto) contano per il primo cassiere."""
    import sqlite3

    with make_client(tmp_path, demo=False) as c:
        c.post("/api/gifts/pool", json={"donorName": "Vecchia", "totalAmount": 9})
    con = sqlite3.connect(tmp_path / "data" / "neuroparty.db")
    con.execute("UPDATE gift_pool_contributions SET collector_id = '', collector_name = ''")
    con.commit(); con.close()
    with make_client(tmp_path, demo=False) as c:
        board = c.get("/api/gifts/pool", headers=TREASURER).json()
        assert board["contributions"][0]["collectorId"] == "roberto" and board["contributions"][0]["collectorName"] == "Dott. Paolo Roberto"
        assert next(x for x in board["summary"]["byCollector"] if x["collectorId"] == "roberto")["contributions"] == 1


def test_photo_upload_resizes_and_serves(client):
    img = Image.new("RGB", (3000, 2000), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = client.post(
        "/api/photos",
        files={"file": ("big.png", buf.getvalue(), "image/png")},
        data={"authorName": "Chiara", "caption": "Prova"},
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["imageUri"].startswith("https://festa.example.org/uploads/") and p["imageUri"].endswith(".jpg")
    path = p["imageUri"].replace("https://festa.example.org", "")
    served = client.get(path)
    assert served.status_code == 200
    w, h = Image.open(io.BytesIO(served.content)).size
    assert max(w, h) == 1600
    assert client.post(f"/api/photos/{p['id']}/like", headers=ADMIN).json()["likesCount"] == 2
    bad = client.post("/api/photos", files={"file": ("x.txt", b"non immagine", "text/plain")}, headers=ADMIN)
    assert bad.status_code == 400


def test_notifications_require_admin_and_polling(client):
    body = {"title": "🚌 Partenza", "message": "Tra 15 minuti", "category": "Navetta"}
    assert client.post("/api/notifications", json=body).status_code == 403
    assert client.post("/api/notifications", json=body, headers={"X-Admin-Token": "sbagliato"}).status_code == 403
    r = client.post("/api/notifications", json=body, headers=ADMIN)
    assert r.status_code == 201
    n = r.json()
    latest = client.get("/api/notifications").json()
    assert latest[0]["id"] == n["id"]
    since = client.get("/api/notifications", params={"since": n["timestamp"] - 1}).json()
    assert [x["id"] for x in since] == [n["id"]]
    assert client.get("/api/notifications", params={"since": n["timestamp"]}).json() == []


def test_admin_disabled_when_token_missing(tmp_path):
    with make_client(tmp_path, admin_token="") as c:
        assert c.get("/api/state").json()["adminEnabled"] is False
        r = c.post("/api/notifications", json={"title": "a", "message": "b"}, headers={"X-Admin-Token": "x"})
        assert r.status_code == 503


def test_push_subscription_roundtrip(client):
    key = client.get("/api/push/vapid-public-key").json()["publicKey"]
    assert len(key) > 80 and "=" not in key
    sub = {"endpoint": "https://push.example.org/abc123", "keys": {"p256dh": "p", "auth": "a"}}
    assert client.post("/api/push/subscribe", json=sub, headers={"X-Client-Id": "dev-1"}).status_code == 201
    assert client.post("/api/push/subscribe", json=sub).json()["subscriptions"] == 1  # idempotente
    assert client.get("/api/state").json()["pushSubscriptions"] == 1
    assert client.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]}).json() == {"ok": True}
    assert client.get("/api/state").json()["pushSubscriptions"] == 0


def test_vapid_keys_persist_across_restarts(tmp_path):
    with make_client(tmp_path) as c1:
        k1 = c1.get("/api/push/vapid-public-key").json()["publicKey"]
    with make_client(tmp_path) as c2:
        k2 = c2.get("/api/push/vapid-public-key").json()["publicKey"]
        # anche il DB persiste (nessun doppio seed)
        assert len(c2.get("/api/guests", headers=ADMIN).json()) == 7
    assert k1 == k2


def test_new_gift_target_added_to_existing_database(tmp_path):
    """Un neo-specialista aggiunto a event-data.json dopo il primo avvio deve comparire
    fra i regali al riavvio, senza toccare le quote già raccolte dagli altri."""
    with open(SEED, encoding="utf-8") as f:
        data = json.load(f)
    reduced = dict(data, giftTargets=[t for t in data["giftTargets"] if t["id"] != "regina"])
    old_seed = tmp_path / "old-event-data.json"
    old_seed.write_text(json.dumps(reduced), encoding="utf-8")

    with make_client(tmp_path, seed_file=str(old_seed)) as c1:
        assert [t["id"] for t in c1.get("/api/gifts/targets").json()].count("regina") == 0
        c1.post("/api/gifts/pool", json={"donorName": "Zia", "totalAmount": 80})  # divisa fra gli 8 presenti
        v1 = c1.get("/api/state").json()["version"]

    with make_client(tmp_path) as c2:  # riavvio con il JSON completo (9 regali)
        targets = c2.get("/api/gifts/targets").json()
        assert next(t for t in targets if t["id"] == "regina")["name"] == "Dott. Donato Regina"
        assert targets[-1]["id"] == "regina"  # rispetta l'ordine del JSON
        assert c2.get("/api/state").json()["version"] == v1 + 1  # i client ricaricano
        assert len(c2.get("/api/guests", headers=ADMIN).json()) == 7  # nessun doppio seed demo
        # le quote registrate prima restano (con la ripartizione di allora)
        board = c2.get("/api/gifts/pool", headers=TREASURER).json()
        zia = next(k for k in board["contributions"] if k["donorName"] == "Zia")
        assert len(zia["allocations"]) == 8 and all(a["amount"] == 10 for a in zia["allocations"])


def test_gift_target_removed_from_json_disappears(tmp_path):
    """Un regalo tolto da event-data.json (es. il vecchio regalo comune) sparisce al riavvio;
    le quote uniche che lo includevano restano leggibili nel CSV con il nome salvato."""
    with open(SEED, encoding="utf-8") as f:
        data = json.load(f)
    extra = dict(data["giftTargets"][0], id="extra", name="Dott. Extra")
    bigger = tmp_path / "bigger-event-data.json"
    bigger.write_text(json.dumps(dict(data, giftTargets=data["giftTargets"] + [extra])), encoding="utf-8")

    with make_client(tmp_path, seed_file=str(bigger)) as c1:
        assert len(c1.get("/api/gifts/targets").json()) == 10
        c1.post("/api/gifts/pool", json={"donorName": "Nonna", "totalAmount": 15, "graduateIds": ["extra", "luisi"]})
        v1 = c1.get("/api/state").json()["version"]

    with make_client(tmp_path) as c2:
        assert len(c2.get("/api/gifts/targets").json()) == 9
        assert c2.get("/api/state").json()["version"] == v1 + 1
        csv_head = c2.get("/api/export/gift-pool.csv", headers=TREASURER).text.splitlines()[0]
        assert csv_head.endswith(";Dott. Extra")


def test_gift_texts_updated_only_with_flag(tmp_path):
    """Cambiare titolo/IBAN di un regalo nel JSON aggiorna il DB solo con GIFT_SYNC_UPDATE_TEXTS."""
    with open(SEED, encoding="utf-8") as f:
        data = json.load(f)
    for t in data["giftTargets"]:
        if t["id"] == "luisi":
            t["giftTitle"] = "Nuovo regalo di Fedele"
            t["iban"] = "IT00 A000 0000 0000 0000 0000 000"
    changed = tmp_path / "changed-event-data.json"
    changed.write_text(json.dumps(data), encoding="utf-8")

    with make_client(tmp_path) as c1:  # primo avvio con il seed originale
        v1 = c1.get("/api/state").json()["version"]

    with make_client(tmp_path, seed_file=str(changed)) as c2:  # flag spento: nessuna modifica
        luisi = next(t for t in c2.get("/api/gifts/targets").json() if t["id"] == "luisi")
        assert luisi["giftTitle"] != "Nuovo regalo di Fedele"
        assert c2.get("/api/state").json()["version"] == v1

    with make_client(tmp_path, seed_file=str(changed), update_gift_texts=True) as c3:
        luisi = next(t for t in c3.get("/api/gifts/targets").json() if t["id"] == "luisi")
        assert luisi["giftTitle"] == "Nuovo regalo di Fedele"
        assert luisi["iban"] == "IT00 A000 0000 0000 0000 0000 000"
        assert c3.get("/api/state").json()["version"] == v1 + 1

    with make_client(tmp_path, seed_file=str(changed), update_gift_texts=True) as c4:
        assert c4.get("/api/state").json()["version"] == v1 + 1  # niente da aggiornare: versione ferma


def test_schedule_placeholders_resolved_in_event(tmp_path):
    with open(SEED, encoding="utf-8") as f:
        data = json.load(f)
    assert "{partyTime" in json.dumps(data)  # il JSON usa i segnaposto

    with make_client(tmp_path) as c:
        ev = c.get("/api/event").json()
        assert ev["schedule"]["partyDate"] == "2026-11-13"
        assert ev["schedule"]["partyTime"] == "21:30" and ev["schedule"]["partyEndTime"] == "03:00"
        assert ev["schedule"]["ceremonyTime"] == "09:00"
        assert not re.search(r"\{\w+", json.dumps(ev))  # nessun segnaposto nei testi serviti
        festa = next(p for p in ev["mapPoints"] if p["id"] == "festa")
        assert festa["timeLabel"] == "Venerdì 13 novembre - dalle 21:30 alle 03:00"
        assert "Dalle ore 21:30 fino alle 03:00." in festa["description"]
        assert ev["program"]["timeline"][3]["time"] == "Ven 13 ore 21:30"
        seduta = next(p for p in ev["mapPoints"] if p["id"] == "seduta")
        assert seduta["timeLabel"] == "9 Novembre - ore 09:00"
        assert "Inizio alle ore 09:00." in ev["program"]["timeline"][0]["details"] and ev["program"]["timeline"][0]["time"] == "Ore 09:00"
        assert "video celebrativo" not in json.dumps(ev)

    # orari tolti/cambiati: le frasi si adattano (forma "da definire" e fine festa assente)
    data["schedule"]["partyTime"] = ""
    data["schedule"]["partyEndTime"] = ""
    data["schedule"]["busDepartureTime"] = "19:15"
    timed = tmp_path / "timed-event-data.json"
    timed.write_text(json.dumps(data), encoding="utf-8")
    with make_client(tmp_path / "b", seed_file=str(timed)) as c:
        ev = c.get("/api/event").json()
        festa = next(p for p in ev["mapPoints"] if p["id"] == "festa")
        assert festa["timeLabel"] == "Venerdì 13 novembre - ora da definire"
        assert "Orario da confermare." in festa["description"]
        assert ev["busSchedule"]["andata"]["timeLabel"] == "Ven 13 - ore 19:15"
        assert ev["program"]["timeline"][3]["time"] == "Ven 13"
        snap = c.get("/api/snapshot").json()
        assert snap["event"]["schedule"]["busDepartureTime"] == "19:15"


def test_calendar_ics(tmp_path):
    with make_client(tmp_path) as c:
        r = c.get("/api/event/calendar.ics")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/calendar")
        body = r.text
        assert body.startswith("BEGIN:VCALENDAR") and body.rstrip().endswith("END:VCALENDAR")
        assert body.count("BEGIN:VEVENT") == 2
        assert "DTSTART;TZID=Europe/Rome:20261109T090000" in body  # seduta alle 9:00, durata predefinita 3 h
        assert "DTSTART;TZID=Europe/Rome:20261113T213000" in body  # festa 21:30 -> 03:00 del giorno dopo
        assert "DTEND;TZID=Europe/Rome:20261114T030000" in body
        assert "LOCATION:Il Giardino dei Tempi - Orto Botanico\\, Via Giovanni Amendola 247\\, 70126 Bari" in body.replace("\r\n ", "")
        assert "URL:https://festa.example.org" in body

    with open(SEED, encoding="utf-8") as f:
        data = json.load(f)
    data["schedule"]["partyTime"] = ""
    data["schedule"]["partyEndTime"] = ""
    data["schedule"]["ceremonyTime"] = "9:30"
    timed = tmp_path / "timed-event-data.json"
    timed.write_text(json.dumps(data), encoding="utf-8")
    with make_client(tmp_path / "b", seed_file=str(timed)) as c:
        body = c.get("/api/event/calendar.ics").text
        assert "DTSTART;VALUE=DATE:20261113" in body  # festa senza orario: tutto il giorno
        assert "DTSTART;TZID=Europe/Rome:20261109T093000" in body  # seduta con orario, durata predefinita 3 h
        assert "DTEND;TZID=Europe/Rome:20261109T123000" in body


def test_guests_summary(client):
    s = client.get("/api/guests/summary", headers=ADMIN).json()
    assert s["confirmedGuests"] == 5 and s["covers"] == 10
    assert s["pendingGuests"] == 1 and s["pendingCovers"] == 2 and s["declinedGuests"] == 1
    cats = {c["category"]: c for c in s["byCategory"]}
    assert cats["Familiari"] == {"category": "Familiari", "guests": 2, "covers": 6}
    notes = {d["note"]: d for d in s["dietary"]}
    assert "Nessuna" not in notes and "Nessuna restrizione" not in notes
    assert notes["1 Celiaco (menu senza glutine)"]["guests"] == ["Matteo Moretti & Famiglia"]
    assert notes["Opzione Vegetariana"]["covers"] == 1


def test_csv_export_requires_admin(client):
    assert client.get("/api/export/guests.csv").status_code == 403
    r = client.get("/api/export/guests.csv", headers=ADMIN)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="invitati.csv"' in r.headers["content-disposition"]
    lines = r.text.lstrip("\ufeff").splitlines()
    assert lines[0] == "Nome;Categoria;Stato RSVP;Persone;Menu;Accompagnatori e menu;Esigenze alimentari;Email;Telefono;Contatto;Aggiornato il"
    assert len(lines) == 8  # intestazione + 7 invitati demo
    assert any(line.startswith("Dott. Luca Gatti;Colleghi Reparto;In attesa;2;;;") for line in lines)

    r = client.get("/api/export/bus.csv", headers=ADMIN)
    lines = r.text.lstrip("\ufeff").splitlines()
    assert lines[0].startswith("Passeggero;Posti;Stato;Codice;Fermata;Ritorno;Telefono;Email;Invitato;Salito")
    assert lines[-2].startswith("TOTALE POSTI CONFERMATI;10;") and lines[-1].startswith("IN LISTA D'ATTESA;0;")


def test_scheduled_notification_lifecycle(client):
    from app.scheduler import publish_due_notifications

    n0 = len(client.get("/api/notifications").json())
    v0 = client.get("/api/state").json()["version"]
    future = client.get("/api/state").json()["serverTime"] + 3_600_000
    body = {"title": "🚌 Navetta in partenza", "message": "Tra 15 minuti", "category": "Navetta", "sendAt": future}
    r = client.post("/api/notifications", json=body, headers=ADMIN)
    assert r.status_code == 201 and r.json()["scheduledAt"] == future
    nid = r.json()["id"]

    # invisibile agli invitati, nessun cambio di versione, visibile agli organizzatori
    assert len(client.get("/api/notifications").json()) == n0
    assert len(client.get("/api/snapshot").json()["notifications"]) == n0
    assert client.get("/api/state").json()["version"] == v0
    assert client.get("/api/notifications/scheduled").status_code == 403
    scheduled = client.get("/api/notifications/scheduled", headers=ADMIN).json()
    assert [x["id"] for x in scheduled] == [nid]

    # non ancora scaduta: lo scheduler non la tocca
    app = client.app
    assert publish_due_notifications(app.state.session_factory, app.state.push) == []
    # arrivata l'ora: pubblicata, versione incrementata, in cima alla cronologia
    published = publish_due_notifications(app.state.session_factory, app.state.push, now=future + 1)
    assert [x["id"] for x in published] == [nid] and published[0]["scheduledAt"] is None
    assert client.get("/api/notifications").json()[0]["id"] == nid
    assert client.get("/api/state").json()["version"] == v0 + 1
    assert client.get("/api/notifications/scheduled", headers=ADMIN).json() == []

    # sendAt nel passato = invio immediato; cancellazione riservata agli organizzatori
    r = client.post("/api/notifications", json={**body, "sendAt": 1}, headers=ADMIN)
    assert r.status_code == 201 and r.json()["scheduledAt"] is None
    assert client.delete(f"/api/notifications/{r.json()['id']}").status_code == 403
    assert client.delete(f"/api/notifications/{r.json()['id']}", headers=ADMIN).status_code == 204
    assert client.delete(f"/api/notifications/{r.json()['id']}", headers=ADMIN).status_code == 404


def test_schema_migration_adds_scheduled_at(tmp_path):
    """Un database creato prima delle notifiche programmate viene aggiornato all'avvio."""
    import sqlite3

    from app.db import ensure_schema, make_engine

    (tmp_path / "data").mkdir()
    path = tmp_path / "data" / "neuroparty.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE event_notifications (id INTEGER PRIMARY KEY, title VARCHAR(300), message TEXT, category VARCHAR(50), timestamp INTEGER)")
    con.execute("INSERT INTO event_notifications (title, message, category, timestamp) VALUES ('vecchia', 'm', 'Festa', 1)")
    # prima versione dei regali: importi sui destinatari e tabella delle quote per singolo regalo
    con.execute(
        "CREATE TABLE gift_targets (id VARCHAR(64) PRIMARY KEY, name VARCHAR(200), specialization VARCHAR(200), "
        "role_title VARCHAR(300), gift_title VARCHAR(300), gift_description TEXT, target_amount FLOAT NOT NULL, "
        "collected_amount FLOAT NOT NULL, iban VARCHAR(64), iban_holder VARCHAR(200), satispay_url VARCHAR(300), "
        "paypal_me_url VARCHAR(300), sort_order INTEGER)"
    )
    con.execute(
        "INSERT INTO gift_targets VALUES ('gruppo', 'Regalo Comune', '', '', '', '', 4800, 300, 'IT..', 'Comitato', '', '', 0)"
    )
    con.execute("CREATE TABLE gift_contributions (id INTEGER PRIMARY KEY, donor_name VARCHAR(200), amount FLOAT)")
    con.commit(); con.close()

    assert ensure_schema(make_engine(str(path))) == [
        "+event_notifications.scheduled_at", "+gift_targets.email", "+gift_targets.access_token", "+gift_targets.bancomat_pay", "+gift_targets.paypal_email",
        "+gift_targets.login_code_hash", "+gift_targets.login_code_expires_at", "+gift_targets.login_code_attempts", "-gift_contributions", "-gift_targets.target_amount", "-gift_targets.collected_amount",
    ]  # gift_pool_contributions non esiste ancora: create_all la crea già con le colonne del cassiere
    assert ensure_schema(make_engine(str(path))) == []  # idempotente
    with make_client(tmp_path, demo=False) as c:
        notes = c.get("/api/notifications").json()
        assert [n["title"] for n in notes] == ["vecchia"] and notes[0]["scheduledAt"] is None
        ids = [t["id"] for t in c.get("/api/gifts/targets").json()]
        assert "gruppo" not in ids and len(ids) == 9  # il regalo comune sparisce, i 9 del JSON vengono inseriti


def test_iban_validation_detects_placeholders():
    from app.validation import iban_is_valid, placeholder_gift_issues

    assert iban_is_valid("IT60 X054 2811 1010 0000 0123 456")  # esempio ufficiale valido
    assert iban_is_valid("DE89370400440532013000")
    assert not iban_is_valid("IT78 K030 6909 6061 0000 1234 567")  # segnaposto del seed
    assert not iban_is_valid("") and not iban_is_valid("ciao")
    with open(SEED, encoding="utf-8") as f:
        data = json.load(f)
    issues = placeholder_gift_issues(data)
    fake_targets = [t for t in data["giftTargets"] if not iban_is_valid(t["iban"])]
    assert len(issues) == len(fake_targets)  # tutti i cassieri hanno l'IBAN vero
    assert not any(i.startswith("cassiere") for i in issues)
    data["giftTargets"][0]["iban"] = "IT60 X054 2811 1010 0000 0123 456"
    for c in data["giftCollectors"]:
        c["iban"] = "IT60 X054 2811 1010 0000 0123 456"
    assert len(placeholder_gift_issues(data)) == len([t for t in data["giftTargets"][1:] if not iban_is_valid(t["iban"])])


def test_photo_gallery_newest_first_pages_and_thumbnails(client):
    """Galleria dalla più recente; lo snapshot manda la prima pagina, le altre arrivano a ritroso da /api/photos/page;
    ogni foto caricata ha la miniatura leggera (thumbUri) oltre alla foto intera (imageUri)."""
    from app.routers.photos import PAGE_SIZE

    def png(color):
        buf = io.BytesIO()
        Image.new("RGB", (2000, 1200), color).save(buf, "PNG")
        return buf.getvalue()

    ids = []
    for i in range(PAGE_SIZE + 5):  # più di una pagina (3 foto demo + queste)
        r = client.post("/api/photos", files={"file": (f"f{i}.png", png((i * 7 % 255, 40, 90)), "image/png")}, data={"caption": f"n{i}"}, headers=ADMIN)
        assert r.status_code == 201
        ids.append(r.json()["id"])
    p = client.get("/api/photos", headers=ADMIN).json()[0]
    assert p["id"] == ids[-1] and p["thumbUri"].endswith("_t.jpg") and p["imageUri"].endswith(".jpg") and p["thumbUri"] != p["imageUri"]
    w, h = Image.open(io.BytesIO(client.get(p["thumbUri"].replace("https://festa.example.org", "")).content)).size
    assert max(w, h) == 480

    snap = client.get("/api/snapshot", headers=ADMIN).json()
    assert len(snap["photos"]) == PAGE_SIZE and snap["photosHasMore"] is True
    assert [x["id"] for x in snap["photos"]][:3] == [ids[-1], ids[-2], ids[-3]]  # più recente in alto
    seen = [x["id"] for x in snap["photos"]]
    last = snap["photos"][-1]
    while True:
        page = client.get(f"/api/photos/page?limit=10&beforeTs={last['createdAt']}&beforeId={last['id']}", headers=ADMIN).json()
        seen += [x["id"] for x in page["photos"]]
        if not page["hasMore"]:
            break
        last = page["photos"][-1]
    all_ids = [x["id"] for x in client.get("/api/photos", headers=ADMIN).json()]
    assert seen == all_ids and len(seen) == len(set(seen)) == page["total"] == PAGE_SIZE + 5 + 3  # nessun doppione, nessuna mancante
    assert client.get("/api/photos/page").status_code == 401  # come la galleria: solo registrati e ruoli
    # la foto cancellata porta via anche la miniatura
    thumb = p["thumbUri"].replace("https://festa.example.org", "")
    assert client.delete(f"/api/photos/{p['id']}", headers=ADMIN).status_code == 204
    assert client.get(thumb).status_code == 404


def test_thumbnails_backfilled_on_start(tmp_path):
    """Le foto caricate prima delle miniature le ricevono al riavvio."""
    with make_client(tmp_path) as c:
        buf = io.BytesIO()
        Image.new("RGB", (900, 900), (10, 200, 10)).save(buf, "PNG")
        pid = c.post("/api/photos", files={"file": ("a.png", buf.getvalue(), "image/png")}, headers=ADMIN).json()["id"]
        from app.models import SharedPhoto
        with c.app.state.session_factory() as db:
            db.get(SharedPhoto, pid).thumb_path = ""
            db.commit()
    with make_client(tmp_path) as c:
        c.app.state.thumbnail_thread.join(timeout=10)  # le miniature si creano in background: qui aspettiamo
        p = next(x for x in c.get("/api/photos", headers=ADMIN).json() if x["id"] == pid)
        assert p["thumbUri"].endswith("_t.jpg")
