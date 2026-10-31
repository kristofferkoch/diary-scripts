# Immich — `immich-recent`

> Index: [scripts/](../README.md)

`immich_recent.py` lists recently-added Immich assets via the API —
the fast replacement for crawling the NFS-mounted library with
`find -newermt` (multi-minute over RAID6) when the user says "I've
uploaded new photos" (innbo rounds, receipts, …). It searches
`POST /api/search/metadata` with the v3.2.0 structured filter
(`filter.createdAt.gte` — upload time, indexed in Postgres),
ascending (`orderBy fileCreatedAt`), cursor-paginated (`nextCursor`;
the old flat fields and `page`/`nextPage` are deprecated):

```bash
uv run immich-recent                              # added in the last 24 h
uv run immich-recent --since "2026-08-04 14:20"   # naive = local time
uv run immich-recent --limit 40
uv run immich-recent --thumbs /tmp/im-thumbs      # + preview JPEGs over HTTP
uv run immich-recent --city Oslo                  # place search, whole library
uv run immich-recent --near 59.9145,10.7716       # radius search, 5 km default
uv run immich-recent --near 59.9145,10.7716,2     # …2 km, nearest first
```

Place search: `--city/--state/--country` are `/api/search/metadata`
filters; with any of them and no explicit `--since` the 24 h default is
dropped and the whole library is searched. `--near` pulls
`GET /api/map/markers` and filters by haversine distance (the metadata
search has no bounding-box filter), then fetches the nearest `--limit`
asset records. Place listings get a location column (city + lat,lon;
`--near` prepends distance).

`--thumbs` writes `NN-<originalFileName>.jpg` in listing order — read
those instead of multi-MB originals over NFS. `originalPath` in the
listing is the container-side path (`/data/…`); the library root is
`UPLOAD_LOCATION` (NFS mount, see diary PHOTOS.md) when you need the
original.

Auth: API key from `$IMMICH_API_KEY`, else the fixed argv
`pass show immich/api-key` (the key is never committed). Create the key in
Immich → user avatar → Account Settings → API Keys; check **`asset.read`**
(search) + **`asset.view`** (thumbnails) + **`map.read`** (`--near`) — keep it
read-only. `asset.download` is only needed if originals must ever be fetched
via the API (they're normally read over NFS).
Base URL: `hosts.immich` in root-owned `/etc/diary/trusted.toml`; local config
and `IMMICH_URL` cannot override it.
