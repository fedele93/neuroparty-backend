import io
import json

from PIL import Image

from conftest import ADMIN, SEED, make_client


def test_health_and_event(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    ev = client.get("/api/event").json()
    assert len(ev["graduates"]) == 9
    assert ev["meta"]["maxBusSeats"] == 54
    assert len(ev["program"]["timeline"]) == 5


def test_demo_seed_matches_json(client):
    snap = client.get("/api/snapshot").json()
    assert len(snap["guests"]) == 7
    assert len(snap["busBookings"]) == 3
    assert len(snap["wishes"]) == 10
    assert len(snap["photos"]) == 3
    assert len(snap["giftTargets"]) == 10
    assert len(snap["giftContributions"]) == 4
    assert len(snap["notifications"]) == 4
    assert snap["version"] >= 1


def test_empty_seed_keeps_only_gift_targets(empty_client):
    snap = empty_client.get("/api/snapshot").json()
    assert snap["guests"] == [] and snap["wishes"] == [] and snap["notifications"] == []
    assert len(snap["giftTargets"]) == 10
    assert all(t["collectedAmount"] == 0 for t in snap["giftTargets"])


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


def test_gift_contribution_updates_target(client):
    before = {t["id"]: t["collectedAmount"] for t in client.get("/api/gifts/targets").json()}
    r = client.post(
        "/api/gifts/contributions",
        json={"donorName": "Zio", "targetGraduateId": "luisi", "amount": 50, "paymentMethod": "Satispay", "isAnonymous": True},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["contribution"]["donorName"] == "Un invitato generoso"
    assert body["target"]["collectedAmount"] == before["luisi"] + 50
    assert client.post("/api/gifts/contributions", json={"targetGraduateId": "nessuno", "amount": 10}).status_code == 404
    assert client.post("/api/gifts/contributions", json={"targetGraduateId": "luisi", "amount": 0}).status_code == 422


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
        c1.post("/api/gifts/contributions", json={"targetGraduateId": "luisi", "amount": 30, "donorName": "Zia"})
        luisi_before = next(t for t in c1.get("/api/gifts/targets").json() if t["id"] == "luisi")["collectedAmount"]
        v1 = c1.get("/api/state").json()["version"]

    with make_client(tmp_path) as c2:  # riavvio con il JSON completo (10 regali)
        targets = c2.get("/api/gifts/targets").json()
        regina = next(t for t in targets if t["id"] == "regina")
        assert regina["collectedAmount"] == 0 and regina["name"] == "Dott. Donato Regina"
        assert targets[-1]["id"] == "regina"  # rispetta l'ordine del JSON
        assert next(t for t in targets if t["id"] == "luisi")["collectedAmount"] == luisi_before
        assert c2.get("/api/state").json()["version"] == v1 + 1  # i client ricaricano
        assert len(c2.get("/api/guests").json()) == 7  # nessun doppio seed demo
