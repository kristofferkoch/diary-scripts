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


def _jpeg_with_gps(lat_dms, lat_ref, lon_dms, lon_ref) -> bytes:
    """A small JPEG carrying the given EXIF GPS triples/refs."""
    base = Image.new("RGB", (300, 200), "green")
    exif = base.getexif()
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = lat_ref, lat_dms
    gps[3], gps[4] = lon_ref, lon_dms
    buf = io.BytesIO()
    base.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_process_extracts_gps_to_decimal_degrees():
    # Oslo-ish: 59°55'30"N, 10°45'00"E -> 59.925, 10.75
    out = ni.process(_jpeg_with_gps((59.0, 55.0, 30.0), "N", (10.0, 45.0, 0.0), "E"))
    assert out.gps_lat == pytest.approx(59.925)
    assert out.gps_lon == pytest.approx(10.75)


def test_process_signs_southern_western_hemisphere():
    out = ni.process(_jpeg_with_gps((33.0, 51.0, 0.0), "S", (151.0, 12.0, 0.0), "W"))
    assert out.gps_lat < 0 and out.gps_lon < 0
    assert out.gps_lat == pytest.approx(-33.85)


def test_process_no_gps_leaves_coords_none():
    out = ni.process(_jpeg((100, 100)))
    assert out.gps_lat is None and out.gps_lon is None


def test_process_partial_gps_is_no_fix():
    # Latitude present but longitude missing -> treat as no location at all.
    base = Image.new("RGB", (120, 80), "green")
    exif = base.getexif()
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (59.0, 0.0, 0.0)  # lat only, no lon
    buf = io.BytesIO()
    base.save(buf, format="JPEG", exif=exif)
    out = ni.process(buf.getvalue())
    assert out.gps_lat is None and out.gps_lon is None


def test_process_survives_zero_denominator_gps_rational():
    # Some cameras write a 0/0 EXIF rational for an empty seconds field. PIL
    # decodes it as an IFDRational that raises ZeroDivisionError on float() —
    # it must be treated as absent GPS, not crash the whole upload (which 500'd
    # the /notes/ page on 2026-06-06).
    from PIL.TiffImagePlugin import IFDRational

    base = Image.new("RGB", (120, 80), "green")
    exif = base.getexif()
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (59.0, 55.0, IFDRational(0, 0))
    gps[3], gps[4] = "E", (10.0, 45.0, IFDRational(0, 0))
    buf = io.BytesIO()
    base.save(buf, format="JPEG", exif=exif)

    out = ni.process(buf.getvalue())  # must not raise
    assert out.gps_lat is None and out.gps_lon is None


def test_process_rejects_out_of_range_coords():
    # Corrupt EXIF claiming 200° longitude is dropped, not stored.
    out = ni.process(_jpeg_with_gps((10.0, 0.0, 0.0), "N", (200.0, 0.0, 0.0), "E"))
    assert out.gps_lat is None and out.gps_lon is None


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
