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

Auth: API key from ``$IMMICH_API_KEY``, else ``immich.api_key_cmd`` in
the private config (a ``pass`` reference — the key itself is never
committed; grant it at least asset.read + asset.view + asset.download).
Base URL: ``$IMMICH_URL``, else ``hosts.immich`` in config. Thumbnails
are written as ``NN-<originalFileName>.jpg`` so names match the listing
order; ``originalPath`` is the container-side path — the library root is
``UPLOAD_LOCATION`` (NFS mount on the photo host, see diary PHOTOS.md).
"""
from __future__ import annotations

import argparse
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
    client: httpx.Client, since: str, limit: int
) -> list[dict]:
    """POST /api/search/metadata with createdAfter, ascending, paginated."""
    out: list[dict] = []
    page = 1
    while len(out) < limit:
        r = client.post(
            "/api/search/metadata",
            json={
                "createdAfter": since,
                "order": "asc",
                "page": page,
                "size": min(250, limit - len(out)),
            },
        )
        r.raise_for_status()
        payload = r.json()["assets"]
        out.extend(payload["items"])
        if not payload.get("nextPage"):
            break
        page = int(payload["nextPage"])
    return out[:limit]


def fetch_thumbs(client: httpx.Client, assets: list[dict], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for i, a in enumerate(assets, 1):
        name = f"{i:02d}-{Path(a['originalFileName']).stem}.jpg"
        r = client.get(f"/api/assets/{a['id']}/thumbnail", params={"size": "preview"})
        r.raise_for_status()
        (dest / name).write_bytes(r.content)


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--since", help="ISO datetime or 'YYYY-MM-DD HH:MM' (default: last 24 h)")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--thumbs", type=Path, help="also download preview JPEGs to DIR")
    args = ap.parse_args()

    base = immich_url()
    headers = {"x-api-key": _api_key(), "Accept": "application/json"}
    with httpx.Client(base_url=base, headers=headers, timeout=30) as client:
        assets = search_recent(client, _parse_since(args.since), args.limit)
        for i, a in enumerate(assets, 1):
            print(
                f"{i:3d} {a['createdAt'][11:19]}Z  taken {a.get('localDateTime', '?')[:16]}"
                f"  {a['type']:5s}  {a['originalFileName']:42s} {a['originalPath']}"
            )
        if not assets:
            print("(no assets)")
        if args.thumbs and assets:
            fetch_thumbs(client, assets, args.thumbs)
            print(f"\n{len(assets)} thumbnails in {args.thumbs}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
