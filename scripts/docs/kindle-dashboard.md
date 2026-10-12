# kindle_dashboard — wall display PNG generator

> Index: [scripts/](../README.md)

`scripts/kindle_dashboard/` is a FastAPI + Playwright service that renders a
single PNG for the wall-mounted Kindle Paperwhite. See
[KINDLE.md](../../../KINDLE.md) for the device-side jailbreak/kiosk state and
[scripts/kindle_dashboard/README.md](../kindle_dashboard/README.md) for the
generator's architecture, content blocks, and customization hooks.

```bash
# Dev: launch with logs in the foreground (run from inside diary-scripts/)
uv run python -m scripts.kindle_dashboard.serve

# Production: systemd user unit
systemctl --user {start,stop,restart,status} kindle-dashboard
journalctl --user -u kindle-dashboard -f
```

Endpoints once running:
- `http://192.0.2.10:8801/dashboard.png` — what the Kindle on the LAN polls
- `https://server.example.ts.net/kindle/` — phone preview over the tailnet

The Kindle SSH key lives at `~/.ssh/kindle_ed25519` on server, with
the `kindle` host alias in `~/.ssh/config`. Standard `ssh kindle 'initctl
restart dashboard'` forces an immediate refresh.

When adding a new content block: collector in `data.py` (return empty/None
on failure, never raise), wire it into `view.build_context()` with a
try/except, add a template section that renders gracefully when empty.
PNGs are 1448×1072 landscape composed, rotated −90° → 1072×1448 portrait,
mode "L" — render.py's `_ensure_kindle_format()` enforces this. **Do not
bypass it**; raw RGBA out the door breaks `eips` on-device.
