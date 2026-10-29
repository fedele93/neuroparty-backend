import io
import os
import threading
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import and_, delete, func, or_, select
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..auth import graduate_for_token, is_admin, require_admin, require_member, treasurer_scope
from ..deps import base_url_for, get_db
from ..models import GiftTarget, Guest, PhotoComment, SharedPhoto, now_ms
from ..schemas import PhotoCommentIn
from ..seed import gift_collectors_info
from ..state import bump_version

router = APIRouter(prefix="/api/photos", tags=["foto"])

MAX_SIDE = 1600  # le foto vengono ridimensionate per risparmiare spazio e banda
THUMB_SIDE = 480  # miniatura per la galleria
PAGE_SIZE = 24  # foto per pagina: lo snapshot manda solo le più recenti, le altre a richiesta
MAX_PAGE_SIZE = 60
PREVIEW_COMMENTS = 2  # ultimi commenti mostrati nella card della galleria; il resto si chiede aprendo la foto
COMMENT_MIN_INTERVAL_MS = 5_000  # anti doppio tocco / spam: un commento ogni 5 s per autore

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


def with_comments(db: Session, photos: list[SharedPhoto], base_url: str) -> list[dict]:
    """Le foto come JSON con il numero di commenti e gli ultimi PREVIEW_COMMENTS (anteprima nella card).
    Una query sola per tutte le foto: lo snapshot ne porta 24 alla volta."""
    ids = [p.id for p in photos]
    by_photo: dict[int, list[PhotoComment]] = {i: [] for i in ids}
    if ids:
        for c in db.scalars(select(PhotoComment).where(PhotoComment.photo_id.in_(ids)).order_by(PhotoComment.created_at.asc(), PhotoComment.id.asc())).all():
            by_photo.setdefault(c.photo_id, []).append(c)
    out = []
    for p in photos:
        cs = by_photo.get(p.id, [])
        out.append({**p.to_dict(base_url), "commentsCount": len(cs), "lastComments": [c.to_dict() for c in cs[-PREVIEW_COMMENTS:]]})
    return out


def photo_dict(db: Session, photo: SharedPhoto, base_url: str) -> dict:
    return with_comments(db, [photo], base_url)[0]


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


def ensure_thumbnails_in_background(session_factory, upload_dir: str, log) -> threading.Thread | None:
    """All'avvio: se ci sono foto senza miniatura le crea in un thread, senza ritardare le prime richieste
    (nel frattempo la galleria usa la foto intera). Ritorna il thread, None se non c'è nulla da fare."""
    with session_factory() as db:
        missing = db.scalar(select(func.count()).select_from(SharedPhoto).where(SharedPhoto.thumb_path == "", SharedPhoto.image_path != "")) or 0
    if not missing:
        return None

    def work() -> None:
        with session_factory() as db:
            log.info("Create %d miniature per le foto già caricate", ensure_thumbnails(db, upload_dir))

    thread = threading.Thread(target=work, name="thumbnails", daemon=True)
    thread.start()
    return thread


@router.get("", dependencies=[Depends(require_member)])
def list_photos(request: Request, db: Session = Depends(get_db)):
    """Tutte le foto, dalla più recente (per pochi dati; le app usano /api/photos/page)."""
    base = base_url_for(request)
    rows = db.scalars(select(SharedPhoto).order_by(*NEWEST_FIRST)).all()
    return with_comments(db, rows, base)


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
    return {"photos": with_comments(db, rows, base), "hasMore": more, "total": photos_total(db)}


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
    return photo_dict(db, photo, base_url_for(request))


@router.post("/{photo_id}/like", dependencies=[Depends(require_member)])
def like_photo(photo_id: int, request: Request, db: Session = Depends(get_db)):
    photo = db.get(SharedPhoto, photo_id)
    if photo is None:
        raise HTTPException(404, "Foto non trovata")
    photo.likes_count += 1
    bump_version(db)
    db.commit()
    db.refresh(photo)
    return photo_dict(db, photo, base_url_for(request))


# ---------------------------------------------------------------- commenti (solo registrati)


def comment_author(request: Request, db: Session, member: Guest | None) -> tuple[str, str, int | None]:
    """(nome, tipo, guest_id) di chi commenta, deciso dal server: profilo dell'invitato, festeggiato, cassiere
    oppure "Organizzatori". Stesso ordine di precedenza degli elenchi riservati."""
    if member is not None:
        return member.full_name, "guest", member.id
    h = request.headers
    graduate_id = graduate_for_token(request, h.get("x-graduate-token"), db)
    if graduate_id:
        target = db.get(GiftTarget, graduate_id)
        return (target.short_name if target else graduate_id), "graduate", None
    scope = treasurer_scope(request, None, h.get("x-treasurer-token")) if h.get("x-treasurer-token") else False
    if scope is None:  # TREASURER_TOKEN globale: tutti i cassieri
        return "Cassieri", "treasurer", None
    if scope:
        collector_id = next(iter(scope))
        name = next((c.get("name") for c in gift_collectors_info(request.app.state.event_data) if c["id"] == collector_id), "")
        return (name or "Cassiere"), "treasurer", None
    return "Organizzatori", "admin", None


def _can_delete_comment(c: PhotoComment, author: tuple[str, str, int | None], admin: bool) -> bool:
    name, kind, guest_id = author
    if admin:
        return True
    if kind == "guest":
        return guest_id is not None and c.guest_id == guest_id
    return c.author_kind == kind and c.author_name == name


def _comment_json(c: PhotoComment, author: tuple[str, str, int | None], admin: bool) -> dict:
    return {**c.to_dict(), "mine": _can_delete_comment(c, author, admin)}


@router.get("/{photo_id}/comments")
def list_comments(photo_id: int, request: Request, db: Session = Depends(get_db), member: Guest | None = Depends(require_member),
                  admin: bool = Depends(is_admin)):
    """Tutti i commenti della foto, dal più vecchio; "mine" = lo si può cancellare."""
    if db.get(SharedPhoto, photo_id) is None:
        raise HTTPException(404, "Foto non trovata")
    author = comment_author(request, db, member)
    rows = db.scalars(select(PhotoComment).where(PhotoComment.photo_id == photo_id).order_by(PhotoComment.created_at.asc(), PhotoComment.id.asc())).all()
    return [_comment_json(c, author, admin) for c in rows]


@router.post("/{photo_id}/comments", status_code=201)
def add_comment(photo_id: int, body: PhotoCommentIn, request: Request, db: Session = Depends(get_db),
                member: Guest | None = Depends(require_member), admin: bool = Depends(is_admin)):
    """Nuovo commento: l'autore lo decide il server (vedi comment_author). Ritorna il commento e la foto aggiornata."""
    photo = db.get(SharedPhoto, photo_id)
    if photo is None:
        raise HTTPException(404, "Foto non trovata")
    name, kind, guest_id = author = comment_author(request, db, member)
    last = db.scalars(select(PhotoComment).where(PhotoComment.author_kind == kind, PhotoComment.author_name == name)
                      .order_by(PhotoComment.created_at.desc()).limit(1)).first()
    if last is not None and now_ms() - last.created_at < COMMENT_MIN_INTERVAL_MS:
        raise HTTPException(429, "Aspetta qualche secondo prima di un altro commento")
    c = PhotoComment(photo_id=photo_id, guest_id=guest_id, author_name=name, author_kind=kind, text=body.text)
    db.add(c)
    bump_version(db)
    db.commit()
    db.refresh(c)
    request.app.state.webhooks.emit("photo.commented", {"comment": c.to_dict(), "photo": photo.to_dict(base_url_for(request))})
    return {"comment": _comment_json(c, author, admin), "photo": photo_dict(db, photo, base_url_for(request))}


@router.delete("/{photo_id}/comments/{comment_id}", status_code=204)
def delete_comment(photo_id: int, comment_id: int, request: Request, db: Session = Depends(get_db),
                   member: Guest | None = Depends(require_member), admin: bool = Depends(is_admin)):
    """Cancella un commento: il suo autore o un organizzatore."""
    c = db.get(PhotoComment, comment_id)
    if c is None or c.photo_id != photo_id:
        raise HTTPException(404, "Commento non trovato")
    if not _can_delete_comment(c, comment_author(request, db, member), admin):
        raise HTTPException(403, "Puoi cancellare solo i tuoi commenti")
    db.delete(c)
    bump_version(db)
    db.commit()
    return None


@router.delete("/{photo_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_photo(photo_id: int, request: Request, db: Session = Depends(get_db)):
    photo = db.get(SharedPhoto, photo_id)
    if photo is None:
        raise HTTPException(404, "Foto non trovata")
    remove_photo_files(request.app.state.settings.upload_dir, photo)
    db.execute(delete(PhotoComment).where(PhotoComment.photo_id == photo.id))
    db.delete(photo)
    bump_version(db)
    db.commit()
    return None
