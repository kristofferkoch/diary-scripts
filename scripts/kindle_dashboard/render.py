"""PNG rendering for the Kindle dashboard.

Output contract — strictly required by `eips` on PW5:
    - PNG, mode "L" (8-bit grayscale, NOT RGB/RGBA).
    - Dimensions exactly KINDLE_W x KINDLE_H (1072 x 1448), portrait.

Two paths:
    placeholder_png(): pure-Pillow, no browser. Used at boot, on errors,
        and as the first end-to-end smoke test.
    html_to_png(): renders an HTML string through Playwright/Chromium at
        the Kindle's native viewport, screenshots it, converts to "L".
        The browser is launched once and reused for the lifetime of the
        process.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# The panel is physically 1072×1448 (portrait). We compose the dashboard in
# landscape because that's how the device is wall-mounted, then rotate the
# resulting image 90° before handing it to eips. The mount orientation that
# lines up with ROTATE_DIR is documented in KINDLE.md.
KINDLE_W = 1072            # panel width — what eips writes to the framebuffer
KINDLE_H = 1448            # panel height
COMPOSE_W = 1448           # HTML viewport width (landscape)
COMPOSE_H = 1072           # HTML viewport height
ROTATE_DIR = -90           # PIL rotate angle to go landscape → portrait


# ---------- output coercion --------------------------------------------------


def _ensure_kindle_format(img: Image.Image) -> Image.Image:
    """Coerce any PIL image into the strict eips-compatible format."""
    if img.mode != "L":
        img = img.convert("L")
    if img.size != (KINDLE_W, KINDLE_H):
        img = img.resize((KINDLE_W, KINDLE_H), Image.LANCZOS)
    return img


def to_png_bytes(img: Image.Image) -> bytes:
    """Final serialization. Always pass images through this on the way out."""
    img = _ensure_kindle_format(img)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------- placeholder (pure Pillow) ----------------------------------------


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        # DejaVu (Debian/Ubuntu + some Fedora installs)
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        # Liberation / Noto — what baglernissen (Fedora) actually ships. Without
        # one of these, the loop fell through to load_default(), a fixed ~8px
        # bitmap that ignores `size`, so every placeholder string came out
        # unreadably tiny on the wall regardless of the size we asked for.
        "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/google-noto-vf/NotoSans[wght].ttf",
        "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    # Last resort: modern Pillow's load_default(size) scales; older ignores it.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> str:
    """Greedy word-wrap `text` to `max_width` px, returning a `\\n`-joined block."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and draw.textlength(candidate, font=font) > max_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return "\n".join(lines)


def _compose_placeholder(message: str, now: str) -> Image.Image:
    """Draw the placeholder in the **landscape** compose orientation.

    Mirrors `html_to_png()`: the device is wall-mounted landscape, so we
    compose at COMPOSE_W x COMPOSE_H and let `placeholder_png()` apply the
    same ROTATE_DIR. Drawing straight onto the portrait framebuffer (the old
    behaviour) put the text 90° off and shrunk — unreadable on the wall.
    """
    img = Image.new("L", (COMPOSE_W, COMPOSE_H), 255)
    draw = ImageDraw.Draw(img)
    margin = 70

    title_font = _load_font(110)
    body_font = _load_font(64)
    small_font = _load_font(36)

    draw.text((margin, 90), "Kindle dashboard", font=title_font, fill=0)
    draw.line([(margin, 230), (COMPOSE_W - margin, 230)], fill=0, width=4)

    wrapped = _wrap_text(draw, message, body_font, COMPOSE_W - 2 * margin)
    draw.multiline_text((margin, 300), wrapped, font=body_font, fill=0, spacing=16)

    draw.text((margin, COMPOSE_H - 80), f"Rendered {now}", font=small_font, fill=0)
    return img


def placeholder_png(message: str = "kindle_dashboard online") -> bytes:
    """Pillow-only placeholder. No network, no browser — always works.

    Composed landscape then rotated through the same ROTATE_DIR as the real
    render, so it reads upright (and full-size) on the wall-mounted device.
    """
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    img = _compose_placeholder(message, now).rotate(ROTATE_DIR, expand=True)
    return to_png_bytes(img)


# ---------- HTML → PNG via Playwright ---------------------------------------

# Module-level browser cache. The first call to html_to_png() launches Chromium
# and keeps it warm; later calls reuse it. The lock is async-only because all
# Playwright calls are async and FastAPI runs the endpoint in the same loop.

_pw_lock = asyncio.Lock()
_pw_ctx = None  # async_playwright context manager handle
_browser = None  # Browser instance


async def _get_browser():
    """Lazy-init + cache Chromium. Holds the lock only during launch."""
    global _pw_ctx, _browser
    async with _pw_lock:
        if _browser is None:
            from playwright.async_api import async_playwright

            _pw_ctx = await async_playwright().start()
            _browser = await _pw_ctx.chromium.launch(
                args=["--disable-dev-shm-usage", "--no-sandbox"]
            )
    return _browser


async def html_to_png(html: str) -> bytes:
    """Render HTML at the landscape compose viewport, rotate, return 8-bit gray."""
    browser = await _get_browser()
    ctx = await browser.new_context(
        viewport={"width": COMPOSE_W, "height": COMPOSE_H},
        device_scale_factor=1,
        color_scheme="light",
    )
    try:
        page = await ctx.new_page()
        await page.set_content(html, wait_until="networkidle")
        raw = await page.screenshot(
            type="png", full_page=False, omit_background=False
        )
    finally:
        await ctx.close()

    img = Image.open(io.BytesIO(raw))
    img = img.rotate(ROTATE_DIR, expand=True)
    return to_png_bytes(img)


async def shutdown_browser() -> None:
    """Called from FastAPI's shutdown hook so reloads don't leak Chromium."""
    global _pw_ctx, _browser
    async with _pw_lock:
        if _browser is not None:
            await _browser.close()
            _browser = None
        if _pw_ctx is not None:
            await _pw_ctx.stop()
            _pw_ctx = None
