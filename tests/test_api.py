import io
import json
import re

from PIL import Image

from conftest import ADMIN, SEED, TREASURER, make_client


def test_health_and_event(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    ev = client.get("/api/event").json()
    assert len(ev["graduates"]) == 9
    assert ev["meta"]["maxBusSeats"] == 54
    assert len(ev["program"]["timeline"]) == 5
    assert ev["giftCollector"]["name"] == "Dott. Paolo Roberto"
    assert ev["giftCollector"]["paymentMethods"] == ["IBAN", "PayPal", "Contanti"]


def test_demo_seed_matches_json(client):
    snap = client.get("/api/snapshot").json()
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
    pool = client.get("/api/gifts/pool", headers=ADMIN).json()
    assert pool["summary"]["contributions"] == 3 and pool["summary"]["received"] == 1
    assert pool["summary"]["totalAmount"] == 180 + 150 + 90


def test_empty_seed_keeps_only_gift_targets(empty_client):
    snap = empty_client.get("/api/snapshot").json()
    assert snap["guests"] == [] and snap["wishes"] == [] and snap["notifications"] == []
    assert len(snap["giftTargets"]) == 9
    assert empty_client.get("/api/gifts/pool", headers=ADMIN).json()["contributions"] == []


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

    seeded = client.get("/api/guests").json()[0]  # record senza owner: solo l'organizzatore
    assert client.delete(f"/api/guests/{seeded['id']}", headers={"X-Client-Id": "dev-A"}).status_code == 403
    assert client.delete(f"/api/guests/{seeded['id']}", headers=ADMIN).status_code == 204


def test_guest_validation(client):
    assert client.post("/api/guests", json={"fullName": "   "}).status_code == 422
    assert client.post("/api/guests", json={"fullName": "X", "rsvpStatus": "MAYBE"}).status_code == 422


def test_bus_capacity(client):
    s = client.get("/api/bus/summary").json()
    assert s == {"maxSeats": 54, "bookedSeats": 10, "availableSeats": 44}
    r = client.post("/api/bus/bookings", json={"passengerName": "Gruppo", "seatsCount": 45})
    assert r.status_code == 409
    r = client.post("/api/bus/bookings", json={"passengerName": "Gruppo", "seatsCount": 44}, headers={"X-Client-Id": "c1"})
    assert r.status_code == 201
    assert client.get("/api/bus/summary").json()["availableSeats"] == 0
    assert client.post("/api/bus/bookings", json={"passengerName": "Tardo", "seatsCount": 1}).status_code == 409
    bid = r.json()["id"]
    assert client.delete(f"/api/bus/bookings/{bid}").status_code == 403
    assert client.delete(f"/api/bus/bookings/{bid}", headers={"X-Client-Id": "c1"}).status_code == 204
    assert client.get("/api/bus/summary").json()["availableSeats"] == 44


def test_wishes(client):
    r = client.post("/api/wishes", json={"authorName": "", "message": " Auguri! ", "targetGraduate": "Fedele Luisi"})
    assert r.status_code == 201
    w = r.json()
    assert w["authorName"] == "Amico/a" and w["message"] == "Auguri!" and w["heartCount"] == 1
    assert client.post(f"/api/wishes/{w['id']}/heart").json()["heartCount"] == 2
    assert client.post("/api/wishes", json={"message": "   "}).status_code == 422
    assert client.get("/api/wishes").json()[0]["id"] == w["id"]  # ordinamento: più recente per primo
    assert client.delete(f"/api/wishes/{w['id']}").status_code == 403
    assert client.delete(f"/api/wishes/{w['id']}", headers=ADMIN).status_code == 204


def test_gift_targets_hide_amounts_and_pool_is_private(client):
    targets = client.get("/api/gifts/targets").json()
    assert [t["id"] for t in targets][:2] == ["luisi", "carlone"] and "gruppo" not in [t["id"] for t in targets]
    for t in targets:
        assert "collectedAmount" not in t and "targetAmount" not in t
        assert t["iban"] and t["paypalMeUrl"] and t["satispayUrl"]
    collector = client.get("/api/gifts/collector").json()
    assert collector["name"] == "Dott. Paolo Roberto" and collector["iban"]
    assert "Satispay" not in collector["paymentMethods"]
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
    assert client.get("/api/state").json()["version"] == v0 + 1

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
    client.patch(f"/api/gifts/pool/{mine['id']}/status", json={"status": "RECEIVED"}, headers=ADMIN)
    assert client.delete(f"/api/gifts/pool/{mine['id']}", headers=a).status_code == 409  # già ricevuta
    client.patch(f"/api/gifts/pool/{mine['id']}/status", json={"status": "PENDING"}, headers=ADMIN)
    assert client.delete(f"/api/gifts/pool/{mine['id']}", headers=a).status_code == 204
    assert client.delete(f"/api/gifts/pool/{mine['id']}", headers=a).status_code == 404
    # il cassiere/organizzatore cancella sempre
    other = client.post("/api/gifts/pool", json={"donorName": "Bruno", "totalAmount": 9}, headers=b).json()
    assert client.delete(f"/api/gifts/pool/{other['id']}", headers=ADMIN).status_code == 204


def test_pool_treasurer_dashboard_and_csv(tmp_path):
    with make_client(tmp_path, treasurer_token="cassa-test", automation_token="n8n-test") as c:
        assert c.get("/api/state").json()["treasurerEnabled"] is True
        board = c.get("/api/gifts/pool", headers=TREASURER).json()
        s = board["summary"]
        assert s["contributions"] == 3 and s["pending"] == 2 and s["receivedAmount"] == 180
        luisi = next(g for g in s["byGraduate"] if g["graduateId"] == "luisi")
        assert luisi == {"graduateId": "luisi", "graduateName": "Dott. Fedele Luisi", "amount": 80, "receivedAmount": 20, "contributions": 3}
        assert {m["paymentMethod"]: m["amount"] for m in s["byMethod"]} == {"IBAN": 180, "PayPal": 150, "Contanti": 90}
        assert board["contributions"][0]["donorName"] == "Amici del corso"  # la più recente per prima

        # il cassiere segna come ricevuta la quota PayPal
        paypal = next(k for k in board["contributions"] if k["paymentMethod"] == "PayPal")
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

        # CSV: una colonna per neo-specialista e righe dei totali; importi con la virgola
        for headers in (TREASURER, ADMIN, {"X-Automation-Token": "n8n-test"}):
            r = c.get("/api/export/gift-pool.csv", headers=headers)
            assert r.status_code == 200
        assert 'filename="quote-uniche.csv"' in r.headers["content-disposition"]
        lines = r.text.lstrip("\ufeff").splitlines()
        head = lines[0].split(";")
        assert head[:9] == ["Data", "Donatore", "Contatto", "Metodo", "Ripartizione", "Totale", "Stato", "Ricevuta il", "Note"]
        assert head[9:] == [t["name"] for t in c.get("/api/gifts/targets").json()]
        assert len(lines) == 1 + 3 + 2
        rows = {ln.split(";")[1]: ln.split(";") for ln in lines[1:4]}
        assert rows["Colleghi Reparto Stroke"][3:7] == ["PayPal", "Personalizzata", "150,00", "Ricevuta"]
        assert rows["Colleghi Reparto Stroke"][9] == "50,00" and rows["Colleghi Reparto Stroke"][10] == ""  # luisi, carlone
        assert rows["Amici del corso"][5:7] == ["90,00", "In attesa"] and rows["Amici del corso"][9] == "10,00"
        assert lines[4].split(";")[:6] == ["TOTALE", "", "", "", "", "420,00"] and lines[4].split(";")[9] == "80,00"
        assert lines[5].split(";")[:6] == ["DI CUI RICEVUTO", "", "", "", "", "330,00"] and lines[5].split(";")[9] == "70,00"

    with make_client(tmp_path / "b", admin_token="", treasurer_token="") as c:
        assert c.get("/api/gifts/pool", headers=TREASURER).status_code == 503
        assert c.get("/api/state").json()["treasurerEnabled"] is False


def test_photo_upload_resizes_and_serves(client):
    img = Image.new("RGB", (3000, 2000), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = client.post(
        "/api/photos",
        files={"file": ("big.png", buf.getvalue(), "image/png")},
        data={"authorName": "Chiara", "caption": "Prova"},
    )
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["imageUri"].startswith("https://festa.example.org/uploads/") and p["imageUri"].endswith(".jpg")
    path = p["imageUri"].replace("https://festa.example.org", "")
    served = client.get(path)
    assert served.status_code == 200
    w, h = Image.open(io.BytesIO(served.content)).size
    assert max(w, h) == 1600
    assert client.post(f"/api/photos/{p['id']}/like").json()["likesCount"] == 2
    bad = client.post("/api/photos", files={"file": ("x.txt", b"non immagine", "text/plain")})
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
        assert len(c2.get("/api/guests").json()) == 7
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
        assert len(c2.get("/api/guests").json()) == 7  # nessun doppio seed demo
        # le quote registrate prima restano (con la ripartizione di allora)
        board = c2.get("/api/gifts/pool", headers=ADMIN).json()
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
        csv_head = c2.get("/api/export/gift-pool.csv", headers=ADMIN).text.splitlines()[0]
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
        assert ev["schedule"]["ceremonyTime"] == ""  # seduta: ora ancora da definire
        assert not re.search(r"\{\w+", json.dumps(ev))  # nessun segnaposto nei testi serviti
        festa = next(p for p in ev["mapPoints"] if p["id"] == "festa")
        assert festa["timeLabel"] == "Venerdì 13 novembre - dalle 21:30 alle 03:00"
        assert "Dalle ore 21:30 fino alle 03:00." in festa["description"]
        assert ev["program"]["timeline"][3]["time"] == "Ven 13 ore 21:30"
        seduta = next(p for p in ev["mapPoints"] if p["id"] == "seduta")
        assert seduta["timeLabel"] == "9 Novembre - ora da definire"

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
        assert "DTSTART;VALUE=DATE:20261109" in body  # seduta senza orario: tutto il giorno
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
    s = client.get("/api/guests/summary").json()
    assert s["confirmedGuests"] == 5 and s["covers"] == 10
    assert s["pendingGuests"] == 1 and s["pendingCovers"] == 2 and s["declinedGuests"] == 1
    cats = {c["category"]: c for c in s["byCategory"]}
    assert cats["Famigliari"] == {"category": "Famigliari", "guests": 2, "covers": 6}
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
    assert lines[0] == "Nome;Categoria;Stato RSVP;Persone;Esigenze alimentari;Contatto;Aggiornato il"
    assert len(lines) == 8  # intestazione + 7 invitati demo
    assert any(line.startswith("Dott. Luca Gatti;Colleghi Reparto;In attesa;2;") for line in lines)

    r = client.get("/api/export/bus.csv", headers=ADMIN)
    lines = r.text.lstrip("\ufeff").splitlines()
    assert lines[0].startswith("Passeggero;Posti;Fermata;Ritorno")
    assert lines[-1].startswith("TOTALE POSTI;10;")


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
        "+event_notifications.scheduled_at", "-gift_contributions", "-gift_targets.target_amount", "-gift_targets.collected_amount",
    ]
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
    assert len(issues) == len(data["giftTargets"]) + 1  # tutti segnaposto, cassiere compreso
    assert issues[0].startswith("cassiere")
    data["giftTargets"][0]["iban"] = "IT60 X054 2811 1010 0000 0123 456"
    data["giftCollector"]["iban"] = "IT60 X054 2811 1010 0000 0123 456"
    assert len(placeholder_gift_issues(data)) == len(data["giftTargets"]) - 1
