"""Assistente vocale con avatar dei neo-specialisti (Mistral: Voxtral + chat + voci clonate).

Pubblico:
- GET  /api/assistant/status            stato (configurato, simulatore, avatar disponibili)
- GET  /api/assistant/avatars           avatar selezionabili (abilitati e con una voce)
- POST /api/assistant/talk              un turno: audio o testo -> risposta scritta + audio + azioni

Organizzatori (X-Admin-Token):
- GET  /api/assistant/admin/avatars                    tutti gli avatar con lo stato di configurazione
- PUT  /api/assistant/admin/avatars/{id}               persona, abilitazione, voce preimpostata
- POST /api/assistant/admin/avatars/{id}/sample        carica il campione vocale e clona la voce su Mistral
- GET  /api/assistant/admin/avatars/{id}/sample        riascolta il campione caricato
- DELETE /api/assistant/admin/avatars/{id}/sample      elimina campione e voce clonata
- POST /api/assistant/admin/avatars/{id}/preview       prova la voce dell'avatar su una frase
- GET  /api/assistant/admin/voices                     voci preimpostate di Mistral
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assistant import SECTIONS, clean_history, run_turn
from ..auth import get_client_id, require_admin
from ..deps import get_db
from ..mistral import MistralError
from ..models import AssistantAvatar, now_ms
from ..state import bump_version

log = logging.getLogger("neuroparty.assistant")
router = APIRouter(prefix="/api/assistant", tags=["assistente"])

MAX_AUDIO_BYTES = 15 * 1024 * 1024
MAX_SAMPLE_BYTES = 10 * 1024 * 1024
AUDIO_EXT = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav",
             "audio/x-wav": "wav", "audio/wave": "wav", "audio/flac": "flac", "audio/aac": "aac", "audio/x-m4a": "m4a"}
PREVIEW_TEXT = "Ciao, sono {name}! Sono felice di festeggiare con te venerdì tredici novembre al Giardino dei Tempi."


def _svc(request: Request):
    return request.app.state.assistant


def _avatars(db: Session) -> list[AssistantAvatar]:
    return db.scalars(select(AssistantAvatar).order_by(AssistantAvatar.sort_order.asc(), AssistantAvatar.id.asc())).all()


def _get_avatar(db: Session, avatar_id: str) -> AssistantAvatar:
    a = db.get(AssistantAvatar, avatar_id)
    if a is None:
        raise HTTPException(404, "Avatar non trovato")
    return a


def _ext_for(upload: UploadFile) -> str:
    mime = (upload.content_type or "").split(";")[0].strip().lower()
    if mime in AUDIO_EXT:
        return AUDIO_EXT[mime]
    ext = os.path.splitext(upload.filename or "")[1].lstrip(".").lower()
    return ext if re.fullmatch(r"[a-z0-9]{1,5}", ext or "") else "webm"


def _speak(request: Request, text: str, avatar: AssistantAvatar) -> tuple[str | None, str | None]:
    """Sintetizza il testo con la voce dell'avatar; in caso di errore la risposta resta solo scritta."""
    svc = _svc(request)
    voice = svc.voices.voice_for(avatar)
    fmt = "wav" if svc.client.fake else "mp3"
    try:
        audio = svc.client.speech(text, voice, fmt)
    except MistralError as e:
        log.warning("Sintesi vocale non riuscita: %s", e)
        return None, None
    return base64.b64encode(audio).decode("ascii"), ("audio/wav" if fmt == "wav" else "audio/mpeg")


# ---------------------------------------------------------------------- pubblico


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db)):
    svc = _svc(request)
    ready = [a for a in _avatars(db) if a.is_ready(svc.voices.fallback_available)] if svc.client.enabled else []
    return {
        "configured": svc.client.enabled,
        "fake": svc.client.fake,
        "chatModel": svc.client.chat_model,
        "avatars": len(ready),
        "sections": SECTIONS,
    }


@router.get("/avatars")
def list_avatars(request: Request, db: Session = Depends(get_db)):
    svc = _svc(request)
    if not svc.client.enabled:
        return []
    fb = svc.voices.fallback_available
    return [a.to_public(fb) for a in _avatars(db) if a.is_ready(fb)]


@router.post("/talk")
async def talk(
    request: Request,
    avatarId: str = Form(...),
    text: str = Form(default=""),
    history: str = Form(default="[]"),
    wantAudio: bool = Form(default=True),
    audio: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    client_id: str | None = Depends(get_client_id),
):
    svc = _svc(request)
    if not svc.client.enabled:
        raise HTTPException(503, "Assistente non configurato sul server (MISTRAL_API_KEY mancante)")
    avatar = _get_avatar(db, avatarId)
    if not avatar.is_ready(svc.voices.fallback_available):
        raise HTTPException(409, "Questo avatar non è ancora disponibile")
    try:
        past = clean_history(json.loads(history or "[]"))
    except json.JSONDecodeError:
        past = []

    transcript = ""
    user_text = (text or "").strip()
    if audio is not None:
        data = await audio.read()
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Registrazione troppo lunga")
        if len(data) < 100:
            raise HTTPException(422, "Registrazione vuota: tieni premuto il pulsante mentre parli")
        filename = f"voce.{_ext_for(audio)}"
        try:
            transcript = svc.client.transcribe(data, filename, audio.content_type or "application/octet-stream", language="it")
        except MistralError as e:
            raise HTTPException(502, f"Trascrizione non riuscita: {e}") from e
        user_text = transcript.strip()
    if not user_text:
        raise HTTPException(422, "Non ho sentito nulla: riprova parlando più vicino al microfono, oppure scrivi il messaggio")

    try:
        turn = run_turn(svc.client, avatar, user_text[:2000], past, request, db, client_id)
    except MistralError as e:
        raise HTTPException(502, f"Assistente non disponibile: {e}") from e

    audio_b64, mime = _speak(request, turn["reply"], avatar) if wantAudio else (None, None)
    return {
        "avatarId": avatar.id,
        "transcript": transcript,
        "userText": user_text,
        "reply": turn["reply"],
        "audio": audio_b64,
        "audioMime": mime,
        "actions": turn["actions"],
        "navigate": turn["navigate"],
        "history": turn["history"],
    }


# ---------------------------------------------------------------------- organizzatori


class AvatarUpdate(BaseModel):
    persona: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    presetVoiceId: str | None = Field(default=None, max_length=128)  # "" = nessuna (usa la voce di riserva)


class PreviewIn(BaseModel):
    text: str = Field(default="", max_length=400)


admin = APIRouter(prefix="/api/assistant/admin", tags=["assistente"], dependencies=[Depends(require_admin)])


@admin.get("/avatars")
def admin_avatars(request: Request, db: Session = Depends(get_db)):
    svc = _svc(request)
    fb = svc.voices.fallback_available
    return {
        "configured": svc.client.enabled,
        "fake": svc.client.fake,
        "fallbackVoiceId": svc.voices.fallback() if svc.client.enabled else None,
        "avatars": [a.to_admin(fb) for a in _avatars(db)],
    }


@admin.put("/avatars/{avatar_id}")
def admin_update_avatar(avatar_id: str, body: AvatarUpdate, request: Request, db: Session = Depends(get_db)):
    a = _get_avatar(db, avatar_id)
    if body.persona is not None:
        a.persona = body.persona.strip()
    if body.enabled is not None:
        a.enabled = body.enabled
    if body.presetVoiceId is not None:
        a.preset_voice_id = body.presetVoiceId.strip() or None
    a.updated_at = now_ms()
    bump_version(db)
    db.commit()
    db.refresh(a)
    return a.to_admin(_svc(request).voices.fallback_available)


@admin.post("/avatars/{avatar_id}/sample", status_code=201)
async def admin_upload_sample(avatar_id: str, request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Salva il campione (pochi secondi di voce pulita) e crea la voce clonata su Mistral."""
    svc = _svc(request)
    if not svc.client.enabled:
        raise HTTPException(503, "MISTRAL_API_KEY non configurata sul server")
    a = _get_avatar(db, avatar_id)
    data = await file.read()
    if len(data) < 1000:
        raise HTTPException(422, "Campione troppo corto o vuoto")
    if len(data) > MAX_SAMPLE_BYTES:
        raise HTTPException(413, "Campione troppo grande (max 10 MB)")
    ext = _ext_for(file)
    filename = f"{a.id}.{ext}"
    try:
        voice = svc.client.create_voice(name=f"NeuroParty {a.short_name}", sample=data, filename=filename, languages=["it"])
    except MistralError as e:
        raise HTTPException(502, f"Clonazione della voce non riuscita: {e}") from e
    old_voice = a.voice_id
    os.makedirs(request.app.state.settings.voices_dir, exist_ok=True)
    # rimuove eventuali campioni precedenti con altra estensione
    for old in os.listdir(request.app.state.settings.voices_dir):
        if old.startswith(a.id + "."):
            os.remove(os.path.join(request.app.state.settings.voices_dir, old))
    with open(os.path.join(request.app.state.settings.voices_dir, filename), "wb") as f:
        f.write(data)
    a.voice_id = voice.get("id")
    a.voice_name = voice.get("name") or ""
    a.sample_filename = filename
    a.sample_uploaded_at = now_ms()
    a.updated_at = now_ms()
    bump_version(db)
    db.commit()
    if old_voice and old_voice != a.voice_id:
        try:
            svc.client.delete_voice(old_voice)
        except MistralError:
            pass
    db.refresh(a)
    return a.to_admin(svc.voices.fallback_available)


@admin.get("/avatars/{avatar_id}/sample")
def admin_get_sample(avatar_id: str, request: Request, db: Session = Depends(get_db)):
    a = _get_avatar(db, avatar_id)
    path = os.path.join(request.app.state.settings.voices_dir, a.sample_filename) if a.sample_filename else ""
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "Nessun campione caricato")
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@admin.delete("/avatars/{avatar_id}/sample", status_code=204)
def admin_delete_sample(avatar_id: str, request: Request, db: Session = Depends(get_db)):
    svc = _svc(request)
    a = _get_avatar(db, avatar_id)
    if a.voice_id:
        try:
            svc.client.delete_voice(a.voice_id)
        except MistralError as e:
            raise HTTPException(502, f"Eliminazione della voce su Mistral non riuscita: {e}") from e
    if a.sample_filename:
        path = os.path.join(request.app.state.settings.voices_dir, a.sample_filename)
        if os.path.isfile(path):
            os.remove(path)
    a.voice_id = None
    a.voice_name = ""
    a.sample_filename = ""
    a.sample_uploaded_at = None
    a.updated_at = now_ms()
    bump_version(db)
    db.commit()
    return None


@admin.post("/avatars/{avatar_id}/preview")
def admin_preview(avatar_id: str, body: PreviewIn, request: Request, db: Session = Depends(get_db)):
    svc = _svc(request)
    if not svc.client.enabled:
        raise HTTPException(503, "MISTRAL_API_KEY non configurata sul server")
    a = _get_avatar(db, avatar_id)
    voice = svc.voices.voice_for(a)
    if not voice:
        raise HTTPException(409, "Nessuna voce disponibile per questo avatar: carica un campione o scegli una voce preimpostata")
    text = body.text.strip() or PREVIEW_TEXT.format(name=a.short_name)
    fmt = "wav" if svc.client.fake else "mp3"
    try:
        audio = svc.client.speech(text, voice, fmt)
    except MistralError as e:
        raise HTTPException(502, f"Sintesi vocale non riuscita: {e}") from e
    return {"text": text, "voiceId": voice, "voiceKind": a.voice_kind(svc.voices.fallback_available),
            "audio": base64.b64encode(audio).decode("ascii"), "audioMime": "audio/wav" if fmt == "wav" else "audio/mpeg"}


@admin.get("/voices")
def admin_voices(request: Request, refresh: bool = False):
    svc = _svc(request)
    if not svc.client.enabled:
        return {"voices": [], "fallbackVoiceId": None}
    return {"voices": svc.voices.presets(refresh=refresh), "fallbackVoiceId": svc.voices.fallback()}
