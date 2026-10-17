"""Fase 4: area festeggiato, strumenti dei cassieri (note, stato in blocco, promemoria), email al donatore e al festeggiato."""


from conftest import ADMIN, TREASURER, make_client
from test_automation import N8N_URL, FakeN8n

ROBERTO = {"X-Treasurer-Token": "tok-roberto"}
AKI = {"X-Treasurer-Token": "tok-aki"}
LUISI = {"X-Graduate-Token": "tok-luisi"}


def _mails(n8n: FakeN8n) -> list[dict]:
    return [n8n.payload(i)["data"] for i, e in enumerate(n8n.events) if e == "mail.send"]


def _client(tmp_path, n8n: FakeN8n, **kw):
    c = make_client(
        tmp_path, webhook_url=N8N_URL, webhook_secret="shh", webhook_handler=n8n,
        treasurer_tokens={"roberto": "tok-roberto", "aki": "tok-aki"}, graduate_tokens={"luisi": "tok-luisi"},
        assistant_fake=True, **kw,
    )
    c.app.state.mailer.sync = True
    return c


def test_pool_email_confirmation_note_batch_and_reminder(tmp_path):
    n8n = FakeN8n()
    with _client(tmp_path, n8n) as c:
        body = {"collectorId": "roberto", "donorName": "Anna Verdi", "email": "Anna@Example.org", "paymentMethod": "IBAN",
                "splitMode": "EQUAL", "totalAmount": 90, "graduateIds": ["luisi", "carlone", "prezioso"]}
        r = c.post("/api/gifts/pool", json=body, headers={"X-Client-Id": "tel-anna"})
        assert r.status_code == 201, r.text
        q = r.json()
        assert q["email"] == "anna@example.org" and q["treasurerNote"] == "" and q["mail"]["status"] == "DELIVERED" and q["mail"]["kind"] == "pool_confirmation"
        mails = _mails(n8n)
        assert len(mails) == 1 and mails[0]["kind"] == "pool_confirmation" and mails[0]["to"] == "anna@example.org"
        assert "90,00 €" in mails[0]["subject"] and "Paolo Roberto" in mails[0]["subject"]
        assert "IBAN" in mails[0]["text"] and "Anna Verdi" in mails[0]["text"] and "30,00 €" in mails[0]["text"]
        # email non valida -> 422; senza email nessuna mail
        bad = {**body, "email": "non-una-mail"}
        assert c.post("/api/gifts/pool", json=bad, headers={"X-Client-Id": "x"}).status_code == 422
        r2 = c.post("/api/gifts/pool", json={**body, "email": "", "donorName": "Bruno", "collectorId": "aki", "paymentMethod": "PayPal"}, headers={"X-Client-Id": "tel-bruno"})
        assert r2.status_code == 201 and "mail" not in r2.json() and len(_mails(n8n)) == 1

        # nota del cassiere: solo nel proprio ambito
        r = c.patch(f"/api/gifts/pool/{q['id']}", json={"treasurerNote": " bonifico visto il 3/11 "}, headers=ROBERTO)
        assert r.status_code == 200 and r.json()["treasurerNote"] == "bonifico visto il 3/11"
        assert c.patch(f"/api/gifts/pool/{q['id']}", json={"treasurerNote": "x"}, headers=AKI).status_code == 403
        assert c.patch(f"/api/gifts/pool/{q['id']}", json={"treasurerNote": "x"}).status_code == 403
        assert c.patch("/api/gifts/pool/9999", json={"treasurerNote": "x"}, headers=TREASURER).status_code == 404
        mine = c.get("/api/gifts/pool/mine", headers={"X-Client-Id": "tel-anna"}).json()
        assert mine[0]["treasurerNote"] == "bonifico visto il 3/11"

        # stato in blocco: le quote di altri cassieri vengono saltate
        ids = [q["id"], r2.json()["id"]]
        r = c.post("/api/gifts/pool/status", json={"ids": ids + [9999], "status": "RECEIVED"}, headers=ROBERTO)
        assert r.status_code == 200 and r.json() == {"updated": 1, "skipped": [r2.json()["id"], 9999]}
        rows = {x["id"]: x for x in c.get("/api/gifts/pool", headers=TREASURER).json()["contributions"]}
        assert rows[q["id"]]["status"] == "RECEIVED" and rows[q["id"]]["receivedAt"] and rows[r2.json()["id"]]["status"] == "PENDING"
        r = c.post("/api/gifts/pool/status", json={"ids": ids, "status": "PENDING"}, headers=TREASURER)
        assert r.json() == {"updated": 2, "skipped": []}
        assert c.post("/api/gifts/pool/status", json={"ids": ids, "status": "BOH"}, headers=TREASURER).status_code == 422

        # promemoria: solo quote in attesa, con email, più vecchie di N giorni, nel proprio ambito
        assert c.post("/api/mail/reminders/pool", headers=AKI).json()["sent"] == 0  # Bruno non ha email
        assert c.post("/api/mail/reminders/pool?days=7", headers=ROBERTO).json()["sent"] == 0  # troppo recente
        r = c.post("/api/mail/reminders/pool?days=0", headers=ROBERTO)
        assert r.status_code == 200 and r.json()["sent"] == 1
        last = _mails(n8n)[-1]
        assert last["kind"] == "pool_reminder" and last["to"] == "anna@example.org" and "ancora in attesa" in last["subject"]
        assert c.post("/api/mail/reminders/pool?days=0", headers=AKI).json()["sent"] == 0
        assert c.post("/api/mail/reminders/pool?days=0").status_code == 403
        # ricevuta -> niente più promemoria
        c.post("/api/gifts/pool/status", json={"ids": [q["id"]], "status": "RECEIVED"}, headers=ROBERTO)
        assert c.post("/api/mail/reminders/pool?days=0", headers=TREASURER).json()["sent"] == 0
        # il CSV: ?collector= restringe (n8n manda a ogni cassiere il suo), ma non oltre il proprio ambito
        csv_all = c.get("/api/export/gift-pool.csv", headers=TREASURER).text
        csv_aki = c.get("/api/export/gift-pool.csv?collector=aki", headers=TREASURER).text
        assert "Anna Verdi" in csv_all and "Bruno" in csv_all and "Bruno" in csv_aki and "Anna Verdi" not in csv_aki
        assert c.get("/api/export/gift-pool.csv?collector=aki", headers=ROBERTO).status_code == 403
        assert "Anna Verdi" in c.get("/api/export/gift-pool.csv?collector=roberto", headers=ROBERTO).text


def test_graduate_area_wish_mail_and_own_avatar(tmp_path, monkeypatch):
    n8n = FakeN8n()
    with _client(tmp_path, n8n) as c:
        # senza email privata del festeggiato nessuna mail per gli auguri
        c.post("/api/wishes", json={"authorName": "Zia Pina", "targetGraduate": "Fedele Luisi", "message": "Bravo!"}, headers={**ADMIN, "X-Client-Id": "z"})
        assert _mails(n8n) == []
        # email del festeggiato (privata: non esce dalle API pubbliche)
        from app.models import GiftTarget
        with c.app.state.session_factory() as db:
            t = db.get(GiftTarget, "luisi")
            t.email = "fedele@example.org"
            db.commit()
        pub = next(t for t in c.get("/api/gifts/targets").json() if t["id"] == "luisi")
        assert "email" not in pub and pub["shortName"] == "Fedele Luisi"
        r = c.post("/api/wishes", json={"authorName": "Nonna", "targetGraduate": "Fedele Luisi", "message": "Auguri dottore"}, headers={**ADMIN, "X-Client-Id": "n"})
        assert r.status_code == 201
        mails = _mails(n8n)
        assert len(mails) == 1 and mails[0]["kind"] == "wish_received" and mails[0]["to"] == "fedele@example.org"
        assert "Nonna" in mails[0]["subject"] and "Auguri dottore" in mails[0]["text"]
        c.post("/api/wishes", json={"authorName": "Tutti", "targetGraduate": "Tutti i Laureandi", "message": "Evviva"}, headers={**ADMIN, "X-Client-Id": "t"})
        assert len(_mails(n8n)) == 1

        # area festeggiato
        assert c.get("/api/graduate/me").status_code == 403
        assert c.get("/api/graduate/me", headers={"X-Graduate-Token": "sbagliato"}).status_code == 403
        assert c.get("/api/graduate/me", headers=ADMIN).status_code == 422
        assert c.get("/api/graduate/me?graduate_id=nessuno", headers=ADMIN).status_code == 404
        me = c.get("/api/graduate/me", headers=LUISI).json()
        assert me["graduate"]["id"] == "luisi" and me["graduate"]["email"] == "fedele@example.org" and me["graduate"]["iban"]
        assert "collectedAmount" not in me["graduate"] and "pool" not in me
        assert me["guests"]["confirmedGuests"] >= 1 and isinstance(me["guests"]["confirmed"], list) and "pending" in me["guests"]
        # 2 auguri di questo test + quello dimostrativo dei colleghi; quelli per altri festeggiati non ci sono
        assert me["wishesForMe"] == 3 and [w["message"] for w in me["wishes"]][:2] == ["Evviva", "Auguri dottore"]
        assert all(w["targetGraduate"] in ("Fedele Luisi", "Tutti i Laureandi") for w in me["wishes"])
        assert me["bus"]["maxSeats"] == 54 and "passengers" in me["bus"]
        assert me["avatar"]["id"] == "luisi" and me["assistantConfigured"] is True
        # con token organizzatore serve l'id
        assert c.get("/api/graduate/me?graduate_id=luisi", headers=ADMIN).json()["graduate"]["id"] == "luisi"

        # il festeggiato gestisce solo il proprio avatar
        lst = c.get("/api/assistant/admin/avatars", headers=LUISI).json()
        assert [a["id"] for a in lst["avatars"]] == ["luisi"]
        assert len(c.get("/api/assistant/admin/avatars", headers=ADMIN).json()["avatars"]) == 9
        assert c.get("/api/assistant/admin/avatars").status_code == 403
        r = c.put("/api/assistant/admin/avatars/luisi", json={"persona": "Sono Fedele, parlo piano."}, headers=LUISI)
        assert r.status_code == 200 and r.json()["persona"] == "Sono Fedele, parlo piano."
        assert c.put("/api/assistant/admin/avatars/carlone", json={"persona": "x"}, headers=LUISI).status_code == 403
        assert c.get("/api/assistant/admin/voices", headers=LUISI).status_code == 200
        assert c.post("/api/assistant/admin/avatars/carlone/preview", json={"text": "ciao"}, headers=LUISI).status_code == 403
        assert c.post("/api/assistant/admin/avatars/luisi/preview", json={"text": "ciao"}, headers=LUISI).status_code == 200


def test_graduate_without_tokens_is_503(tmp_path):
    with make_client(tmp_path, admin_token="") as c:
        assert c.get("/api/graduate/me", headers={"X-Graduate-Token": "x"}).status_code == 503
    with make_client(tmp_path / "b") as c:  # con ADMIN_TOKEN ma senza token festeggiati: 403, non 503
        assert c.get("/api/graduate/me", headers={"X-Graduate-Token": "x"}).status_code == 403


def test_graduate_email_from_env_gets_wish_mail(tmp_path):
    """GRADUATE_EMAIL_<ID>: email privata del festeggiato fuori dal JSON pubblico."""
    n8n = FakeN8n()
    with _client(tmp_path, n8n, graduate_emails={"totaro": "dalila@example.org"}) as c:
        from app.models import GiftTarget
        with c.app.state.session_factory() as db:
            assert db.get(GiftTarget, "totaro").email == "dalila@example.org"
        assert "email" not in next(t for t in c.get("/api/gifts/targets").json() if t["id"] == "totaro")
        r = c.post("/api/wishes", json={"authorName": "Zia", "targetGraduate": "Dalila Totaro", "message": "Brava Dalila!"}, headers=ADMIN)
        assert r.status_code == 201
        mails = _mails(n8n)
        assert len(mails) == 1 and mails[0]["kind"] == "wish_received" and mails[0]["to"] == "dalila@example.org"


def test_graduate_link_by_email(tmp_path):
    """Il festeggiato si fa mandare il link (#festeggiato=<token>) all'email privata: vale come GRADUATE_TOKEN."""
    import re

    n8n = FakeN8n()
    with _client(tmp_path, n8n, graduate_emails={"totaro": "Dalila@Example.org"}) as c:
        r = c.post("/api/graduate/request-link", json={"email": "dalila@example.org"})
        assert r.status_code == 202 and r.json()["queued"] is True
        mails = _mails(n8n)
        assert len(mails) == 1 and mails[0]["kind"] == "graduate_link" and mails[0]["to"] == "Dalila@Example.org"
        token = re.search(r"#festeggiato=([A-Za-z0-9_-]+)", mails[0]["text"]).group(1)
        assert "https://festa.example.org/#festeggiato=" in mails[0]["text"]
        H = {"X-Graduate-Token": token}
        me = c.get("/api/graduate/me", headers=H).json()
        assert me["graduate"]["id"] == "totaro"
        st = c.get("/api/state", headers=H).json()
        assert st["roles"]["graduate"] == "totaro" and st["canSeeLists"] is True and st["canSeeMemories"] is True
        assert c.get("/api/assistant/admin/avatars", headers=H).status_code == 200  # il proprio avatar
        # stesso link a ogni richiesta; email sconosciuta: stessa risposta, nessuna mail
        c.post("/api/graduate/request-link", json={"email": "DALILA@example.org"})
        assert token in _mails(n8n)[-1]["text"] and len(_mails(n8n)) == 2
        other = c.post("/api/graduate/request-link", json={"email": "chiunque@example.org"})
        assert other.json()["message"] == r.json()["message"] and len(_mails(n8n)) == 2
        assert c.post("/api/graduate/request-link", json={"email": "non-una-mail"}).status_code == 422
        # token sbagliato: niente area, niente elenchi
        assert c.get("/api/graduate/me", headers={"X-Graduate-Token": token[:-1] + "x"}).status_code == 403
        assert c.get("/api/state", headers={"X-Graduate-Token": "x" * 30}).json()["roles"]["graduate"] is None


def test_graduate_link_without_mail_says_so(tmp_path):
    with make_client(tmp_path, graduate_emails={"totaro": "dalila@example.org"}) as c:
        r = c.post("/api/graduate/request-link", json={"email": "dalila@example.org"})
        assert r.status_code == 202 and r.json()["queued"] is False and "organizzatori" in r.json()["message"]


def test_role_invites_dry_run_and_send(tmp_path):
    """Cruscotto: inviti con link e istruzioni a festeggiati (GRADUATE_EMAIL_<ID>) e cassieri (TREASURER_EMAIL_<ID> + token)."""
    import re

    n8n = FakeN8n()
    with _client(tmp_path, n8n, graduate_emails={"totaro": "dalila@example.org", "carlone": "seba@example.org"},
                 treasurer_emails={"aki": "aki@example.org"}) as c:
        assert c.post("/api/admin/invites", json={}).status_code == 403
        dry = c.post("/api/admin/invites", json={"dryRun": True}, headers=ADMIN).json()
        by = {(r["role"], r["id"]): r for r in dry["invites"]}
        assert by[("graduate", "totaro")]["status"] == "READY" and by[("graduate", "totaro")]["email"] == "dalila@example.org"
        assert by[("graduate", "luisi")]["status"] == "NO_EMAIL"
        assert by[("treasurer", "aki")]["status"] == "READY" and by[("treasurer", "roberto")]["status"] == "NO_EMAIL"
        assert dry["ready"] == 3 and _mails(n8n) == []  # anteprima: nessuna email
        sent = c.post("/api/admin/invites", json={"dryRun": False, "testPhase": True, "ids": ["totaro", "aki"]}, headers=ADMIN).json()
        assert {(r["role"], r["id"]) for r in sent["invites"]} == {("graduate", "totaro"), ("treasurer", "aki")}
        mails = _mails(n8n)
        assert [m["kind"] for m in mails] == ["graduate_link", "treasurer_invite"]
        assert "fase di prova" in mails[0]["text"].lower() and "Installa" in mails[0]["text"] and "#festeggiato=" in mails[0]["text"]
        token = re.search(r"#festeggiato=([A-Za-z0-9_-]+)", mails[0]["text"]).group(1)
        assert c.get("/api/graduate/me", headers={"X-Graduate-Token": token}).json()["graduate"]["id"] == "totaro"
        # il link del cassiere nasce nel database (non è il token del .env) e vale solo per le sue quote
        aki_link = re.search(r"#cassiere=([A-Za-z0-9_-]{20,})", mails[1]["text"]).group(1)
        assert aki_link != "tok-aki" and "Promemoria" in mails[1]["text"] and mails[1]["to"] == "aki@example.org"
        board = c.get("/api/gifts/pool", headers={"X-Treasurer-Token": aki_link}).json()
        assert [x["id"] for x in board["collectors"]] == ["aki"]
        assert c.get("/api/state", headers={"X-Treasurer-Token": aki_link}).json()["roles"]["treasurerIds"] == ["aki"]
        # stesso link a un nuovo invio; con rotate ne nasce uno nuovo e il vecchio smette di valere
        c.post("/api/admin/invites", json={"dryRun": False, "ids": ["aki"], "roles": ["treasurers"]}, headers=ADMIN)
        assert aki_link in _mails(n8n)[-1]["text"]
        c.post("/api/admin/invites", json={"dryRun": False, "ids": ["aki"], "roles": ["treasurers"], "rotate": True}, headers=ADMIN)
        new_link = re.search(r"#cassiere=([A-Za-z0-9_-]{20,})", _mails(n8n)[-1]["text"]).group(1)
        assert new_link != aki_link
        assert c.get("/api/gifts/pool", headers={"X-Treasurer-Token": aki_link}).status_code == 403
        assert c.get("/api/gifts/pool", headers={"X-Treasurer-Token": new_link}).status_code == 200
        assert c.get("/api/gifts/pool", headers={"X-Treasurer-Token": "tok-aki"}).status_code == 200  # il .env continua a valere


def test_reset_test_data_keeps_configuration(tmp_path):
    import io as _io

    from PIL import Image

    n8n = FakeN8n()
    with _client(tmp_path, n8n, graduate_emails={"totaro": "dalila@example.org"}) as c:
        buf = _io.BytesIO()
        Image.new("RGB", (300, 200), (1, 2, 3)).save(buf, "PNG")
        photo = c.post("/api/photos", files={"file": ("a.png", buf.getvalue(), "image/png")}, headers=ADMIN).json()
        c.post("/api/graduate/request-link", json={"email": "dalila@example.org"})
        import re
        token = re.search(r"#festeggiato=([A-Za-z0-9_-]+)", _mails(n8n)[-1]["text"]).group(1)
        assert c.post("/api/admin/reset-test-data", json={"confirm": "AZZERA"}).status_code == 403
        assert c.post("/api/admin/reset-test-data", json={"confirm": "sì"}, headers=ADMIN).status_code == 422
        r = c.post("/api/admin/reset-test-data", json={"confirm": "azzera"}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["deleted"]["guests"] >= 7 and r.json()["deleted"]["photos"] >= 4
        snap = c.get("/api/snapshot", headers=ADMIN).json()
        assert snap["guests"] == [] and snap["busBookings"] == [] and snap["wishes"] == [] and snap["photos"] == [] and snap["notifications"] == []
        assert len(snap["giftTargets"]) == 9 and snap["giftTargets"][0]["iban"]  # la configurazione resta
        assert c.get(photo["imageUri"].replace("https://festa.example.org", "")).status_code == 404  # file cancellati
        assert c.get("/api/graduate/me", headers={"X-Graduate-Token": token}).status_code == 200  # i link dei festeggiati restano
        assert c.get("/api/gifts/pool", headers=TREASURER).json()["summary"]["contributions"] == 0


def test_treasurer_link_works_without_env_tokens(tmp_path):
    """Senza TREASURER_TOKEN nel .env i cassieri entrano comunque con il link generato dal cruscotto."""
    n8n = FakeN8n()
    with make_client(tmp_path, webhook_url=N8N_URL, webhook_secret="shh", webhook_handler=n8n, treasurer_token="",
                     treasurer_emails={"roberto": "paolo@example.org"}) as c:
        c.app.state.mailer.sync = True
        assert c.get("/api/state").json()["treasurerEnabled"] is False
        assert c.get("/api/gifts/pool", headers={"X-Treasurer-Token": "x" * 30}).status_code == 503
        c.post("/api/admin/invites", json={"dryRun": False, "roles": ["treasurers"]}, headers=ADMIN)
        import re
        link = re.search(r"#cassiere=([A-Za-z0-9_-]{20,})", _mails(n8n)[-1]["text"]).group(1)
        assert c.get("/api/state").json()["treasurerEnabled"] is True
        assert c.get("/api/gifts/pool", headers={"X-Treasurer-Token": link}).status_code == 200
        assert c.get("/api/gifts/pool", headers={"X-Treasurer-Token": "x" * 30}).status_code == 403
