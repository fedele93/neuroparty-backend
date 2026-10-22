"""Elenchi riservati (invitati, navetta) e cifre delle quote uniche solo ai cassieri; pulizia da organizzatore."""


from conftest import ADMIN, TREASURER, make_client

DEV = {"X-Client-Id": "tel-privato"}


def test_lists_are_reserved_to_roles_and_own_records_only(tmp_path):
    with make_client(tmp_path, graduate_tokens={"luisi": "grad-luisi"}) as c:
        # senza token: lista vuota (nessun record proprio), riepilogo e snapshot senza elenchi
        assert c.get("/api/guests").json() == [] and c.get("/api/bus/bookings").json() == []
        assert c.get("/api/guests/summary").status_code == 403
        snap = c.get("/api/snapshot").json()
        assert snap["guests"] == [] and snap["busBookings"] == [] and snap["canSeeLists"] is False
        assert snap["wishes"] == [] and snap["photos"] == [] and snap["canSeeMemories"] is False  # foto e auguri: solo registrati
        assert len(snap["giftTargets"]) == 9  # il resto resta pubblico
        assert c.get("/api/state").json()["canSeeLists"] is False
        # un dispositivo vede i record che ha creato (invitato e prenotazione)
        g = c.post("/api/guests", json={"fullName": "Solo Io", "guestsCount": 2}, headers=DEV).json()
        b = c.post("/api/bus/bookings", json={"passengerName": "Solo Io", "seatsCount": 2}, headers={**ADMIN, **DEV}).json()  # a mano: solo organizzatori
        assert [x["id"] for x in c.get("/api/guests", headers=DEV).json()] == [g["id"]]
        assert [x["id"] for x in c.get("/api/bus/bookings", headers=DEV).json()] == [b["id"]]
        snap = c.get("/api/snapshot", headers=DEV).json()
        assert [x["id"] for x in snap["guests"]] == [g["id"]] and [x["id"] for x in snap["busBookings"]] == [b["id"]]
        assert c.get("/api/guests", headers={"X-Client-Id": "altro"}).json() == []
        # organizzatori, festeggiati e cassieri vedono tutto
        for h in (ADMIN, TREASURER, {"X-Graduate-Token": "grad-luisi"}):
            assert len(c.get("/api/guests", headers=h).json()) == 8, h
            assert len(c.get("/api/bus/bookings", headers=h).json()) == 4, h
            assert c.get("/api/guests/summary", headers=h).status_code == 200
            assert c.get("/api/state", headers=h).json()["canSeeLists"] is True
            assert len(c.get("/api/snapshot", headers=h).json()["guests"]) == 8
        assert c.get("/api/guests", headers={"X-Graduate-Token": "sbagliato"}).json() == []
        # email e telefono restano solo per gli organizzatori
        assert "email" not in c.get("/api/guests", headers=TREASURER).json()[0]
        assert "email" in c.get("/api/guests", headers=ADMIN).json()[0]


def test_pool_amounts_only_for_treasurers(tmp_path):
    with make_client(tmp_path) as c:
        # il token organizzatore non apre più il cruscotto delle quote né il CSV
        assert c.get("/api/gifts/pool", headers=ADMIN).status_code == 403
        assert c.get("/api/export/gift-pool.csv", headers=ADMIN).status_code == 403
        assert c.post("/api/gifts/pool/status", json={"ids": [1], "status": "RECEIVED"}, headers=ADMIN).status_code == 403
        assert c.get("/api/gifts/pool", headers=TREASURER).status_code == 200
        assert c.get("/api/state").json()["treasurerEnabled"] is True
        # cruscotto organizzatori e report: conteggi sì, cifre no
        dash = c.get("/api/admin/dashboard", headers=ADMIN).json()["pool"]
        assert dash["contributions"] == 3 and "totalAmount" not in dash and "receivedAmount" not in dash
        rep = c.get("/api/automation/report", headers=ADMIN).json()
        assert "totalAmount" not in rep["gifts"] and all("totalAmount" not in k for k in rep["recent"]["giftPool"])
        txt = c.get("/api/automation/report.txt", headers=ADMIN).text
        assert "€" not in txt.split("Regali")[1].split("Auguri")[0] if "Regali" in txt else True
    with make_client(tmp_path / "b", treasurer_token="") as c:
        assert c.get("/api/state").json()["treasurerEnabled"] is False
        assert c.get("/api/gifts/pool", headers=ADMIN).status_code == 503


def test_admin_can_delete_photos_wishes_and_clear_history(tmp_path):
    with make_client(tmp_path) as c:
        wish = c.get("/api/wishes", headers=ADMIN).json()[0]
        photo = c.get("/api/photos", headers=ADMIN).json()[0]
        assert c.delete(f"/api/wishes/{wish['id']}").status_code == 403
        assert c.delete(f"/api/photos/{photo['id']}").status_code == 403
        assert c.delete(f"/api/wishes/{wish['id']}", headers=ADMIN).status_code == 204
        assert c.delete(f"/api/photos/{photo['id']}", headers=ADMIN).status_code == 204
        assert wish["id"] not in [w["id"] for w in c.get("/api/wishes", headers=ADMIN).json()]
        assert photo["id"] not in [p["id"] for p in c.get("/api/photos", headers=ADMIN).json()]
        # cronologia notifiche: svuotata dall'organizzatore, le programmate restano
        future = c.get("/api/state").json()["serverTime"] + 3_600_000
        c.post("/api/notifications", json={"title": "Dopo", "message": "x", "sendAt": future}, headers=ADMIN)
        assert len(c.get("/api/notifications").json()) >= 1
        assert c.delete("/api/notifications").status_code == 403
        r = c.delete("/api/notifications", headers=ADMIN)
        assert r.status_code == 200 and r.json()["deleted"] >= 1
        assert c.get("/api/notifications").json() == []
        assert len(c.get("/api/notifications/scheduled", headers=ADMIN).json()) == 1


def test_state_reports_roles_unlocked_by_tokens(tmp_path):
    with make_client(tmp_path, treasurer_tokens={"aki": "cassa-aki"}, graduate_tokens={"luisi": "grad-luisi"}) as c:
        none = {"admin": False, "treasurer": False, "treasurerIds": [], "graduate": None}
        assert c.get("/api/state").json()["roles"] == none
        assert c.get("/api/state", headers=ADMIN).json()["roles"] == {**none, "admin": True}
        assert c.get("/api/state", headers=TREASURER).json()["roles"] == {**none, "treasurer": True}
        assert c.get("/api/state", headers={"X-Treasurer-Token": "cassa-aki"}).json()["roles"] == {**none, "treasurer": True, "treasurerIds": ["aki"]}
        assert c.get("/api/state", headers={"X-Graduate-Token": "grad-luisi"}).json()["roles"] == {**none, "graduate": "luisi"}
        # token sbagliati: nessun ruolo (e il token festeggiato sbagliato non toglie quello organizzatore qui)
        bad = {"X-Admin-Token": "no", "X-Treasurer-Token": "no", "X-Graduate-Token": "no"}
        assert c.get("/api/state", headers=bad).json()["roles"] == none
        both = {**ADMIN, "X-Graduate-Token": "grad-luisi"}
        assert c.get("/api/state", headers=both).json()["roles"] == {**none, "admin": True, "graduate": "luisi"}
