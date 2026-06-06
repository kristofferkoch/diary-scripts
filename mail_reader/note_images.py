"""Image processing for note attachments.

The /notes/ page lets the user attach a photo from a phone (Firefox
Android). Phone photos are large (several MB, multi-thousand pixels) and
often carry an EXIF orientation tag plus location metadata. This module
turns raw uploaded bytes into two web-friendly JPEGs — a downscaled
"web" image and a small "thumbnail".

Orientation is baked into the pixels (and the now-redundant Orientation
tag normalized away). The rest of the EXIF — crucially GPS — is
deliberately PRESERVED on the web image: a note's photo location is a
useful signal ("where was this taken"), not noise to strip. See the
"GPS as a signal" item in IDEAS.md for what we might do with it.

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
    """A processed attachment ready to store: two JPEGs + web dimensions.

    `gps_lat`/`gps_lon` are signed decimal degrees pulled from the photo's
    EXIF GPS tags, or None — location is optional (screenshots, location-off,
    share-sheet stripping), so a missing fix is normal, not an error. See the
    "GPS as a signal" item in IDEAS.md.
    """

    mime_type: str
    image_bytes: bytes
    thumb_bytes: bytes
    width: int
    height: int
    gps_lat: float | None = None
    gps_lon: float | None = None


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


def _encode_jpeg(img: Image.Image, exif: Image.Exif | None = None) -> bytes:
    buf = io.BytesIO()
    if exif is not None:
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True, exif=exif)
    else:
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


# EXIF GPS IFD tag (0x8825) and the sub-tags we read within it.
_GPS_IFD = 0x8825
_GPS_LAT_REF, _GPS_LAT = 1, 2
_GPS_LON_REF, _GPS_LON = 3, 4


def _dms_to_deg(dms, ref) -> float | None:
    """Convert one EXIF (degrees, minutes, seconds) triple plus its hemisphere
    ref ('N'/'S'/'E'/'W') to signed decimal degrees. Returns None if the triple
    isn't three numbers — corrupt or absent GPS shouldn't sink the whole upload.

    >>> _dms_to_deg((59.0, 55.0, 30.0), "N")
    59.925
    >>> _dms_to_deg((10.0, 45.0, 0.0), "W")
    -10.75
    >>> _dms_to_deg((10.0, 45.0, 0.0), b"E")
    10.75
    >>> _dms_to_deg(None, "N") is None
    True
    >>> _dms_to_deg((1.0, 2.0), "N") is None
    True
    """
    try:
        deg, minute, sec = (float(x) for x in dms)
    except (TypeError, ValueError, ZeroDivisionError):
        # A 0/0 EXIF rational (cameras write it for an empty seconds field)
        # is an IFDRational that reprs as nan but raises ZeroDivisionError on
        # float() — treat it as absent GPS rather than sinking the upload.
        return None
    value = deg + minute / 60 + sec / 3600
    if isinstance(ref, bytes):
        ref = ref.decode("ascii", "ignore")
    if isinstance(ref, str) and ref.upper() in ("S", "W"):
        value = -value
    return value


def _extract_gps(exif: Image.Exif) -> tuple[float, float] | None:
    """Pull (lat, lon) in signed decimal degrees from EXIF, or None when the
    photo carries no usable fix. Partial (one axis only) or out-of-range
    coordinates count as no fix — better to drop a dubious location than store
    a wrong one."""
    gps = exif.get_ifd(_GPS_IFD)
    if not gps:
        return None
    lat = _dms_to_deg(gps.get(_GPS_LAT), gps.get(_GPS_LAT_REF))
    lon = _dms_to_deg(gps.get(_GPS_LON), gps.get(_GPS_LON_REF))
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def process(raw: bytes) -> ProcessedImage:
    """Decode `raw`, bake in EXIF orientation, and produce a downscaled web
    JPEG plus a thumbnail JPEG. GPS / remaining EXIF is preserved on the web
    image (it's a signal); the thumbnail is a clean derivative.

    Raises NotAnImage if the bytes aren't a decodable image.
    """
    if not raw:
        raise NotAnImage("empty upload")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # force decode now so truncated/garbage data fails here
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise NotAnImage(str(exc)) from exc

    # Bake in the camera's orientation. exif_transpose rotates the pixels and
    # drops the now-redundant Orientation tag, but keeps the rest of the EXIF
    # (GPS included) on the returned image — that's what we carry forward.
    img = ImageOps.exif_transpose(img)
    exif = img.getexif()
    gps = _extract_gps(exif)
    img = _to_rgb(img)

    web = img.copy()
    web.thumbnail((WEB_MAX_EDGE, WEB_MAX_EDGE))
    thumb = img.copy()
    thumb.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE))

    return ProcessedImage(
        mime_type=MIME_TYPE,
        image_bytes=_encode_jpeg(web, exif=exif or None),
        thumb_bytes=_encode_jpeg(thumb),
        width=web.width,
        height=web.height,
        gps_lat=gps[0] if gps else None,
        gps_lon=gps[1] if gps else None,
    )
