"""Background watcher: nudge the wall Kindle when precipitation changes.

Runs as an asyncio task inside the FastAPI app (see serve.py). Polls
yr.no's nowcast every POLL_SECONDS via the shared cache in data.py, and
calls `ssh kindle 'initctl restart dashboard'` only when the change
crosses a meaningful threshold:

  - dry → wet, wet → dry: rain starting or stopping in the next 90 min
  - intensity shift ≥ INTENSITY_DELTA mm/h: notable change in max rate
  - timing shift > TIMING_DELTA_MIN min: first-rain-minute moved enough
    to matter ("rain in 15 min" → "rain in 5 min" warrants a nudge)

Quiet hours stay quiet — no SSH, no nudge, the Kindle's hourly poll
catches calendar/spond/forecast drift on its own.

The watcher does not own any state the dashboard renders; it only
observes data.nowcast_stats(). The actual re-render happens server-side
when the Kindle's next poll lands (the material-hash cache picks up the
change automatically).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

from . import data

log = logging.getLogger("kindle_dashboard.watcher")

POLL_SECONDS = 300          # 5 min — matches yr.no's own update cadence
INTENSITY_DELTA = 0.5       # mm/h
TIMING_DELTA_MIN = 5        # min

_last_state: dict[str, float | int | None] | None = None


def _meaningful_change(
    prev: dict[str, float | int | None] | None,
    curr: dict[str, float | int | None] | None,
) -> str | None:
    """Return a short reason string if the change warrants a nudge."""
    if curr is None:
        return None  # API blip — wait for next tick

    prev_max = float((prev or {}).get("max_rate", 0.0) or 0.0)
    curr_max = float(curr.get("max_rate", 0.0) or 0.0)
    prev_first = (prev or {}).get("first_rain_minute")
    curr_first = curr.get("first_rain_minute")

    if prev_max < data.RAIN_ON_THRESHOLD and curr_max >= data.RAIN_ON_THRESHOLD:
        return f"rain started (max {curr_max:.2f} mm/h, first {curr_first} min)"
    if prev_max >= data.RAIN_ON_THRESHOLD and curr_max < data.RAIN_ON_THRESHOLD:
        return "rain stopped"
    if abs(prev_max - curr_max) >= INTENSITY_DELTA:
        return f"intensity {prev_max:.2f} → {curr_max:.2f} mm/h"
    if (
        prev_first is not None
        and curr_first is not None
        and abs(int(prev_first) - int(curr_first)) > TIMING_DELTA_MIN
    ):
        return f"timing {prev_first} → {curr_first} min"
    return None


def _poke_kindle() -> None:
    """Fire-and-forget SSH to the wall Kindle. Logs on failure, never raises."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
             "kindle", "initctl restart dashboard"],
            timeout=15,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("kindle poke ok")
        else:
            log.warning(
                "kindle poke rc=%s stderr=%r",
                result.returncode,
                result.stderr.strip(),
            )
    except Exception:
        log.exception("ssh poke to kindle failed")


async def precipitation_watcher() -> None:
    """Long-running task: poll nowcast, nudge Kindle on meaningful change."""
    global _last_state
    log.info(
        "precipitation watcher starting (poll=%ds, intensity_delta=%s mm/h,"
        " timing_delta=%s min)",
        POLL_SECONDS,
        INTENSITY_DELTA,
        TIMING_DELTA_MIN,
    )
    while True:
        try:
            curr = data.nowcast_stats()
        except Exception:
            log.exception("nowcast_stats failed; will retry")
            curr = None

        # Don't poke on the first valid fetch — we have no baseline to
        # compare against. Just record the state.
        if _last_state is None and curr is not None:
            log.info(
                "watcher baseline: max_rate=%s mm/h, first_rain=%s min",
                curr["max_rate"],
                curr["first_rain_minute"],
            )
            _last_state = {
                "max_rate": curr["max_rate"],
                "first_rain_minute": curr["first_rain_minute"],
            }
        else:
            reason = _meaningful_change(_last_state, curr)
            if reason:
                log.info("nedbør endring: %s — poking kindle", reason)
                _poke_kindle()
                if curr is not None:
                    _last_state = {
                        "max_rate": curr["max_rate"],
                        "first_rain_minute": curr["first_rain_minute"],
                    }

        await asyncio.sleep(POLL_SECONDS)
