import io
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..deps import base_url_for, get_db
from ..models import SharedPhoto
from ..state import bump_version

router = APIRouter(prefix="/api/photos", tags=["foto"])

MAX_SIDE = 1600  # le foto vengono ridimensionate per risparmiare spazio e banda


@router.get("")
def list_photos(request: Request, db: Session = Depends(get_db)):
    base = base_url_for(request)
    rows = db.scalars(select(SharedPhoto).order_by(SharedPhoto.created_at.desc())).all()
    return [p.to_dict(base) for p in rows]


@router.post("", status_code=201)
async def upload_photo(
    request: Request,
    file: UploadFile = File(...),
    authorName: str = Form(default="Invitato"),
    caption: str = Form(default=""),
    db: Session = Depends(get_db),
):
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

    photo = SharedPhoto(
        author_name=(authorName or "").strip() or "Invitato",
        caption=(caption or "").strip(),
        image_path=f"/uploads/{name}",
        likes_count=1,
    )
    db.add(photo)
    bump_version(db)
    db.commit()
    db.refresh(photo)
    return photo.to_dict(base_url_for(request))


@router.post("/{photo_id}/like")
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
    if photo.image_path:
        path = os.path.join(request.app.state.settings.upload_dir, os.path.basename(photo.image_path))
        if os.path.exists(path):
            os.remove(path)
    db.delete(photo)
    bump_version(db)
    db.commit()
    return None
