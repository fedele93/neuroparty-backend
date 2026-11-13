"""Gioco delle coppie: interruttore nel cruscotto (visibile a tutti nello snapshot) e foto con l'etichetta "coppie"."""
import io

from PIL import Image

from conftest import ADMIN, guest_headers, make_client


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 30, 90)).save(buf, "PNG")
    return buf.getvalue()


def test_pair_game_toggle_and_tagged_photos(tmp_path):
    with make_client(tmp_path) as c:
        U = guest_headers(c, "Ospite Coppia")
        assert c.get("/api/snapshot").json()["pairGameEnabled"] is False  # spento: anche per chi non è registrato
        v0 = c.get("/api/state").json()["version"]
        assert c.put("/api/admin/settings", json={"pairGame": True}, headers=ADMIN).json()["pairGame"] is True
        assert c.get("/api/state").json()["version"] == v0 + 1 and c.get("/api/snapshot").json()["pairGameEnabled"] is True
        assert c.get("/api/admin/dashboard", headers=ADMIN).json()["settings"]["pairGame"] is True
        # foto del gioco: etichetta "coppie" (le altre etichette no); l'etichetta esce nel JSON e nello snapshot
        r = c.post("/api/photos", files={"file": ("c.png", _png(), "image/png")}, data={"caption": "Sale & Pepe", "tag": "coppie"}, headers=U)
        assert r.status_code == 201 and r.json()["tag"] == "coppie" and r.json()["authorName"] == "Ospite Coppia"
        plain = c.post("/api/photos", files={"file": ("d.png", _png(), "image/png")}, data={"caption": "normale"}, headers=U).json()
        assert plain["tag"] == ""
        assert c.post("/api/photos", files={"file": ("e.png", _png(), "image/png")}, data={"tag": "altro"}, headers=U).status_code == 422
        tags = {p["caption"]: p["tag"] for p in c.get("/api/snapshot", headers=U).json()["photos"]}
        assert tags["Sale & Pepe"] == "coppie" and tags["normale"] == ""
        assert c.put("/api/admin/settings", json={"pairGame": False}, headers=ADMIN).json()["pairGame"] is False
