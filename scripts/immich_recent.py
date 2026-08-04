#!/usr/bin/env python3
"""immich_recent.py — recently-added Immich assets via the API.

Replaces the slow ``find /mnt/…/upload -newermt …`` crawl of the
NFS-mounted library (multi-minute over RAID6) with a metadata search
against the Immich server, which indexes upload time (``createdAt``) in
Postgres. Used when the user says "I've uploaded new photos" (inventory
rounds, receipts, …) — list what landed, then either open the original
over NFS or fetch a small preview thumbnail over HTTP.

    uv run immich-recent                              # added in the last 24 h
    uv run immich-recent --since "2026-08-04 14:20"   # local time, tz attached
    uv run immich-recent --limit 40
    uv run immich-recent --thumbs /tmp/im-thumbs      # also fetch preview JPEGs
    uv run immich-recent --city Oslo                  # place search (no time default)
    uv run immich-recent --near 59.9145,10.7716       # radius search, 5 km default
    uv run immich-recent --near 59.9145,10.7716,2     # …2 km, sorted by distance

Place search: ``--city/--state/--country`` are ``/api/search/metadata``
filters; ``--near`` pulls ``GET /api/map/markers`` (needs the ``map.read``
key permission) and filters by haversine distance. With a place filter
and no explicit ``--since`` there is no 24 h default — the whole library
is searched. When any place mode is active the listing gets a location
column (city + lat,lon from exif).

Auth: API key from ``$IMMICH_API_KEY``, else ``immich.api_key_cmd`` in
the private config (a ``pass`` reference — the key itself is never
committed). The key needs ``asset.read`` (search) + ``asset.view``
(thumbnails), plus ``map.read`` for ``--near`` — keep it read-only; add
``asset.download`` only if originals must ever be fetched via the API.
Base URL: ``$IMMICH_URL``, else ``hosts.immich`` in config. Thumbnails
are written as ``NN-<originalFileName>.jpg`` so names match the listing
order; ``originalPath`` is the container-side path — the library root is
``UPLOAD_LOCATION`` (NFS mount on the photo host, see diary PHOTOS.md).
"""
from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from mail_reader.config import cfg, immich_url


def _api_key() -> str:
    key = os.environ.get("IMMICH_API_KEY")
    if key:
        return key.strip()
    cmd = cfg("immich.api_key_cmd", None)
    if cmd:
        try:
            out = subprocess.run(
                cmd, shell=True, check=True, capture_output=True, text=True
            ).stdout.strip()
        except subprocess.CalledProcessError:
            out = ""
        if out:
            return out
    raise SystemExit(
        "No Immich API key — set $IMMICH_API_KEY or immich.api_key_cmd "
        "in the private config (create the key in Immich → user settings "
        "→ API Keys)."
    )


def _parse_since(raw: str | None) -> str:
    """--since as ISO 8601 with offset; naive input is read as local time."""
    if raw is None:
        dt = datetime.now().astimezone() - timedelta(hours=24)
        return dt.isoformat(timespec="seconds")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt).astimezone()
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue
    # Already ISO (possibly with offset/Z) — validate and pass through.
    return datetime.fromisoformat(raw).isoformat()


def search_recent(
    client: httpx.Client, since: str | None, limit: int, place: dict
) -> list[dict]:
    """POST /api/search/metadata with createdAfter, ascending, paginated."""
    out: list[dict] = []
    page = 1
    body: dict = {"order": "asc", **place}
    if since:
        body["createdAfter"] = since
    if place:
        body["withExif"] = True  # city + lat/lon for the location column
    while len(out) < limit:
        r = client.post(
            "/api/search/metadata",
            json={**body, "page": page, "size": min(250, limit - len(out))},
        )
        r.raise_for_status()
        payload = r.json()["assets"]
        out.extend(payload["items"])
        if not payload.get("nextPage"):
            break
        page = int(payload["nextPage"])
    return out[:limit]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def search_near(
    client: httpx.Client, lat: float, lon: float, radius_km: float, limit: int
) -> list[tuple[float, dict]]:
    """Radius search via GET /api/map/markers + per-asset fetch.

    The metadata search has no bounding-box filter, so pull all map
    markers (one per geotagged asset), keep those within the radius, and
    fetch the nearest ``limit`` asset records. Returns (distance_km,
    asset) pairs, nearest first.
    """
    r = client.get("/api/map/markers")
    r.raise_for_status()
    hits = []
    for m in r.json():
        d = _haversine_km(lat, lon, m["lat"], m["lon"])
        if d <= radius_km:
            hits.append((d, m["id"]))
    hits.sort()
    out = []
    for d, asset_id in hits[:limit]:
        r = client.get(f"/api/assets/{asset_id}")
        r.raise_for_status()
        out.append((d, r.json()))
    return out


def _parse_near(raw: str) -> tuple[float, float, float]:
    parts = raw.split(",")
    if len(parts) not in (2, 3):
        raise SystemExit("--near expects LAT,LON[,RADIUS_KM]")
    lat, lon = float(parts[0]), float(parts[1])
    radius = float(parts[2]) if len(parts) == 3 else 5.0
    return lat, lon, radius


def fetch_thumbs(client: httpx.Client, assets: list[dict], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for i, a in enumerate(assets, 1):
        name = f"{i:02d}-{Path(a['originalFileName']).stem}.jpg"
        try:
            r = client.get(
                f"/api/assets/{a['id']}/thumbnail", params={"size": "preview"}
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # e.g. encoded-video companions 404 — skip, keep going
            print(f"!! thumb {i} ({a['originalFileName']}): {e}", file=sys.stderr)
            continue
        (dest / name).write_bytes(r.content)


def _location(a: dict) -> str:
    e = a.get("exifInfo") or {}
    bits = []
    if e.get("city"):
        bits.append(e["city"])
    if e.get("latitude") is not None and e.get("longitude") is not None:
        bits.append(f"{e['latitude']:.4f},{e['longitude']:.4f}")
    return " ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--since", help="ISO datetime or 'YYYY-MM-DD HH:MM' (default: last 24 h)")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--thumbs", type=Path, help="also download preview JPEGs to DIR")
    ap.add_argument("--city", help="metadata search: city")
    ap.add_argument("--state", help="metadata search: state")
    ap.add_argument("--country", help="metadata search: country")
    ap.add_argument("--near", help="LAT,LON[,RADIUS_KM] radius search via map markers")
    args = ap.parse_args()

    place = {k: v for k in ("city", "state", "country") if (v := getattr(args, k))}
    # With a place filter and no explicit --since, search the whole library.
    since = None if (place and args.since is None) else _parse_since(args.since)

    base = immich_url()
    headers = {"x-api-key": _api_key(), "Accept": "application/json"}
    with httpx.Client(base_url=base, headers=headers, timeout=30) as client:
        distances: list[float] | None = None
        if args.near:
            lat, lon, radius = _parse_near(args.near)
            hits = search_near(client, lat, lon, radius, args.limit)
            distances = [d for d, _ in hits]
            assets = [a for _, a in hits]
        else:
            assets = search_recent(client, since, args.limit, place)
        for i, a in enumerate(assets, 1):
            loc = ""
            if place or distances is not None:
                loc = f"  {_location(a)}"
            if distances is not None:
                loc = f"  {distances[i-1]:.2f} km{loc}"
            print(
                f"{i:3d} {a['createdAt'][11:19]}Z  taken {a.get('localDateTime', '?')[:16]}"
                f"  {a['type']:5s}  {a['originalFileName']:42s} {a['originalPath']}{loc}"
            )
        if not assets:
            print("(no assets)")
        if args.thumbs and assets:
            fetch_thumbs(client, assets, args.thumbs)
            print(f"\n{len(assets)} thumbnails in {args.thumbs}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
