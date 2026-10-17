import io
import os
import threading
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import and_, func, or_, select
from starlette.concurrency import run_in_threadpool
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


def photos_total(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(SharedPhoto)) or 0)


def _upload_file(upload_dir: str, rel: str) -> str:
    return os.path.join(upload_dir, os.path.basename(rel or ""))


def make_thumbnail(upload_dir: str, image_path: str, img: Image.Image | None = None) -> str:
    """Crea la miniatura accanto alla foto (abc.jpg -> abc_t.jpg) e ne ritorna il percorso pubblico, "" se non riesce.
    Con [img] (immagine già decodificata, es. all'upload) non rilegge il file da disco."""
    src = _upload_file(upload_dir, image_path)
    if not image_path or (img is None and not os.path.exists(src)):
        return ""
    name = os.path.splitext(os.path.basename(src))[0] + "_t.jpg"
    try:
        thumb = img.copy() if img is not None else Image.open(src)
        with thumb:
            thumb = thumb.convert("RGB")
            thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))
            thumb.save(os.path.join(upload_dir, name), "JPEG", quality=78, optimize=True)
    except (UnidentifiedImageError, OSError):
        return ""
    return f"/uploads/{name}"


def remove_photo_files(upload_dir: str, photo: SharedPhoto) -> None:
    """Cancella dal disco la foto e la sua miniatura (se ci sono)."""
    for rel in (photo.image_path, photo.thumb_path):
        path = _upload_file(upload_dir, rel)
        if rel and os.path.exists(path):
            os.remove(path)


def _process_upload(raw: bytes, upload_dir: str) -> tuple[str, str]:
    """Decodifica, ridimensiona e salva foto e miniatura (lavoro CPU: va eseguito fuori dall'event loop).
    Ritorna (percorso foto, percorso miniatura); ValueError se il file non è un'immagine."""
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)  # rispetta la rotazione salvata dal telefono
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("Il file non è un'immagine valida") from e
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    os.makedirs(upload_dir, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    img.save(os.path.join(upload_dir, name), "JPEG", quality=85, optimize=True)
    image_path = f"/uploads/{name}"
    return image_path, make_thumbnail(upload_dir, image_path, img=img)


def ensure_thumbnails(db: Session, upload_dir: str) -> int:
    """Crea le miniature mancanti (foto caricate prima di questa versione). Ritorna quante."""
    made = 0
    for p in db.scalars(select(SharedPhoto).where(SharedPhoto.thumb_path == "", SharedPhoto.image_path != "")).all():
        thumb = make_thumbnail(upload_dir, p.image_path)
        if thumb:
            p.thumb_path = thumb
            made += 1
    if made:
        db.commit()
    return made


def ensure_thumbnails_in_background(session_factory, upload_dir: str, log) -> None:
    """All'avvio: se ci sono foto senza miniatura le crea in un thread, senza ritardare le prime richieste
    (nel frattempo la galleria usa la foto intera)."""
    with session_factory() as db:
        missing = db.scalar(select(func.count()).select_from(SharedPhoto).where(SharedPhoto.thumb_path == "", SharedPhoto.image_path != "")) or 0
    if not missing:
        return

    def work() -> None:
        with session_factory() as db:
            log.info("Create %d miniature per le foto già caricate", ensure_thumbnails(db, upload_dir))

    threading.Thread(target=work, name="thumbnails", daemon=True).start()


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
    return {"photos": [p.to_dict(base) for p in rows], "hasMore": more, "total": photos_total(db)}


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
        # decodifica e ridimensionamento in un thread: il server continua a rispondere agli altri
        image_path, thumb = await run_in_threadpool(_process_upload, raw, settings.upload_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))

    photo = SharedPhoto(
        author_name=member.full_name if member is not None else ((authorName or "").strip() or "Invitato"),
        caption=(caption or "").strip(),
        image_path=image_path,
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
    remove_photo_files(request.app.state.settings.upload_dir, photo)
    db.delete(photo)
    bump_version(db)
    db.commit()
    return None
