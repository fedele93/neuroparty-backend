"""Assistente vocale: avatar, conversazione con strumenti, pannello organizzatori.

Con assistant_fake=True il backend usa il simulatore (app/mistral.py) al posto di Mistral;
test_real_client_http_calls verifica invece le chiamate HTTP del client vero con un finto server.
"""
import base64
import json

import httpx
import pytest

from conftest import ADMIN, make_client


@pytest.fixture
def bot(tmp_path):
    with make_client(tmp_path, assistant_fake=True) as c:
        yield c


def test_disabled_without_api_key(client):
    st = client.get("/api/assistant/status").json()
    assert st["configured"] is False and st["avatars"] == 0
    assert client.get("/api/assistant/avatars").json() == []
    assert client.post("/api/assistant/talk", data={"avatarId": "luisi", "text": "ciao"}).status_code == 503
    # il pannello organizzatori funziona lo stesso (per preparare persona e abilitazioni)
    board = client.get("/api/assistant/admin/avatars", headers=ADMIN).json()
    assert board["configured"] is False and len(board["avatars"]) == 9
    assert client.post("/api/assistant/admin/avatars/luisi/preview", json={}, headers=ADMIN).status_code == 503


def test_avatars_only_when_enabled_and_voiced(bot):
    st = bot.get("/api/assistant/status").json()
    assert st["configured"] is True and st["fake"] is True and st["avatars"] == 1
    avatars = bot.get("/api/assistant/avatars").json()
    assert [a["id"] for a in avatars] == ["luisi"]
    assert avatars[0]["shortName"] == "Fedele Luisi" and avatars[0]["voiceKind"] == "preimpostata"
    assert "Fedele" in avatars[0]["persona"]
    # gli altri esistono ma non sono abilitati: non selezionabili
    assert bot.post("/api/assistant/talk", data={"avatarId": "carlone", "text": "ciao"}).status_code == 409
    assert bot.post("/api/assistant/talk", data={"avatarId": "nessuno", "text": "ciao"}).status_code == 404


def test_text_turn_returns_reply_audio_and_history(bot):
    r = bot.post("/api/assistant/talk", data={"avatarId": "luisi", "text": "Quando è la festa?"})
    assert r.status_code == 200
    body = r.json()
    assert "13 novembre" in body["reply"] and body["transcript"] == ""
    assert body["audioMime"] == "audio/wav" and base64.b64decode(body["audio"]).startswith(b"RIFF")
    assert body["actions"] == [] and body["navigate"] is None
    assert [m["role"] for m in body["history"]] == ["user", "assistant"]
    # secondo turno con la cronologia; senza audio se non richiesto
    r2 = bot.post("/api/assistant/talk", data={"avatarId": "luisi", "text": "grazie", "history": json.dumps(body["history"]), "wantAudio": "false"})
    assert r2.status_code == 200 and r2.json()["audio"] is None and len(r2.json()["history"]) == 4


def test_audio_turn_is_transcribed(bot):
    fake_audio = b"\x1aE\xdf\xa3" + b"\x00" * 500
    r = bot.post("/api/assistant/talk", data={"avatarId": "luisi"}, files={"audio": ("voce.webm", fake_audio, "audio/webm")})
    assert r.status_code == 200
    assert r.json()["transcript"].startswith("Trascrizione simulata") and r.json()["userText"] == r.json()["transcript"]
    assert bot.post("/api/assistant/talk", data={"avatarId": "luisi"}, files={"audio": ("voce.webm", b"x", "audio/webm")}).status_code == 422
    assert bot.post("/api/assistant/talk", data={"avatarId": "luisi", "text": "   "}).status_code == 422


def test_tools_read_and_write_through_the_app(bot):
    # lettura: posti navetta (strumento get_bus_availability)
    r = bot.post("/api/assistant/talk", data={"avatarId": "luisi", "text": "Quanti posti ci sono sulla navetta?"}).json()
    assert r["actions"][0]["tool"] == "get_bus_availability" and r["actions"][0]["ok"]
    assert "44 posti liberi" in r["reply"]  # 54 - 10 demo
    # scrittura: prenotazione (il simulatore chiama book_bus dopo la "conferma")
    before = bot.get("/api/bus/summary").json()["bookedSeats"]
    r = bot.post("/api/assistant/talk", data={"avatarId": "luisi", "text": "Conferma prenotazione navetta per Nonna Lina"},
                 headers={"X-Client-Id": "tel-1"}).json()
    assert r["actions"][0]["tool"] == "book_bus" and r["actions"][0]["ok"]
    assert "Nonna Lina" in r["actions"][0]["summary"] and r["reply"].startswith("Fatto!")
    assert bot.get("/api/bus/summary").json()["bookedSeats"] == before + 2
    booking = next(b for b in bot.get("/api/bus/bookings").json() if b["passengerName"] == "Nonna Lina")
    assert bot.delete(f"/api/bus/bookings/{booking['id']}", headers={"X-Client-Id": "tel-1"}).status_code == 204  # proprietà del dispositivo
    # navigazione
    r = bot.post("/api/assistant/talk", data={"avatarId": "luisi", "text": "Apri la sezione navetta"}).json()
    assert r["navigate"] == "bus" and r["actions"][0]["tool"] == "open_section"


def test_execute_tool_rsvp_wish_and_errors(bot):
    from app.assistant import execute_tool
    from fastapi import Request

    app = bot.app
    scope = {"type": "http", "app": app, "headers": [], "method": "POST", "path": "/", "query_string": b"", "client": ("test", 0), "server": ("test", 80), "scheme": "http"}
    request = Request(scope)
    with app.state.session_factory() as db:
        out = execute_tool("set_rsvp", {"fullName": "Zia Pina", "status": "CONFIRMED", "guestsCount": 3, "dietaryNotes": "vegetariana"}, request, db, "dev-1")
        assert out["created"] is True and out["guest"]["guestsCount"] == 3
        out = execute_tool("set_rsvp", {"fullName": "zia pina", "status": "DECLINED", "guestsCount": 0}, request, db, "dev-1")
        assert out["created"] is False and out["guest"]["rsvpStatus"] == "DECLINED"
        assert execute_tool("find_guest", {"name": "pina"}, request, db, None)["count"] == 1
        out = execute_tool("post_wish", {"authorName": "Zia Pina", "message": "Bravi tutti!"}, request, db, None)
        assert out["navigate"] == "wishes" and out["wish"]["targetGraduate"] == "Tutti i Laureandi"
        assert "error" in execute_tool("book_bus", {"passengerName": "Troppi", "seatsCount": 60}, request, db, None)
        assert "error" in execute_tool("book_bus", {"passengerName": "", "seatsCount": 1}, request, db, None)
        assert "error" in execute_tool("open_section", {"section": "boh"}, request, db, None)
        assert "error" in execute_tool("set_rsvp", {"fullName": "", "status": "CONFIRMED"}, request, db, None)
    assert len(bot.get("/api/wishes").json()) == 11


def test_system_prompt_contains_event_facts(bot):
    from app.assistant import system_prompt
    from app.models import AssistantAvatar

    with bot.app.state.session_factory() as db:
        avatar = db.get(AssistantAvatar, "luisi")
        prompt = system_prompt(avatar, bot.app.state.event_data, 54)
    assert "Sei Dott. Fedele Luisi" in prompt and "in prima persona come Fedele Luisi" in prompt
    assert "venerdì 13 novembre 2026" in prompt and "21:30" in prompt and "Giardino dei Tempi" in prompt
    assert "Paolo Roberto" in prompt and "cifre suggerite" in prompt
    assert "Stazione Ferroviaria Centrale di Bari" in prompt


def test_admin_configures_avatars_and_clones_voice(bot):
    assert bot.get("/api/assistant/admin/avatars").status_code == 403
    board = bot.get("/api/assistant/admin/avatars", headers=ADMIN).json()
    assert board["fallbackVoiceId"] == "preset-alba"  # prima voce preimpostata "italiana"
    carlone = next(a for a in board["avatars"] if a["id"] == "carlone")
    assert carlone["enabled"] is False and carlone["ready"] is False and carlone["voiceKind"] == "preimpostata"

    # voci preimpostate
    voices = bot.get("/api/assistant/admin/voices", headers=ADMIN).json()["voices"]
    assert [v["id"] for v in voices] == ["preset-alba", "preset-marco"]

    # campione vocale -> voce clonata
    sample = b"RIFF" + b"\x00" * 5000
    r = bot.post("/api/assistant/admin/avatars/carlone/sample", files={"file": ("sebastiano.wav", sample, "audio/wav")}, headers=ADMIN)
    assert r.status_code == 201
    a = r.json()
    assert a["voiceKind"] == "clonata" and a["voiceId"].startswith("voice-") and a["sampleFilename"] == "carlone.wav"
    assert a["ready"] is False  # ancora disabilitato
    assert bot.get("/api/assistant/admin/avatars/carlone/sample", headers=ADMIN).status_code == 200
    assert ("create_voice", {"name": "NeuroParty Sebastiano Carlone", "bytes": len(sample)}) in bot.app.state.assistant.client.calls

    # abilitazione + persona + voce preimpostata per un altro
    r = bot.put("/api/assistant/admin/avatars/carlone", json={"enabled": True, "persona": "Sei Sebastiano."}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["ready"] is True
    ids = [x["id"] for x in bot.get("/api/assistant/avatars").json()]
    assert ids == ["luisi", "carlone"]
    r = bot.put("/api/assistant/admin/avatars/totaro", json={"presetVoiceId": "preset-marco", "enabled": True}, headers=ADMIN)
    assert r.json()["voiceKind"] == "preimpostata" and r.json()["presetVoiceId"] == "preset-marco"

    # anteprima della voce
    r = bot.post("/api/assistant/admin/avatars/carlone/preview", json={}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["voiceKind"] == "clonata" and "Sebastiano Carlone" in r.json()["text"]
    assert r.json()["voiceId"] == a["voiceId"]
    # la conversazione usa la voce clonata
    bot.post("/api/assistant/talk", data={"avatarId": "carlone", "text": "ciao"})
    assert ("speech", {"chars": bot.app.state.assistant.client.calls[-1][1]["chars"], "voice": a["voiceId"]}) == bot.app.state.assistant.client.calls[-1]

    # ricaricare un campione sostituisce la voce e cancella la vecchia su Mistral
    r = bot.post("/api/assistant/admin/avatars/carlone/sample", files={"file": ("nuovo.m4a", sample, "audio/mp4")}, headers=ADMIN)
    assert r.json()["sampleFilename"] == "carlone.m4a" and r.json()["voiceId"] != a["voiceId"]
    assert ("delete_voice", {"id": a["voiceId"]}) in bot.app.state.assistant.client.calls
    assert bot.get("/api/assistant/admin/avatars/carlone/sample", headers=ADMIN).status_code == 200

    # eliminazione: torna alla voce preimpostata
    assert bot.delete("/api/assistant/admin/avatars/carlone/sample", headers=ADMIN).status_code == 204
    board = bot.get("/api/assistant/admin/avatars", headers=ADMIN).json()
    carlone = next(x for x in board["avatars"] if x["id"] == "carlone")
    assert carlone["voiceId"] is None and carlone["voiceKind"] == "preimpostata" and carlone["ready"] is True
    assert bot.get("/api/assistant/admin/avatars/carlone/sample", headers=ADMIN).status_code == 404
    assert bot.post("/api/assistant/admin/avatars/carlone/sample", files={"file": ("x.wav", b"abc", "audio/wav")}, headers=ADMIN).status_code == 422


def test_avatar_settings_survive_restart(tmp_path):
    with make_client(tmp_path, assistant_fake=True) as c1:
        c1.put("/api/assistant/admin/avatars/regina", json={"enabled": True, "persona": "Sei Donato."}, headers=ADMIN)
    with make_client(tmp_path, assistant_fake=True) as c2:
        regina = next(a for a in c2.get("/api/assistant/admin/avatars", headers=ADMIN).json()["avatars"] if a["id"] == "regina")
        assert regina["enabled"] is True and regina["persona"] == "Sei Donato."


class FakeMistralServer:
    """Finto api.mistral.ai per verificare le richieste HTTP del client vero."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path == "/v1/audio/transcriptions":
            return httpx.Response(200, json={"model": "voxtral-mini-latest", "text": " Ciao Fedele, a che ora inizia la festa? ", "language": "it", "usage": {}})
        if path == "/v1/chat/completions":
            body = json.loads(req.content)
            if body["messages"][-1]["role"] == "tool":
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Ci sono ancora posti liberi!"}}]})
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_bus_availability", "arguments": "{}"}}]}}]})
        if path == "/v1/audio/speech":
            return httpx.Response(200, json={"audio_data": base64.b64encode(b"ID3mp3bytes").decode()})
        if path == "/v2/audio/voices":
            return httpx.Response(200, json={"result": {"data": [{"id": "vx-1", "name": "Giulia", "gender": "female", "languages": ["it"]}], "next_page_token": None}})
        if path == "/v1/audio/voices" and req.method == "POST":
            return httpx.Response(200, json={"id": "custom-9", "name": json.loads(req.content)["name"], "type": "custom"})
        if path.startswith("/v1/audio/voices/") and req.method == "DELETE":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1]})
        return httpx.Response(404, json={"message": "not found"})


def test_real_client_http_calls(tmp_path):
    server = FakeMistralServer()
    with make_client(tmp_path, mistral_api_key="sk-test") as c:
        c.app.state.assistant.client.client = httpx.Client(transport=httpx.MockTransport(server))
        assert c.get("/api/assistant/status").json() == {**c.get("/api/assistant/status").json(), "configured": True, "fake": False, "avatars": 1}
        r = c.post("/api/assistant/talk", data={"avatarId": "luisi"}, files={"audio": ("v.webm", b"\x00" * 300, "audio/webm")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["transcript"] == "Ciao Fedele, a che ora inizia la festa?"
        assert body["reply"] == "Ci sono ancora posti liberi!" and body["actions"][0]["tool"] == "get_bus_availability"
        assert body["audioMime"] == "audio/mpeg" and base64.b64decode(body["audio"]) == b"ID3mp3bytes"

        paths = [(q.method, q.url.path) for q in server.requests]
        assert paths[0] == ("GET", "/v2/audio/voices")  # voce di riserva letta una volta sola
        assert ("POST", "/v1/audio/transcriptions") in paths and paths.count(("POST", "/v1/chat/completions")) == 2
        assert paths[-1] == ("POST", "/v1/audio/speech")
        for q in server.requests:
            assert q.headers["authorization"] == "Bearer sk-test"
        stt = next(q for q in server.requests if q.url.path == "/v1/audio/transcriptions")
        assert b'name="model"' in stt.content and b"voxtral-mini-latest" in stt.content and b'name="language"' in stt.content
        chat = json.loads(next(q for q in server.requests if q.url.path == "/v1/chat/completions").content)
        assert chat["model"] == "mistral-small-latest" and chat["tool_choice"] == "auto"
        assert {t["function"]["name"] for t in chat["tools"]} >= {"book_bus", "set_rsvp", "post_wish", "open_section"}
        assert chat["messages"][0]["role"] == "system" and "Giardino dei Tempi" in chat["messages"][0]["content"]
        tts = json.loads(server.requests[-1].content)
        assert tts == {"model": "voxtral-mini-tts-2603", "input": "Ci sono ancora posti liberi!", "response_format": "mp3", "stream": False, "voice_id": "vx-1"}

        # clonazione: campione in base64 con nome file, poi cancellazione
        r = c.post("/api/assistant/admin/avatars/luisi/sample", files={"file": ("fedele.wav", b"RIFF" + b"\x01" * 2000, "audio/wav")}, headers=ADMIN)
        assert r.status_code == 201 and r.json()["voiceId"] == "custom-9"
        created = json.loads(next(q for q in server.requests if q.url.path == "/v1/audio/voices" and q.method == "POST").content)
        assert created["sample_filename"] == "luisi.wav" and created["languages"] == ["it"]
        assert base64.b64decode(created["sample_audio"]).startswith(b"RIFF")
        assert c.delete("/api/assistant/admin/avatars/luisi/sample", headers=ADMIN).status_code == 204
        assert ("DELETE", "/v1/audio/voices/custom-9") in [(q.method, q.url.path) for q in server.requests]

        # errore di Mistral -> messaggio leggibile
        c.app.state.assistant.client.client = httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(429, json={"message": "rate limit"})))
        r = c.post("/api/assistant/talk", data={"avatarId": "luisi", "text": "ciao"})
        assert r.status_code == 502 and "429" in r.json()["detail"]
