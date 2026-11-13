"""Download di tutte le foto in uno zip: solo registrati e solo se l'organizzatore l'ha acceso dal cruscotto."""
import io
import zipfile

from PIL import Image

from conftest import ADMIN, guest_headers, make_client


def _png(color=(0, 90, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "PNG")
    return buf.getvalue()


def test_photos_archive_toggle(tmp_path):
    with make_client(tmp_path) as c:
        U = guest_headers(c, "Ospite Foto")
        for i in range(2):
            assert c.post("/api/photos", files={"file": (f"{i}.png", _png((i * 90, 40, 90)), "image/png")}, data={"caption": f"n{i}"}, headers=U).status_code == 201
        # spento di default: lo dice lo snapshot e l'archivio risponde 409 (401 a chi non è registrato)
        assert c.get("/api/snapshot", headers=U).json()["photosDownloadEnabled"] is False
        assert c.get("/api/admin/dashboard", headers=ADMIN).json()["settings"] == {"photosDownload": False, "pairGame": False}
        assert c.get("/api/photos/archive.zip").status_code == 401
        assert c.get("/api/photos/archive.zip", headers=U).status_code == 409
        # l'organizzatore lo accende: la versione dati cambia (i telefoni ricaricano), lo snapshot lo mostra
        v0 = c.get("/api/state").json()["version"]
        assert c.put("/api/admin/settings", json={"photosDownload": True}).status_code == 403
        assert c.put("/api/admin/settings", json={"photosDownload": True}, headers=ADMIN).json() == {"photosDownload": True, "pairGame": False}
        assert c.get("/api/state").json()["version"] == v0 + 1
        assert c.get("/api/snapshot", headers=U).json()["photosDownloadEnabled"] is True
        assert c.get("/api/snapshot").json()["photosDownloadEnabled"] is False  # non registrato: niente foto, niente download
        r = c.get("/api/photos/archive.zip", headers=U)
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip" and "foto-neuroparty.zip" in r.headers["content-disposition"]
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert len(names) == 2 and all(n.endswith(".jpg") for n in names) and all("Ospite-Foto" in n for n in names)  # le 3 foto demo sono segnaposto senza file
        # anche i ruoli (organizzatore) scaricano; spento di nuovo: 409
        assert c.get("/api/photos/archive.zip", headers=ADMIN).status_code == 200
        assert c.put("/api/admin/settings", json={"photosDownload": False}, headers=ADMIN).json() == {"photosDownload": False, "pairGame": False}
        assert c.get("/api/photos/archive.zip", headers=U).status_code == 409
