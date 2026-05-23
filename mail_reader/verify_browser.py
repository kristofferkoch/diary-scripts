# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.48"]
# ///
"""End-to-end browser checks for the mail_reader webapp.

Drives a headless Chromium through the three surfaces that are easiest
to break by a server-rendered change: the inbox + agenda strip, the
message view (entity chips), and the tankekart mode switcher.

Each check is independent and produces a screenshot. Exit code is 0 iff
every check passed; failures are printed as `FAIL: <name> — <why>` and
all screenshots are still written so a reviewer can look at what the
page actually looked like at the moment of failure.

The default base URL is the tailnet Caddy endpoint, because hitting
`127.0.0.1:8800` directly bypasses the `/mail/` prefix that the app's
`root_path` setting bakes into every URL via `url_for()` — meaning
clicks on inbox links and HTMX swaps would 404. Override via --base if
running on a different host.

Usage::

    uv run mail_reader/verify_browser.py
    uv run mail_reader/verify_browser.py --base https://other.host/mail/
    uv run mail_reader/verify_browser.py --keep-going --out /tmp/shots
"""
from __future__ import annotations

import argparse
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page, sync_playwright


DEFAULT_BASE = "https://server.example.ts.net/mail"
# System chromium-headless on Fedora — playwright's bundled chromium
# requires a separate download we'd rather avoid.
CHROME = "/usr/lib64/chromium-browser/headless_shell"

# Module-level so check functions don't all need to take `base` as a
# parameter. main() sets this before running checks.
BASE_URL = DEFAULT_BASE


@dataclass
class Result:
    name: str
    ok: bool
    notes: list[str] = field(default_factory=list)
    screenshots: list[Path] = field(default_factory=list)
    error: str | None = None


def _shot(page: Page, out: Path, name: str, result: Result) -> None:
    path = out / f"{name}.png"
    page.screenshot(path=str(path), full_page=False)
    result.screenshots.append(path)


def check_inbox_dismiss(page: Page, out: Path) -> Result:
    """Inbox renders, agenda strip has cards with dismiss buttons, and
    clicking dismiss removes a card via HTMX swap."""
    r = Result(name="inbox+agenda_dismiss", ok=False)
    page.goto(BASE_URL + "/", wait_until="domcontentloaded")
    page.wait_for_selector(".threads", timeout=15000)

    n_threads = page.locator("ul.threads li.thread").count()
    n_cards = page.locator(".agenda-card").count()
    n_dismiss = page.locator(".agenda-dismiss").count()
    r.notes.append(f"threads={n_threads}, agenda_cards={n_cards}, dismiss_buttons={n_dismiss}")
    _shot(page, out, "01_inbox", r)

    if n_threads == 0:
        r.error = "no threads rendered — inbox is empty"
        return r
    if n_cards == 0:
        r.notes.append("(no agenda cards on the strip — skipping dismiss probe)")
        r.ok = True
        return r
    if n_dismiss != n_cards:
        r.error = f"dismiss buttons ({n_dismiss}) != agenda cards ({n_cards})"
        return r

    # Click the first dismiss and watch the count drop.
    first = page.locator(".agenda-card").first
    first_href = first.locator(".agenda-link").get_attribute("href")
    r.notes.append(f"dismissing first card → {first_href}")
    first.locator(".agenda-dismiss").click()
    # swap delay = 140ms; the request is local so 500ms is safe headroom.
    page.wait_for_timeout(500)
    n_after = page.locator(".agenda-card").count()
    _shot(page, out, "02_after_dismiss", r)
    if n_after != n_cards - 1:
        r.error = f"after dismiss expected {n_cards - 1} cards, got {n_after}"
        return r

    r.notes.append(f"agenda_cards after dismiss: {n_after}")
    r.ok = True
    return r


def check_message_entity_chips(page: Page, out: Path) -> tuple[Result, str | None]:
    """Open a message that has entity chips, then click a chip and
    confirm the /e/{id} page renders. Returns the chip's target URL so
    callers can chain further checks. (None if no chips were found.)"""
    r = Result(name="message_entity_chips", ok=False)
    # Find an inbox thread row whose summary mentions enough text that
    # tier-2 likely populated entities. Heuristic: take the first one.
    # The actual filtering happens in entities.py — chips_for_message
    # only returns rows for `summaries.status='done'` at the current
    # prompt_version. If chips=0 we hop to the next thread.
    page.goto(BASE_URL + "/", wait_until="domcontentloaded")
    page.wait_for_selector("ul.threads li.thread a", timeout=15000)
    thread_links = page.locator("ul.threads li.thread a")
    n = min(thread_links.count(), 8)
    if n == 0:
        r.error = "no thread links on inbox"
        return r, None

    entity_href: str | None = None
    for i in range(n):
        page.goto(BASE_URL + "/", wait_until="domcontentloaded")
        page.locator("ul.threads li.thread a").nth(i).click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(300)
        chip_count = page.locator(".entity-chips .chip").count()
        if chip_count > 0:
            r.notes.append(f"thread #{i} → {chip_count} entity chips, url={page.url}")
            kinds = page.locator(".entity-chips .chip").evaluate_all(
                "els => els.map(e => (e.className.match(/chip-([a-z]+)/) || [])[1])"
            )
            r.notes.append(f"chip kinds: {kinds}")
            entity_href = page.locator(".entity-chips .chip").first.get_attribute("href")
            _shot(page, out, "03_message", r)
            break
        r.notes.append(f"thread #{i}: no chips, trying next…")
    else:
        r.notes.append("(no message with entity chips found in first 8 threads)")
        r.ok = True
        return r, None

    if entity_href is None:
        r.notes.append("(no entity chip URL — anchorless chip?)")
        r.ok = True
        return r, None

    # Click the chip and verify the entity page.
    r.notes.append(f"clicking first chip → {entity_href}")
    page.locator(".entity-chips .chip").first.click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(300)
    if page.locator(".entity-head").count() != 1:
        r.error = f"entity page missing .entity-head on {page.url}"
        return r, entity_href
    rows = page.locator("ul.threads li.thread").count()
    r.notes.append(f"entity page rows: {rows}, url={page.url}")
    _shot(page, out, "04_entity", r)
    r.ok = True
    return r, entity_href


def check_tankekart_modes(page: Page, out: Path) -> Result:
    """Open a message and exercise all three tankekart modes + branch
    expansion. The chunks-mode load is async (HTMX hx-trigger=load), so
    wait for `.tankekart-modes` to materialise before probing."""
    r = Result(name="tankekart_modes", ok=False)
    page.goto(BASE_URL + "/", wait_until="domcontentloaded")
    page.wait_for_selector("ul.threads li.thread a", timeout=15000)
    page.locator("ul.threads li.thread a").first.click()
    page.wait_for_load_state("domcontentloaded")
    msg_url = page.url
    r.notes.append(f"opened message: {msg_url}")

    try:
        page.wait_for_selector(".tankekart-modes", timeout=20000)
    except Exception as e:
        r.error = f"tankekart never loaded (.tankekart-modes not visible in 20s): {e}"
        _shot(page, out, "05_tankekart_timeout", r)
        return r

    modes = page.locator(".tankekart-modes .mode").evaluate_all(
        "els => els.map(e => ({label: e.textContent.trim(),"
        " active: e.classList.contains('active')}))"
    )
    r.notes.append(f"mode tabs: {modes}")
    labels = [m["label"] for m in modes]
    if labels != ["Innhold", "Tema", "Funn"]:
        r.error = f"unexpected mode labels: {labels}"
        return r
    if sum(1 for m in modes if m["active"]) != 1 or not modes[0]["active"]:
        r.error = f"default active tab is not Innhold/chunks: {modes}"
        return r

    n_branches = page.locator(".branches .branch").count()
    r.notes.append(f"chunks-mode branches: {n_branches}")
    _shot(page, out, "05_tankekart_chunks", r)

    # Expand one branch (only meaningful if there are any).
    if n_branches > 0:
        page.locator(".branches .branch summary.branch-label").first.click()
        page.wait_for_timeout(200)
        opened = page.locator(".branches .branch[open]").count()
        r.notes.append(f"after expand, branches open: {opened}")
        if opened != 1:
            r.error = f"expected exactly 1 open branch, got {opened}"
            _shot(page, out, "06_expanded_fail", r)
            return r
        _shot(page, out, "06_branch_expanded", r)

    # Switch tabs. The hx-get URL has `?mode=<m>` — wait for the
    # response to land instead of guessing a sleep duration, so the
    # check is robust against slow emergent-mode clustering queries.
    def switch_to(label: str, expected_mode: str) -> str | None:
        with page.expect_response(
            lambda resp: ("/api/tankekart/" in resp.url
                          and f"mode={expected_mode}" in resp.url),
            timeout=15000,
        ):
            page.locator(".tankekart-modes .mode", has_text=label).click()
        # Give HTMX a beat to actually swap the response into the DOM.
        page.wait_for_function(
            "label => {"
            "  const a = document.querySelector('.tankekart-modes .mode.active');"
            "  return a && a.textContent.trim() === label;"
            "}",
            arg=label,
            timeout=5000,
        )
        return page.locator(".tankekart-modes .mode.active").inner_text().strip()

    try:
        active_after = switch_to("Tema", "themes")
    except Exception as e:
        r.error = f"Tema switch failed: {e}"
        _shot(page, out, "07_themes_fail", r)
        return r
    n_themes = page.locator(".branches .branch").count()
    empty_themes = page.locator(".tankekart-body p.empty").count()
    r.notes.append(f"themes (active={active_after!r}): branches={n_themes}, empty={empty_themes}")
    _shot(page, out, "07_tankekart_themes", r)

    try:
        active_after = switch_to("Funn", "emergent")
    except Exception as e:
        r.error = f"Funn switch failed: {e}"
        _shot(page, out, "08_emergent_fail", r)
        return r
    n_emergent = page.locator(".branches .branch").count()
    empty_emergent = page.locator(".tankekart-body p.empty").count()
    r.notes.append(f"emergent (active={active_after!r}): branches={n_emergent}, empty={empty_emergent}")
    _shot(page, out, "08_tankekart_emergent", r)

    r.ok = True
    return r


def check_error_paths(page: Page, base: str, out: Path) -> Result:
    """Bad-input probes hit through the same browser context so cookies/
    headers match what HTMX would send. We don't assert on body text
    (FastAPI's default 400 page is fine) — only on the status code."""
    r = Result(name="error_paths", ok=True)
    ctx = page.context.request
    probes = [
        ("agenda dismiss empty thread_id",
         "POST", "/api/agenda/dismiss?thread_id=&kind=event&occurs_at=2026-05-25", 400),
        ("agenda dismiss bad kind",
         "POST", "/api/agenda/dismiss?thread_id=x&kind=bogus&occurs_at=2026-05-25", 400),
        ("agenda dismiss bad date",
         "POST", "/api/agenda/dismiss?thread_id=x&kind=event&occurs_at=not-a-date", 400),
        ("entity not found",
         "GET", "/e/999999999", 404),
    ]
    for label, method, path, want in probes:
        url = f"{base.rstrip('/')}{path}"
        resp = ctx.fetch(url, method=method)
        actual = resp.status
        ok = actual == want
        r.notes.append(f"{'✅' if ok else '❌'} {label}: want {want}, got {actual}")
        if not ok:
            r.ok = False
            r.error = (r.error or "") + f"\n{label}: want {want}, got {actual}"
    return r


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help=f"base URL (default: {DEFAULT_BASE})")
    parser.add_argument("--out", type=Path, default=Path("/tmp/mr_shots"),
                        help="screenshot output directory (default: /tmp/mr_shots)")
    parser.add_argument("--clean", action="store_true",
                        help="wipe the output dir before starting")
    parser.add_argument("--keep-going", action="store_true",
                        help="run all checks even if one fails")
    parser.add_argument("--chrome", default=CHROME,
                        help=f"chromium-headless binary (default: {CHROME})")
    args = parser.parse_args()

    if args.clean and args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True, exist_ok=True)

    global BASE_URL
    base = args.base.rstrip("/")
    BASE_URL = base
    print(f"# mail_reader browser verify")
    print(f"# base: {base}")
    print(f"# shots: {args.out}")
    print()

    results: list[Result] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chrome, headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  ignore_https_errors=True)
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  !! pageerror: {e}"))

        checks: list[Callable[[], Result]] = [
            lambda: check_inbox_dismiss(page, args.out),
            lambda: check_message_entity_chips(page, args.out)[0],
            lambda: check_tankekart_modes(page, args.out),
            lambda: check_error_paths(page, base, args.out),
        ]
        for run in checks:
            try:
                r = run()
            except Exception as e:
                r = Result(name="(uncaught)", ok=False,
                           error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            results.append(r)
            print(f"[{'PASS' if r.ok else 'FAIL'}] {r.name}")
            for n in r.notes:
                print(f"    · {n}")
            if r.error:
                for line in r.error.splitlines():
                    print(f"    !! {line}")
            for s in r.screenshots:
                print(f"    → {s}")
            print()
            if not r.ok and not args.keep_going:
                break

        browser.close()

    failed = [r for r in results if not r.ok]
    print(f"# {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
