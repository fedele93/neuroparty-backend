"""Commenti alle foto: solo registrati, autore deciso dal server, cancellazione dell'autore o degli organizzatori,
anteprima nella galleria e nello snapshot, cascata con la foto e con l'azzeramento dei dati di prova."""
import io
import time

from PIL import Image

from conftest import ADMIN, TREASURER, guest_headers, make_client
from test_automation import N8N_URL, FakeN8n


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (0, 90, 200)).save(buf, "PNG")
    return buf.getvalue()


def test_photo_comments(tmp_path):
    n8n = FakeN8n()
    with make_client(tmp_path, webhook_url=N8N_URL, webhook_secret="shh", webhook_handler=n8n,
                     graduate_tokens={"luisi": "grad-luisi"}) as c:
        U1, U2, G = guest_headers(c, "Ospite Uno"), guest_headers(c, "Ospite Due"), {"X-Graduate-Token": "grad-luisi"}
        photo = c.post("/api/photos", files={"file": ("a.png", _png(), "image/png")}, data={"caption": "Brindisi"}, headers=ADMIN).json()
        pid = photo["id"]
        assert photo["commentsCount"] == 0 and photo["lastComments"] == []
        # solo registrati
        assert c.get(f"/api/photos/{pid}/comments").status_code == 401
        assert c.post(f"/api/photos/{pid}/comments", json={"text": "ciao"}).status_code == 401
        assert c.post(f"/api/photos/{pid}/comments", json={"text": "ciao"}, headers={"X-Client-Id": "tel"}).status_code == 401
        # l'autore lo decide il server: nome del profilo, festeggiato, cassiere, "Organizzatori"
        r = c.post(f"/api/photos/{pid}/comments", json={"text": "  Che   bella foto! "}, headers=U1)
        assert r.status_code == 201
        c1 = r.json()["comment"]
        assert c1["authorName"] == "Ospite Uno" and c1["authorKind"] == "guest" and c1["text"] == "Che bella foto!" and c1["mine"] is True
        assert r.json()["photo"]["commentsCount"] == 1 and r.json()["photo"]["lastComments"][0]["id"] == c1["id"]
        assert n8n.events[-1] == "photo.commented" and n8n.payload(-1)["data"]["comment"]["text"] == "Che bella foto!"
        c2 = c.post(f"/api/photos/{pid}/comments", json={"text": "Auguri!"}, headers=G).json()["comment"]
        assert c2["authorName"] == "Fedele Luisi" and c2["authorKind"] == "graduate"
        c3 = c.post(f"/api/photos/{pid}/comments", json={"text": "Grande"}, headers=TREASURER).json()["comment"]
        assert c3["authorKind"] == "treasurer" and c3["authorName"] == "Cassieri"  # token globale; con TREASURER_TOKEN_<ID> il nome del cassiere
        c4 = c.post(f"/api/photos/{pid}/comments", json={"text": "Bravi tutti"}, headers=ADMIN).json()["comment"]
        assert c4["authorName"] == "Organizzatori" and c4["authorKind"] == "admin"
        # validazione e anti-spam (un commento ogni 5 s per autore)
        assert c.post(f"/api/photos/{pid}/comments", json={"text": "   "}, headers=U2).status_code == 422
        assert c.post(f"/api/photos/{pid}/comments", json={"text": "x" * 301}, headers=U2).status_code == 422
        assert c.post(f"/api/photos/{pid}/comments", json={"text": "di nuovo"}, headers=U1).status_code == 429
        assert c.post("/api/photos/9999/comments", json={"text": "ciao"}, headers=U1).status_code == 404
        # elenco dal più vecchio, "mine" secondo chi guarda; anteprima = ultimi 2
        rows = c.get(f"/api/photos/{pid}/comments", headers=U2).json()
        assert [x["text"] for x in rows] == ["Che bella foto!", "Auguri!", "Grande", "Bravi tutti"] and not any(x["mine"] for x in rows)
        assert [x["mine"] for x in c.get(f"/api/photos/{pid}/comments", headers=U1).json()] == [True, False, False, False]
        assert all(x["mine"] for x in c.get(f"/api/photos/{pid}/comments", headers=ADMIN).json())
        gal = next(p for p in c.get("/api/photos", headers=U2).json() if p["id"] == pid)
        assert gal["commentsCount"] == 4 and [x["text"] for x in gal["lastComments"]] == ["Grande", "Bravi tutti"]
        snap = next(p for p in c.get("/api/snapshot", headers=U2).json()["photos"] if p["id"] == pid)
        assert snap["commentsCount"] == 4 and len(snap["lastComments"]) == 2
        # cancellazione: solo il proprio (o l'organizzatore)
        assert c.delete(f"/api/photos/{pid}/comments/{c1['id']}", headers=U2).status_code == 403
        assert c.delete(f"/api/photos/{pid}/comments/{c2['id']}", headers=U1).status_code == 403
        assert c.delete(f"/api/photos/{pid}/comments/{c1['id']}").status_code == 401
        assert c.delete(f"/api/photos/{pid}/comments/{c1['id']}", headers=U1).status_code == 204
        assert c.delete(f"/api/photos/{pid}/comments/{c2['id']}", headers=G).status_code == 204  # il festeggiato cancella il suo
        assert c.delete(f"/api/photos/{pid}/comments/{c3['id']}", headers=ADMIN).status_code == 204  # l'organizzatore qualsiasi
        assert c.delete(f"/api/photos/{pid}/comments/{c3['id']}", headers=ADMIN).status_code == 404
        assert c.get(f"/api/photos/{pid}/comments", headers=U1).json()[0]["id"] == c4["id"]
        # cascata: via la foto, via i commenti; l'azzeramento dei dati di prova li conta
        from app.models import PhotoComment
        assert c.delete(f"/api/photos/{pid}", headers=ADMIN).status_code == 204
        with c.app.state.session_factory() as db:
            assert db.query(PhotoComment).count() == 0
        p2 = c.post("/api/photos", files={"file": ("b.png", _png(), "image/png")}, headers=ADMIN).json()
        c.post(f"/api/photos/{p2['id']}/comments", json={"text": "ultimo"}, headers=U2)
        assert c.post("/api/admin/reset-test-data", json={"confirm": "AZZERA"}, headers=ADMIN).json()["deleted"]["photoComments"] == 1
