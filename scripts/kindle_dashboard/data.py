"""Data collectors for the dashboard.

Three blocks:
    calendar_block(today)  - list of {label, events[]} for today + next 3 days,
                             scraped from CALENDAR.md per CALENDAR-RULES.md.
    spond_block()          - list of {who, when, title} pending-response items,
                             scanned from memory/spond/*.jsonl.
    weather_block()        - dict with today + 3-day outlook from yr.no.

Each function returns the empty/None default on failure (caught by view.py),
so the template never sees an exception.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any

from mail_reader.config import cfg

# Household first names the dashboard recognises in calendar titles.
FAMILY_MEMBERS: tuple[str, ...] = tuple(cfg("family.members", ("Robin", "Bjorn", "Carl")))

# ---------- paths -----------------------------------------------------------

# scripts/kindle_dashboard/data.py → diary repo root is two levels up.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CALENDAR_MD = REPO_ROOT / "CALENDAR.md"

# ---------- calendar -------------------------------------------------------

# Bullet line format per CALENDAR-RULES.md:
#   - **<date>[ – <end-date>][ <time>[–<time>]][ (note)]** — <title>[ frieksttekst]
# Date(s): YYYY-MM-DD, range separated by en-dash "–".
# Times: HH:MM, range separated by en-dash "–" (we also tolerate ASCII "-").
_EVENT_RE = re.compile(
    r"""
    ^-\s\*\*
    (?P<d1>\d{4}-\d{2}-\d{2})
    (?:\s*[–-]\s*(?P<d2>\d{4}-\d{2}-\d{2}))?      # optional date range
    (?:\s+(?P<t1>\d{2}:\d{2})                     # optional start time
        (?:\s*[–-]\s*(?P<t2>\d{2}:\d{2}))?        #   optional end time
    )?
    (?:\s*\((?P<paren>[^)]+)\))?                  # optional parenthetical
    \*\*
    \s*—\s*
    (?P<title>.+?)
    \s*$
    """,
    re.VERBOSE,
)

_NB_WEEKDAYS = [
    "Mandag", "Tirsdag", "Onsdag", "Torsdag",
    "Fredag", "Lørdag", "Søndag",
]
_NB_MONTHS = [
    "", "januar", "februar", "mars", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "desember",
]


def _nb_date(d: _dt.date, *, today: _dt.date | None = None) -> str:
    if today and d == today:
        return f"I dag · {_NB_WEEKDAYS[d.weekday()]} {d.day}. {_NB_MONTHS[d.month]}"
    if today and d == today + _dt.timedelta(days=1):
        return f"I morgen · {_NB_WEEKDAYS[d.weekday()]} {d.day}. {_NB_MONTHS[d.month]}"
    return f"{_NB_WEEKDAYS[d.weekday()]} {d.day}. {_NB_MONTHS[d.month]}"


def _who(title: str) -> str:
    # Light heuristic — names that show up as whole words.
    text = title
    found = []
    for name in FAMILY_MEMBERS:
        if re.search(rf"\b{name}\b", text):
            found.append(name[0])  # initials
    return "/".join(found)


def _clean_title(raw: str) -> str:
    """Strip inline markdown and trim the body of a calendar title for display.

    CALENDAR.md often appends a long context blob to the title (RSVP status,
    parents' phone numbers, etc). For the wall display we want only the short
    headline — everything up to the first `.` (period+space) or ` — ` (the
    em-dash that introduces fri-tekst-halen).
    """
    t = raw.strip()
    t = re.sub(r"\*\*", "", t)  # drop inline bold markers
    for sep in (". ", " — ", " – "):
        i = t.find(sep)
        if i > 12:
            t = t[:i].rstrip(",.")
            break
    return t.strip()


def _fmt_time(t1: str | None, t2: str | None, paren: str | None) -> str:
    if t1 and t2:
        return f"{t1}–{t2}"
    if t1:
        return t1
    if paren and "all day" in paren.lower():
        return "hele dagen"
    return "hele dagen"


def parse_calendar(text: str) -> list[dict[str, Any]]:
    """Extract event records from CALENDAR.md text.

    Returns list of dicts:
        {"start": date, "end": date, "time": str, "title": str, "who": str}

    Lines that don't match are silently skipped — per CALENDAR-RULES.md the
    parser must tolerate prose freely interleaved with event lines.
    """
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = _EVENT_RE.match(line)
        if not m:
            continue
        d1 = _dt.date.fromisoformat(m.group("d1"))
        d2 = _dt.date.fromisoformat(m.group("d2")) if m.group("d2") else d1
        title = _clean_title(m.group("title"))
        events.append(
            {
                "start": d1,
                "end": d2,
                "time": _fmt_time(m.group("t1"), m.group("t2"), m.group("paren")),
                "title": title,
                "who": _who(title),
            }
        )
    return events


def month_grid(today: _dt.date) -> dict[str, Any]:
    """6-week grid covering the current month, with today + busy-day markers.

    Returns:
        {"name": "Mai 2026",
         "weeks": [[cell, cell, ..., cell], ...]}
    each cell: {"day": int, "in_month": bool, "is_today": bool, "has_events": bool}
    """
    import calendar as _cal

    try:
        text = CALENDAR_MD.read_text()
        events = parse_calendar(text)
    except FileNotFoundError:
        events = []

    busy: set[_dt.date] = set()
    for ev in events:
        d = ev["start"]
        while d <= ev["end"]:
            busy.add(d)
            d += _dt.timedelta(days=1)

    # Monday-first month grid spanning whatever weeks cover the 1st..last.
    first = today.replace(day=1)
    start = first - _dt.timedelta(days=first.weekday())  # back up to Monday
    weeks: list[list[dict[str, Any]]] = []
    cur = start
    while True:
        week: list[dict[str, Any]] = []
        for _ in range(7):
            week.append(
                {
                    "day": cur.day,
                    "in_month": cur.month == today.month,
                    "is_today": cur == today,
                    "has_events": cur in busy and cur.month == today.month,
                }
            )
            cur += _dt.timedelta(days=1)
        weeks.append(week)
        if cur.month != today.month and cur > today.replace(
            day=_cal.monthrange(today.year, today.month)[1]
        ):
            break
        if len(weeks) >= 6:
            break

    return {
        "name": f"{_NB_MONTHS[today.month].capitalize()} {today.year}",
        "weeks": weeks,
    }


def calendar_block(today: _dt.date, *, days_ahead: int = 3) -> list[dict[str, Any]]:
    """Group events by day for today..today+days_ahead.

    Events spanning multiple days appear on every day they cover.
    """
    try:
        text = CALENDAR_MD.read_text()
    except FileNotFoundError:
        return []
    all_events = parse_calendar(text)

    by_day: list[dict[str, Any]] = []
    for i in range(days_ahead + 1):
        d = today + _dt.timedelta(days=i)
        items = [
            {"time": ev["time"], "who": ev["who"], "text": ev["title"]}
            for ev in all_events
            if ev["start"] <= d <= ev["end"]
        ]
        # Sort: timed events by start time, all-day to the bottom.
        items.sort(
            key=lambda x: (x["time"] in {"hele dagen"}, x["time"])
        )
        by_day.append({"label": _nb_date(d, today=today), "events": items})

    return by_day


# ---------- spond ----------------------------------------------------------

SPOND_DIR = REPO_ROOT / "memory" / "spond"

# member-id → short display label. Add more here as IDs are discovered for
# other kids / clubs. The dashboard surfaces *unanswered* RSVPs for any of
# these IDs across all future events.
SPOND_MEMBER_IDS: dict[str, str] = cfg(
    "spond.member_ids",
    {"0123456789ABCDEF0123456789ABCDEF": "H"},  # placeholder; real ids in private config
)


def _parse_iso(s: str) -> _dt.datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(s)


def _short_when(d: _dt.datetime) -> str:
    """Compact Norwegian "Tor 12.6 17:30" — small enough for one line.

    Norwegian writes dates with dots (12.6), not slashes (12/6). Asserting
    on the separator keeps the doctest independent of the system timezone
    that ``astimezone()`` resolves against.

    >>> "/" in _short_when(_dt.datetime(2026, 6, 12, 17, 30, tzinfo=_dt.UTC))
    False
    >>> _short_when(_dt.datetime(2026, 6, 12, 17, 30, tzinfo=_dt.UTC)).count(".")
    1
    """
    local = d.astimezone()
    wday = ["Man", "Tir", "Ons", "Tor", "Fre", "Lør", "Søn"][local.weekday()]
    return f"{wday} {local.day}.{local.month} {local.strftime('%H:%M')}"


def spond_block() -> list[dict[str, str]]:
    if not SPOND_DIR.exists():
        return []
    now = _dt.datetime.now(_dt.UTC)

    # Latest record per event id (sync re-fetches the same events).
    latest: dict[str, dict[str, Any]] = {}
    import json

    for fp in sorted(SPOND_DIR.glob("*.jsonl")):
        for line in fp.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") != "event":
                continue
            data = rec.get("data") or {}
            eid = data.get("id")
            if not eid:
                continue
            prev = latest.get(eid)
            if prev and (prev.get("fetched_at") or "") >= (rec.get("fetched_at") or ""):
                continue
            latest[eid] = rec

    items: list[dict[str, str]] = []
    for rec in latest.values():
        data = rec["data"]
        start_str = data.get("startTimestamp")
        if not start_str:
            continue
        try:
            start = _parse_iso(start_str)
        except ValueError:
            continue
        if start < now:
            continue
        responses = data.get("responses") or {}
        unanswered = responses.get("unansweredIds") or []
        hit_id = next((mid for mid in SPOND_MEMBER_IDS if mid in unanswered), None)
        if not hit_id:
            continue
        items.append(
            {
                "who": SPOND_MEMBER_IDS[hit_id],
                "when": _short_when(start),
                "title": _clean_title(str(data.get("heading", "?"))),
                "_sort": start.isoformat(),
            }
        )

    items.sort(key=lambda x: x["_sort"])
    for it in items:
        del it["_sort"]
    return items[:4]


# ---------- weather (yr.no / api.met.no) -----------------------------------

# Home coordinates + yr.no User-Agent — overridden by private config.
WEATHER_LAT = cfg("weather.lat", 59.913)
WEATHER_LON = cfg("weather.lon", 10.752)
WEATHER_UA = cfg("weather.ua", "diary-kindle-dashboard/0.1 user@example.com")
WEATHER_TTL_SECONDS = 1800  # 30 min; well under yr.no's typical Expires.

_weather_cache: dict[str, Any] = {"fetched_at": None, "payload": None}

_SYMBOL_LABELS = {
    "clearsky": "Sol",
    "fair": "Lettskyet",
    "partlycloudy": "Halvskyet",
    "cloudy": "Skyet",
    "fog": "Tåke",
    "lightrain": "Lett regn",
    "rain": "Regn",
    "heavyrain": "Kraftig regn",
    "lightrainshowers": "Lette byger",
    "rainshowers": "Regnbyger",
    "heavyrainshowers": "Kraftige byger",
    "lightsleet": "Lett sludd",
    "sleet": "Sludd",
    "heavysleet": "Kraftig sludd",
    "lightsnow": "Lett snø",
    "snow": "Snø",
    "heavysnow": "Kraftig snø",
    "snowshowers": "Snøbyger",
    "thunderstorm": "Torden",
    "lightrainandthunder": "Regn og torden",
    "rainandthunder": "Regn og torden",
}

YR_ICONS_DIR = Path(__file__).parent / "static" / "yricons"


def _label_for(symbol: str) -> str:
    base = symbol.split("_", 1)[0]
    return _SYMBOL_LABELS.get(base, symbol or "")


def _icon_uri_for(symbol: str) -> str | None:
    """Return a data: URI of the official yr.no SVG for this symbol code.

    yr.no's icons are full-colour with gradients; the template applies a
    CSS grayscale filter so they survive the e-ink render as soft mono shapes.
    Falls back through day/base variants if the exact symbol isn't found.
    """
    import base64

    base = symbol.split("_", 1)[0]
    for name in (symbol, base + "_day", base):
        path = YR_ICONS_DIR / f"{name}.svg"
        if path.exists():
            data = path.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            return f"data:image/svg+xml;base64,{b64}"
    return None


def _fetch_yr_compact() -> dict[str, Any]:
    import httpx

    now = _dt.datetime.now(_dt.UTC)
    if _weather_cache["fetched_at"] and _weather_cache["payload"]:
        age = (now - _weather_cache["fetched_at"]).total_seconds()
        if age < WEATHER_TTL_SECONDS:
            return _weather_cache["payload"]
    r = httpx.get(
        "https://api.met.no/weatherapi/locationforecast/2.0/compact",
        params={"lat": WEATHER_LAT, "lon": WEATHER_LON},
        headers={"User-Agent": WEATHER_UA},
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json()
    _weather_cache["payload"] = payload
    _weather_cache["fetched_at"] = now
    return payload


def weather_block() -> dict[str, Any] | None:
    """Today + next 3 days, computed from yr.no compact timeseries."""
    try:
        payload = _fetch_yr_compact()
    except Exception:
        return None
    series = (payload.get("properties") or {}).get("timeseries") or []
    if not series:
        return None

    by_day: dict[_dt.date, list[dict[str, Any]]] = {}
    for entry in series:
        ts = entry.get("time")
        if not ts:
            continue
        try:
            t = _parse_iso(ts).astimezone()
        except ValueError:
            continue
        by_day.setdefault(t.date(), []).append({"t": t, "e": entry})

    today = _dt.date.today()
    days = sorted(d for d in by_day if d >= today)[:4]
    if not days:
        return None

    def _summary(d: _dt.date) -> dict[str, Any] | None:
        entries = by_day[d]
        temps: list[float] = []
        symbol = ""
        symbol_locked = False
        for it in entries:
            inst = ((it["e"].get("data") or {}).get("instant") or {}).get("details") or {}
            temp = inst.get("air_temperature")
            if temp is not None:
                temps.append(float(temp))
            if symbol_locked:
                continue
            data = it["e"].get("data") or {}
            for key in ("next_6_hours", "next_12_hours", "next_1_hours"):
                blk = (data.get(key) or {}).get("summary") or {}
                sym = blk.get("symbol_code")
                if not sym:
                    continue
                # Prefer midday symbol if we hit one; otherwise keep first
                # non-empty we find as fallback.
                if not symbol:
                    symbol = sym
                if 10 <= it["t"].hour <= 14:
                    symbol = sym
                    symbol_locked = True
                break
        if not temps:
            return None
        return {
            "lo": round(min(temps)),
            "hi": round(max(temps)),
            "symbol": symbol,
        }

    today_summary = _summary(today)
    if not today_summary:
        return None

    next_days = []
    for d in days[1:]:
        s = _summary(d)
        if not s:
            continue
        weekday = _NB_WEEKDAYS[d.weekday()][:3]
        next_days.append(
            {
                "label": f"{weekday} {d.day}.",
                "lo": s["lo"],
                "hi": s["hi"],
                "weather": _label_for(s["symbol"]),
                "icon_uri": _icon_uri_for(s["symbol"]),
            }
        )

    return {
        "today": {
            "summary": _label_for(today_summary["symbol"]),
            "lo": today_summary["lo"],
            "hi": today_summary["hi"],
            "icon_uri": _icon_uri_for(today_summary["symbol"]),
        },
        "next": next_days,
    }


# ---------- precipitation nowcast (yr 90-minute) ---------------------------

_nowcast_cache: dict[str, Any] = {"fetched_at": None, "payload": None}
NOWCAST_TTL_SECONDS = 240  # nowcast updates every ~5 min; 4 min is safe.

RAIN_ON_THRESHOLD = 0.05  # mm/h — anything below this counts as "dry".


def _fetch_nowcast_payload() -> dict[str, Any] | None:
    """Cached fetch of the yr nowcast payload. Returns None on failure."""
    import httpx

    now = _dt.datetime.now(_dt.UTC)
    if _nowcast_cache["fetched_at"] and _nowcast_cache["payload"]:
        age = (now - _nowcast_cache["fetched_at"]).total_seconds()
        if age < NOWCAST_TTL_SECONDS:
            return _nowcast_cache["payload"]
    try:
        r = httpx.get(
            "https://api.met.no/weatherapi/nowcast/2.0/complete",
            params={"lat": WEATHER_LAT, "lon": WEATHER_LON},
            headers={"User-Agent": WEATHER_UA},
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
        _nowcast_cache["payload"] = payload
        _nowcast_cache["fetched_at"] = now
        return payload
    except Exception:
        return None


def _parse_nowcast_bars(
    payload: dict[str, Any],
) -> tuple[list[float], int | None]:
    """Extract the 18 5-min precipitation bars + first-rain-minute marker."""
    series = (payload.get("properties") or {}).get("timeseries") or []
    bars: list[float] = []
    first_rain_minute: int | None = None
    for i, entry in enumerate(series[:18]):
        details = (
            (entry.get("data") or {}).get("instant", {}).get("details", {})
        )
        rate = float(details.get("precipitation_rate", 0.0) or 0.0)
        bars.append(rate)
        if rate > RAIN_ON_THRESHOLD and first_rain_minute is None:
            first_rain_minute = i * 5
    return bars, first_rain_minute


def nowcast_stats() -> dict[str, Any] | None:
    """Low-level nowcast summary — returns numbers even when it's dry.

    Used by the precipitation watcher to detect dry → rain (and back)
    transitions. Returns None only when the API fetch fails or there's no
    timeseries; a confidently-dry 90 min is `max_rate == 0.0`, not None.
    """
    payload = _fetch_nowcast_payload()
    if payload is None:
        return None
    bars, first_rain_minute = _parse_nowcast_bars(payload)
    if not bars:
        return None
    return {
        "max_rate": round(max(bars), 3),
        "first_rain_minute": first_rain_minute,
        "fetched_at": _nowcast_cache["fetched_at"],
    }


def nowcast_block() -> dict[str, Any] | None:
    """Yr.no 90-minute precipitation nowcast for the template.

    Hides the block (returns None) when it's dry — that's intentional, so
    the rendered PNG stays bit-identical on quiet days and the e-ink panel
    avoids unnecessary refreshes.
    """
    payload = _fetch_nowcast_payload()
    if payload is None:
        return None
    bars, first_rain_minute = _parse_nowcast_bars(payload)
    if not bars:
        return None

    max_rate = max(bars)
    if max_rate < RAIN_ON_THRESHOLD:
        return None
    total_mm = sum(b * (5 / 60) for b in bars)  # rate (mm/h) × 5 min

    if first_rain_minute == 0:
        if max_rate < 0.5:
            summary = "Lett regn nå"
        elif max_rate < 2:
            summary = "Regn nå"
        else:
            summary = "Kraftig regn nå"
    else:
        summary = f"Regn om ~{first_rain_minute} min"

    # Anchor the timestamp to when the upstream data was actually fetched,
    # not to wall-clock-now. Otherwise every render shifts the labels and
    # defeats the server-side material-hash cache in serve.py.
    fetched_at_local = _nowcast_cache["fetched_at"].astimezone()
    end_local = fetched_at_local + _dt.timedelta(minutes=90)
    return {
        "bars": bars,
        "max_rate": round(max_rate, 2),
        "total_mm": round(total_mm, 1),
        "first_rain_minute": first_rain_minute,
        "summary": summary,
        "now_time": fetched_at_local.strftime("%H:%M"),
        "end_time": end_local.strftime("%H:%M"),
    }


# ---------- sunrise / sunset (local, no network) ---------------------------


def sun_block() -> dict[str, str] | None:
    """Today's sunrise + sunset for Eksempelveien. Pure-Python via astral."""
    try:
        from astral import LocationInfo
        from astral.sun import sun
    except ImportError:
        return None
    try:
        loc = LocationInfo(
            "Oslo", "Norway", "Europe/Oslo", WEATHER_LAT, WEATHER_LON
        )
        s = sun(loc.observer, date=_dt.date.today(), tzinfo=loc.tzinfo)
    except Exception:
        return None
    return {
        "rise": s["sunrise"].strftime("%H:%M"),
        "set": s["sunset"].strftime("%H:%M"),
    }
