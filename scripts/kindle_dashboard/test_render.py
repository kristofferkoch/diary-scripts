from __future__ import annotations

import io

from PIL import Image, ImageDraw

from scripts.kindle_dashboard import render


def test_compose_placeholder_is_landscape():
    """Regression: the placeholder must be composed in the landscape compose
    orientation (like html_to_png), NOT straight onto the portrait framebuffer.

    The old code drew on a KINDLE_W x KINDLE_H portrait canvas and never
    rotated, so on the landscape-mounted wall the error text showed up 90° off
    and shrunk — unreadable. Composing landscape + sharing ROTATE_DIR fixes it.
    """
    img = render._compose_placeholder("render error — see journalctl", "2026-06-13 12:00")
    assert img.size == (render.COMPOSE_W, render.COMPOSE_H)
    assert img.size[0] > img.size[1]  # landscape: wider than tall


def test_placeholder_png_is_device_portrait_format():
    """Output contract: 8-bit grayscale, exact portrait framebuffer size."""
    img = Image.open(io.BytesIO(render.placeholder_png("hello")))
    assert img.mode == "L"
    assert img.size == (render.KINDLE_W, render.KINDLE_H)


def test_load_font_actually_scales_with_size():
    """Regression: every placeholder string used to render at ~8px because the
    candidate font paths didn't exist on this host and `_load_font` fell back to
    the fixed-size bitmap default, silently dropping `size`. A larger requested
    size must yield visibly taller glyphs."""
    draw = ImageDraw.Draw(Image.new("L", (10, 10)))
    small = draw.textbbox((0, 0), "Ag", font=render._load_font(24))
    large = draw.textbbox((0, 0), "Ag", font=render._load_font(96))
    assert (large[3] - large[1]) > 2 * (small[3] - small[1])


def test_wrap_text_breaks_long_message_to_width():
    # Narrow width so it wraps regardless of the available font backend
    # (DejaVu TrueType vs. the bundled bitmap fallback have different metrics).
    draw = ImageDraw.Draw(Image.new("L", (render.COMPOSE_W, render.COMPOSE_H)))
    font = render._load_font(64)
    max_width = 200
    msg = "render error — see journalctl for the full traceback and stack frames"
    wrapped = render._wrap_text(draw, msg, font, max_width)
    assert "\n" in wrapped  # wrapped to more than one line
    for line in wrapped.splitlines():
        # single over-long words are unbreakable; only multi-word lines must fit
        if " " in line:
            assert draw.textlength(line, font=font) <= max_width
