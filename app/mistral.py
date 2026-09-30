"""Client minimale per le API Mistral usate dall'assistente vocale (httpx, sincrono).

- trascrizione: POST /v1/audio/transcriptions (Voxtral)
- conversazione: POST /v1/chat/completions con function calling
- sintesi vocale: POST /v1/audio/speech (voci preimpostate o clonate)
- voci clonate: POST/DELETE /v1/audio/voices, elenco GET /v2/audio/voices

FakeMistralClient simula tutto in locale (ASSISTANT_FAKE=true): utile per provare la PWA senza
chiave e senza costi, e per i test automatici.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import struct
import uuid

import httpx

log = logging.getLogger("neuroparty.mistral")


class MistralError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class MistralClient:
    def __init__(self, api_key: str, base_url: str, chat_model: str, stt_model: str, tts_model: str, timeout_s: float = 90):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.stt_model = stt_model
        self.tts_model = tts_model
        self.client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=15))
        self.fake = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------ http helpers

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "User-Agent": "NeuroParty-Assistant/1.0"}

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.enabled:
            raise MistralError("MISTRAL_API_KEY non configurata sul server", 503)
        try:
            res = self.client.request(method, f"{self.base_url}{path}", headers=self._headers(), **kwargs)
        except httpx.HTTPError as e:  # rete, timeout
            raise MistralError(f"Mistral non raggiungibile: {e.__class__.__name__}", 502) from e
        if res.status_code >= 400:
            detail = ""
            try:
                body = res.json()
                detail = body.get("message") or body.get("detail") or body.get("error") or ""
                if isinstance(detail, (dict, list)):
                    detail = json.dumps(detail)[:300]
            except Exception:  # noqa: BLE001
                detail = res.text[:300]
            log.warning("Mistral %s %s -> %s %s", method, path, res.status_code, detail)
            raise MistralError(f"Errore Mistral ({res.status_code}): {detail or 'richiesta rifiutata'}", res.status_code)
        return res

    # ------------------------------------------------------------------ API

    def transcribe(self, audio: bytes, filename: str, mime: str, language: str = "it") -> str:
        res = self._request(
            "POST", "/v1/audio/transcriptions",
            data={"model": self.stt_model, "language": language},
            files={"file": (filename, audio, mime or "application/octet-stream")},
        )
        return (res.json().get("text") or "").strip()

    def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.3, max_tokens: int = 500) -> dict:
        """Ritorna il messaggio dell'assistente (content + eventuali tool_calls)."""
        body: dict = {"model": self.chat_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        res = self._request("POST", "/v1/chat/completions", json=body)
        choices = res.json().get("choices") or []
        if not choices:
            raise MistralError("Risposta vuota dal modello", 502)
        return choices[0].get("message") or {}

    def speech(self, text: str, voice_id: str | None, fmt: str = "mp3") -> bytes:
        body: dict = {"model": self.tts_model, "input": text, "response_format": fmt, "stream": False}
        if voice_id:
            body["voice_id"] = voice_id
        res = self._request("POST", "/v1/audio/speech", json=body)
        ctype = res.headers.get("content-type", "")
        if "json" in ctype:
            data = res.json().get("audio_data") or ""
            return base64.b64decode(data)
        return res.content

    def create_voice(self, name: str, sample: bytes, filename: str, languages: list[str] | None = None) -> dict:
        body = {
            "name": name,
            "sample_audio": base64.b64encode(sample).decode("ascii"),
            "sample_filename": filename,
            "languages": languages or ["it"],
        }
        res = self._request("POST", "/v1/audio/voices", json=body)
        return res.json()

    def delete_voice(self, voice_id: str) -> None:
        try:
            self._request("DELETE", f"/v1/audio/voices/{voice_id}")
        except MistralError as e:
            if e.status != 404:
                raise

    def list_preset_voices(self) -> list[dict]:
        res = self._request("GET", "/v2/audio/voices", params={"type": "preset", "page_size": 100})
        data = (res.json().get("result") or {}).get("data") or res.json().get("data") or []
        return [
            {"id": v.get("id"), "name": v.get("name"), "gender": v.get("gender"), "languages": v.get("languages") or [],
             "description": v.get("description") or ""}
            for v in data if v.get("id")
        ]


# ---------------------------------------------------------------------- simulatore


def silent_wav(seconds: float = 0.3, rate: int = 8000) -> bytes:
    """Un WAV di silenzio: risposta audio valida del simulatore."""
    frames = int(seconds * rate)
    buf = io.BytesIO()
    data_size = frames * 2
    buf.write(b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE")
    buf.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
    buf.write(b"data" + struct.pack("<I", data_size) + b"\x00" * data_size)
    return buf.getvalue()


class FakeMistralClient(MistralClient):
    """Risposte simulate, senza rete: capisce qualche parola chiave e usa gli strumenti."""

    def __init__(self, chat_model: str = "fake-chat", stt_model: str = "fake-stt", tts_model: str = "fake-tts"):
        super().__init__(api_key="fake", base_url="http://fake.invalid", chat_model=chat_model, stt_model=stt_model, tts_model=tts_model)
        self.fake = True
        self.voices: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []

    def transcribe(self, audio: bytes, filename: str, mime: str, language: str = "it") -> str:
        self.calls.append(("transcribe", {"bytes": len(audio), "filename": filename}))
        return f"Trascrizione simulata di {len(audio)} byte"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.3, max_tokens: int = 500) -> dict:
        self.calls.append(("chat", {"messages": len(messages)}))
        last = messages[-1]
        if last.get("role") == "tool":
            result = json.loads(last.get("content") or "{}")
            if "availableSeats" in result:
                return {"role": "assistant", "content": f"Sulla navetta restano {result['availableSeats']} posti liberi su {result['maxSeats']}."}
            if result.get("navigate"):
                return {"role": "assistant", "content": "Ecco, ti ho aperto la sezione."}
            if result.get("error"):
                return {"role": "assistant", "content": f"Non ci sono riuscito: {result['error']}"}
            return {"role": "assistant", "content": "Fatto! " + (result.get("summary") or "")}
        text = (last.get("content") or "").lower()
        tool_names = {t["function"]["name"] for t in (tools or [])}

        def call(name: str, args: dict) -> dict:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_" + uuid.uuid4().hex[:8], "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            ]}

        if "posti" in text and "get_bus_availability" in tool_names:
            return call("get_bus_availability", {})
        if text.startswith("apri ") and "open_section" in tool_names:
            for key, section in (("programma", "program"), ("invitat", "rsvp"), ("navetta", "bus"), ("augur", "wishes"), ("regal", "gifts")):
                if key in text:
                    return call("open_section", {"section": section})
        if text.startswith("conferma prenotazione navetta per ") and "book_bus" in tool_names:
            name = last["content"][len("conferma prenotazione navetta per "):].strip().rstrip(".")
            return call("book_bus", {"passengerName": name, "seatsCount": 2, "pickupStop": "", "returnTripWanted": True})
        if "festa" in text or "quando" in text:
            return {"role": "assistant", "content": "La festa è venerdì 13 novembre al Giardino dei Tempi: ti aspetto!"}
        return {"role": "assistant", "content": f"Hai detto: \"{last.get('content', '')}\". Sono la versione simulata dell'assistente."}

    def speech(self, text: str, voice_id: str | None, fmt: str = "mp3") -> bytes:
        self.calls.append(("speech", {"chars": len(text), "voice": voice_id}))
        return silent_wav()

    def create_voice(self, name: str, sample: bytes, filename: str, languages: list[str] | None = None) -> dict:
        vid = "voice-" + uuid.uuid4().hex[:8]
        self.voices[vid] = {"id": vid, "name": name, "type": "custom"}
        self.calls.append(("create_voice", {"name": name, "bytes": len(sample)}))
        return self.voices[vid]

    def delete_voice(self, voice_id: str) -> None:
        self.voices.pop(voice_id, None)
        self.calls.append(("delete_voice", {"id": voice_id}))

    def list_preset_voices(self) -> list[dict]:
        return [
            {"id": "preset-alba", "name": "Alba", "gender": "female", "languages": ["it", "en"], "description": "Voce femminile simulata"},
            {"id": "preset-marco", "name": "Marco", "gender": "male", "languages": ["it", "en"], "description": "Voce maschile simulata"},
        ]
