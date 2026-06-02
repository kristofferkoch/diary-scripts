# kindle_dashboard

PNG generator + HTTP server that feeds the wall-mounted Kindle Paperwhite
(`KINDLE.md`). Runs on server as a systemd user unit; reverse-proxied
to the tailnet via caddy.

## Files

```
serve.py                       FastAPI app — /dashboard.png, /, /healthz
render.py                      HTML → screenshot → 8-bit gray rotate → bytes
data.py                        Collectors: calendar, spond, weather, sun, month_grid
view.py                        Glue: assembles the Jinja context
templates/dashboard.html.j2    The layout (1448×1072 landscape, then rotated)
```

## Endpoints

| URL                                                             | Reachable from         |
|-----------------------------------------------------------------|------------------------|
| `http://192.0.2.10:8801/dashboard.png`                          | Kindle on LAN          |
| `https://server.example.ts.net/kindle/`                | Phone over tailnet     |
| `http://127.0.0.1:8801/dashboard.png`                           | this host              |

The PNG is the *only* artifact the Kindle consumes. Browser visitors get a
plain HTML page at `/` that just embeds the same PNG.

## Output contract

Strictly required by `eips` on PW5 (firmware 5.18.1):

- Mode "L" (8-bit grayscale)
- Dimensions exactly 1072×1448 (portrait)
- No alpha

Render flow: HTML rendered at 1448×1072 (landscape) → Chromium screenshot →
PIL rotate −90° → mode "L" → save optimized PNG. Coercion lives in
`render._ensure_kindle_format()` — *always* pipe through it.

## What's on the screen

- Big date header ("Torsdag 28. mai") + current time
- Left column: mini-month grid (today highlighted, days with events get a dot)
  + agenda for today + tomorrow
- Right column: yr.no weather (today + 3 days) + Spond unanswered RSVPs
- Footer: sunrise/sunset + last update time

Data sources:
- Calendar → `CALENDAR.md` (per `CALENDAR-RULES.md`)
- Spond → `memory/spond/*.jsonl`, latest record per event id, filtered to
  future events where a known member-id is in `unansweredIds`. Member-ids
  come from `config/local.toml` `[spond] member_ids`; `data.py` has a
  placeholder default.
- Weather → `api.met.no/weatherapi/locationforecast/2.0/compact`,
  cached 30 min in process. Coordinates from `config/local.toml`
  `[weather] lat`/`lon`; `data.py` has placeholder defaults.
- Sunrise/sunset → `astral`, computed locally (no network), using the
  same coordinates as weather.
- Family names used for calendar event labelling come from
  `config/local.toml` `[family] members`; `data.py` defaults to
  `["Robin", "Bjorn", "Carl"]`.

## Config

Site-specific values (coordinates, family first-names, Spond member-ids)
are kept in the **private** `config/local.toml` in the diary workspace,
outside this public submodule. The code reads them via
`mail_reader.config.cfg()` / `workspace_root()` — do **not** hardcode
real values in `data.py`.

Relevant TOML keys:

```toml
[weather]
lat = 59.913     # decimal degrees
lon = 10.752
ua  = "diary-kindle-dashboard/0.1 user@example.com"

[family]
members = ["Robin", "Bjorn", "Carl"]   # first names for calendar labelling

[spond]
# member-id (32-char hex) → short display label shown on the dashboard.
# Discover new IDs from memory/spond/*.jsonl when a new club is added.
member_ids = { "0123456789ABCDEF0123456789ABCDEF" = "H" }
```

`data.py` ships placeholder defaults for all three so the server starts
even without a local.toml (useful in CI / fresh checkouts), but weather
will point at a generic location and Spond will never match.

## Running locally

Commands run from inside the `diary-scripts/` submodule directory.

```bash
# Dev: just start the server, hit http://127.0.0.1:8801/
cd ~/diary/diary-scripts
uv run --frozen --no-sync python -m scripts.kindle_dashboard.serve

# Alternatively, use the console entry point:
uv run --project ~/diary/diary-scripts kindle-dashboard

# Quick render check (no Kindle in the loop)
curl -s http://127.0.0.1:8801/dashboard.png > /tmp/dash.png
```

The private config must be discoverable: set `DIARY_CONFIG` to point at
`config/local.toml` in the diary workspace (see §Config above), or run
from a directory where `mail_reader.config.workspace_root()` can locate it.

## Production

Systemd user unit (`~/.config/systemd/user/kindle-dashboard.service`):

```bash
systemctl --user {start,stop,restart,status} kindle-dashboard
journalctl --user -u kindle-dashboard -f
```

The unit sets `WorkingDirectory=%h/diary/diary-scripts` and
`Environment=DIARY_CONFIG=%h/diary/config/local.toml` so that
`mail_reader.config` picks up the private config (coordinates, family
names, Spond member-ids) at startup.

`loginctl enable-linger user` keeps the user-bus up after logout, so the
unit survives reboots. (Already on; the mail_reader unit needs it too.)

## Pointing the Kindle at this server

The Kindle stores its target URL in `/mnt/us/dashboard/dashboard.conf`.
From any host that has the kindle SSH key (server has it under
`~/.ssh/kindle_ed25519`):

```bash
ssh kindle "cat > /mnt/us/dashboard/dashboard.conf" <<'EOF'
URL="http://192.0.2.10:8801/dashboard.png"
INTERVAL=3600
KIOSK=1
ROTATE=0
BOOT_RETRY=15
EOF
ssh kindle 'initctl restart dashboard'
```

`INTERVAL=3600` is the production cadence (1 h). During development drop
to 60–180 for fast feedback. `KINDLE.md` explains the rest of the device
config (frontlight, kiosk-kill, recovery paths).

## Customizing the layout

The HTML template is plain Jinja → Chromium → screenshot. Edit
`templates/dashboard.html.j2`, then `systemctl --user restart kindle-dashboard`.

Two rendering gotchas baked in from painful iteration:

- Chromium ships no emoji font on this VM. U+2600 ☀ renders fine, but
  U+2601 ☁, U+2602 ☂, U+2744 ❄ etc render as tofu. Stick to safe Unicode
  (✓ ☀ ▢ □ ▣ ■ ● ● ┌─┐│└┘) or use text labels.
- Font sizes need to be huge — the Kindle is wall-mounted and read from
  across the room. Body text below ~28px gets unreadable.

## Adding new content blocks

1. Add a collector to `data.py` (return None or [] on failure, never raise).
2. Wire it into `view.build_context()` (wrap in try/except + log).
3. Add a template section that handles the empty case gracefully.

The template's `main` grid is `1.25fr 1fr` — wider for the left calendar,
narrower for the right. Keep right-column items compact; they overflow
faster than the left.
