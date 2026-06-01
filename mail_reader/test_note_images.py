"""Tests for mail_reader.note_images — the Pillow attachment pipeline.

DB-free: every case builds an in-memory image with Pillow, so these run
anywhere `pillow` is installed (no mailvec needed).
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from mail_reader import note_images as ni


def _png(size: tuple[int, int], color="red", mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(size: tuple[int, int], color="blue") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def test_process_downscales_oversized_web_and_thumb():
    out = ni.process(_jpeg((4000, 3000)))
    # Web image is capped at WEB_MAX_EDGE on its longest edge…
    assert max(out.width, out.height) == ni.WEB_MAX_EDGE
    # …and aspect ratio is preserved (4:3).
    assert out.width == ni.WEB_MAX_EDGE and out.height == 1200
    # Thumbnail is genuinely smaller than the web image.
    with Image.open(io.BytesIO(out.thumb_bytes)) as t:
        assert max(t.size) == ni.THUMB_MAX_EDGE
        assert max(t.size) < max(out.width, out.height)


def test_process_does_not_upscale_small_image():
    out = ni.process(_jpeg((200, 150)))
    assert (out.width, out.height) == (200, 150)


def test_process_always_emits_jpeg_even_from_png():
    out = ni.process(_png((300, 300)))
    assert out.mime_type == ni.MIME_TYPE == "image/jpeg"
    with Image.open(io.BytesIO(out.image_bytes)) as im:
        assert im.format == "JPEG"


def test_process_flattens_transparency_onto_white():
    # A fully transparent RGBA image should composite to white, not black.
    out = ni.process(_png((50, 50), color=(0, 0, 0, 0), mode="RGBA"))
    with Image.open(io.BytesIO(out.image_bytes)) as im:
        assert im.convert("RGB").getpixel((25, 25)) == (255, 255, 255)


def test_process_applies_exif_orientation():
    # Orientation tag 6 = rotate 90°: a 200x100 stored image should come out
    # 100x200 once the orientation is baked in.
    base = Image.new("RGB", (200, 100), "green")
    exif = base.getexif()
    exif[0x0112] = 6  # Orientation
    buf = io.BytesIO()
    base.save(buf, format="JPEG", exif=exif)
    out = ni.process(buf.getvalue())
    assert (out.width, out.height) == (100, 200)


def test_process_preserves_gps_on_web_image():
    # GPS is a signal we keep (see IDEAS.md "GPS as a signal"). A photo with
    # location should still carry it after processing.
    base = Image.new("RGB", (300, 200), "green")
    exif = base.getexif()
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"; gps[2] = (59.0, 55.0, 23.0)  # latitude
    gps[3] = "E"; gps[4] = (10.0, 45.0, 12.3)  # longitude
    buf = io.BytesIO()
    base.save(buf, format="JPEG", exif=exif)

    out = ni.process(buf.getvalue())
    with Image.open(io.BytesIO(out.image_bytes)) as im:
        kept = im.getexif().get_ifd(0x8825)
        assert kept.get(1) == "N" and kept.get(2) == (59.0, 55.0, 23.0)
        assert kept.get(3) == "E"


def test_process_normalizes_orientation_tag_but_keeps_other_exif():
    # The Orientation tag is baked into pixels and removed; unrelated EXIF
    # (here: a Make tag) rides along.
    base = Image.new("RGB", (200, 100), "blue")
    exif = base.getexif()
    exif[0x0112] = 6           # Orientation -> baked in, then dropped
    exif[0x010F] = "TestCam"   # Make -> preserved
    buf = io.BytesIO()
    base.save(buf, format="JPEG", exif=exif)

    out = ni.process(buf.getvalue())
    assert (out.width, out.height) == (100, 200)  # rotated
    with Image.open(io.BytesIO(out.image_bytes)) as im:
        e = im.getexif()
        assert e.get(0x0112) in (None, 1)        # orientation normalized away
        assert e.get(0x010F) == "TestCam"        # other EXIF preserved


def test_process_rejects_non_image():
    with pytest.raises(ni.NotAnImage):
        ni.process(b"this is not an image")


def test_process_rejects_empty():
    with pytest.raises(ni.NotAnImage):
        ni.process(b"")
