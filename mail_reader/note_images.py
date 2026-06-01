"""Image processing for note attachments.

The /notes/ page lets the user attach a photo from a phone (Firefox
Android). Phone photos are large (several MB, multi-thousand pixels) and
often carry an EXIF orientation tag plus location metadata. This module
turns raw uploaded bytes into two web-friendly JPEGs — a downscaled
"web" image and a small "thumbnail" — with orientation baked in and all
other EXIF (incl. GPS) dropped.

Pure and DB-free by design: it takes bytes and returns bytes, so it
unit-tests without Postgres. Storage lives in `notes.py`; the route
glue lives in `server.py`.

Offensive by construction: `process()` raises `NotAnImage` on anything
Pillow can't decode as an image, so a junk or non-image upload fails
loudly at the boundary instead of landing a corrupt row.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

# Longest-edge caps. The web image is what you get when you tap the
# thumbnail; the thumbnail is what the note list renders inline. Both are
# generous enough to stay legible on a phone while keeping a note's photo
# to a few hundred KB rather than the multi-MB original.
WEB_MAX_EDGE = 1600
THUMB_MAX_EDGE = 480
JPEG_QUALITY = 85

MIME_TYPE = "image/jpeg"


class NotAnImage(ValueError):
    """Raised when the uploaded bytes can't be decoded as an image."""


@dataclass(frozen=True)
class ProcessedImage:
    """A processed attachment ready to store: two JPEGs + web dimensions."""

    mime_type: str
    image_bytes: bytes
    thumb_bytes: bytes
    width: int
    height: int


def _to_rgb(img: Image.Image) -> Image.Image:
    """Flatten to RGB so we can always emit JPEG. Transparency (PNG, some
    HEIC) is composited onto white rather than turning black."""
    if img.mode in ("RGB",):
        return img
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def process(raw: bytes) -> ProcessedImage:
    """Decode `raw`, apply EXIF orientation, strip metadata, and produce a
    downscaled web JPEG plus a thumbnail JPEG.

    Raises NotAnImage if the bytes aren't a decodable image.
    """
    if not raw:
        raise NotAnImage("empty upload")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # force decode now so truncated/garbage data fails here
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise NotAnImage(str(exc)) from exc

    # Bake in the camera's EXIF orientation, then drop all EXIF by going
    # through a fresh RGB image (privacy: GPS tags don't survive).
    img = ImageOps.exif_transpose(img)
    img = _to_rgb(img)

    web = img.copy()
    web.thumbnail((WEB_MAX_EDGE, WEB_MAX_EDGE))
    thumb = img.copy()
    thumb.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE))

    return ProcessedImage(
        mime_type=MIME_TYPE,
        image_bytes=_encode_jpeg(web),
        thumb_bytes=_encode_jpeg(thumb),
        width=web.width,
        height=web.height,
    )
