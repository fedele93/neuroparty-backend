import io
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_member
from ..deps import base_url_for, get_db
from ..models import Guest, SharedPhoto
from ..state import bump_version

router = APIRouter(prefix="/api/photos", tags=["foto"])

MAX_SIDE = 1600  # le foto vengono ridimensionate per risparmiare spazio e banda
THUMB_SIDE = 480  # miniatura per la galleria
PAGE_SIZE = 24  # foto per pagina: lo snapshot manda solo le più recenti, le altre a richiesta
MAX_PAGE_SIZE = 60

# Ordine unico della galleria: dalla più recente alla più vecchia (a parità di orario, id decrescente).
NEWEST_FIRST = (SharedPhoto.created_at.desc(), SharedPhoto.id.desc())


def recent_photos(db: Session, limit: int = PAGE_SIZE, before_ts: int | None = None, before_id: int | None = None) -> tuple[list[SharedPhoto], bool]:
    """Una pagina di foto, dalla più recente; con before_ts/before_id quelle più vecchie della foto indicata
    (paginazione a cursore: stabile anche se nel frattempo arrivano foto nuove). Ritorna (foto, ce_ne_sono_altre)."""
    q = select(SharedPhoto)
    if before_ts is not None and before_id is not None:
        q = q.where(or_(SharedPhoto.created_at < before_ts, and_(SharedPhoto.created_at == before_ts, SharedPhoto.id < before_id)))
    rows = db.scalars(q.order_by(*NEWEST_FIRST).limit(limit + 1)).all()
    return list(rows[:limit]), len(rows) > limit


def make_thumbnail(upload_dir: str, image_path: str) -> str:
    """Crea la miniatura accanto alla foto (abc.jpg -> abc_t.jpg) e ne ritorna il percorso pubblico, "" se non riesce."""
    src = os.path.join(upload_dir, os.path.basename(image_path or ""))
    if not image_path or not os.path.exists(src):
        return ""
    name = os.path.splitext(os.path.basename(src))[0] + "_t.jpg"
    try:
        with Image.open(src) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMB_SIDE, THUMB_SIDE))
            img.save(os.path.join(upload_dir, name), "JPEG", quality=78, optimize=True)
    except (UnidentifiedImageError, OSError):
        return ""
    return f"/uploads/{name}"


def ensure_thumbnails(db: Session, upload_dir: str) -> int:
    """All'avvio: crea le miniature mancanti (foto caricate prima di questa versione). Ritorna quante."""
    made = 0
    for p in db.scalars(select(SharedPhoto).where(SharedPhoto.thumb_path == "", SharedPhoto.image_path != "")).all():
        thumb = make_thumbnail(upload_dir, p.image_path)
        if thumb:
            p.thumb_path = thumb
            made += 1
    if made:
        db.commit()
    return made


@router.get("", dependencies=[Depends(require_member)])
def list_photos(request: Request, db: Session = Depends(get_db)):
    """Tutte le foto, dalla più recente (per pochi dati; le app usano /api/photos/page)."""
    base = base_url_for(request)
    rows = db.scalars(select(SharedPhoto).order_by(*NEWEST_FIRST)).all()
    return [p.to_dict(base) for p in rows]


@router.get("/page", dependencies=[Depends(require_member)])
def photos_page(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(default=PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    beforeTs: int | None = Query(default=None),
    beforeId: int | None = Query(default=None),
):
    """Foto a pagine, dalla più recente. Per andare a ritroso si passano createdAt e id dell'ultima foto
    già mostrata (beforeTs, beforeId). Risposta: {photos, hasMore, total}."""
    rows, more = recent_photos(db, limit, beforeTs, beforeId)
    base = base_url_for(request)
    total = int(db.scalar(select(func.count()).select_from(SharedPhoto)) or 0)
    return {"photos": [p.to_dict(base) for p in rows], "hasMore": more, "total": total}


@router.post("", status_code=201)
async def upload_photo(
    request: Request,
    file: UploadFile = File(...),
    authorName: str = Form(default="Invitato"),
    caption: str = Form(default=""),
    db: Session = Depends(get_db),
    member: Guest | None = Depends(require_member),
):
    """Nuova foto. Per gli invitati registrati l'autore è il nome del profilo."""
    settings = request.app.state.settings
    raw = await file.read()
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"Immagine troppo grande (max {settings.max_upload_mb} MB)")
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)  # rispetta la rotazione salvata dal telefono
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "Il file non è un'immagine valida")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    os.makedirs(settings.upload_dir, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    img.save(os.path.join(settings.upload_dir, name), "JPEG", quality=85, optimize=True)
    thumb = make_thumbnail(settings.upload_dir, f"/uploads/{name}")

    photo = SharedPhoto(
        author_name=member.full_name if member is not None else ((authorName or "").strip() or "Invitato"),
        caption=(caption or "").strip(),
        image_path=f"/uploads/{name}",
        thumb_path=thumb,
        likes_count=1,
    )
    db.add(photo)
    bump_version(db)
    db.commit()
    db.refresh(photo)
    request.app.state.webhooks.emit("photo.uploaded", {"photo": photo.to_dict(base_url_for(request))})
    return photo.to_dict(base_url_for(request))


@router.post("/{photo_id}/like", dependencies=[Depends(require_member)])
def like_photo(photo_id: int, request: Request, db: Session = Depends(get_db)):
    photo = db.get(SharedPhoto, photo_id)
    if photo is None:
        raise HTTPException(404, "Foto non trovata")
    photo.likes_count += 1
    bump_version(db)
    db.commit()
    db.refresh(photo)
    return photo.to_dict(base_url_for(request))


@router.delete("/{photo_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_photo(photo_id: int, request: Request, db: Session = Depends(get_db)):
    photo = db.get(SharedPhoto, photo_id)
    if photo is None:
        raise HTTPException(404, "Foto non trovata")
    for rel in (photo.image_path, photo.thumb_path):
        if rel:
            path = os.path.join(request.app.state.settings.upload_dir, os.path.basename(rel))
            if os.path.exists(path):
                os.remove(path)
    db.delete(photo)
    bump_version(db)
    db.commit()
    return None
