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
| `http://10.0.0.206:8801/dashboard.png`                          | Kindle on LAN          |
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
  in `data.SPOND_MEMBER_IDS` — add more as discovered.
- Weather → `api.met.no/weatherapi/locationforecast/2.0/compact`,
  cached 30 min in process.
- Sunrise/sunset → `astral`, computed locally (no network).

## Running locally

```bash
# Dev: just start the server, hit http://127.0.0.1:8801/
uv run --frozen --no-sync python -m scripts.kindle_dashboard.serve

# Quick render check (no Kindle in the loop)
curl -s http://127.0.0.1:8801/dashboard.png > /tmp/dash.png
```

## Production

Systemd user unit (`~/.config/systemd/user/kindle-dashboard.service`):

```bash
systemctl --user {start,stop,restart,status} kindle-dashboard
journalctl --user -u kindle-dashboard -f
```

`loginctl enable-linger user` keeps the user-bus up after logout, so the
unit survives reboots. (Already on; the mail_reader unit needs it too.)

## Pointing the Kindle at this server

The Kindle stores its target URL in `/mnt/us/dashboard/dashboard.conf`.
From any host that has the kindle SSH key (server has it under
`~/.ssh/kindle_ed25519`):

```bash
ssh kindle "cat > /mnt/us/dashboard/dashboard.conf" <<'EOF'
URL="http://10.0.0.206:8801/dashboard.png"
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
