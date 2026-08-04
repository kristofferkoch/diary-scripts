#!/usr/bin/env bash
# firstboot-reconstitute — rebuild the live state the backup deliberately
# excludes, on the first boot of a qcow-restored image.
#
# Installed as /usr/local/sbin/firstboot-reconstitute and called by
# firstboot-reconstitute-offline.service / firstboot-reconstitute-online.service
# with the phase as $1:
#
#   offline — host Postgres initdb + pg_dumpall restore, notmuch index
#             rebuild + tag restore. Must succeed with NO network (this is
#             what the isolated verify boot asserts).
#   online  — NFS + fscache mounts, Immich docker stack pull/up + dump
#             restore, the gitignored diary-scripts .venv, Playwright
#             browsers. Completes on the real production boot.
#
# Sentinels (/var/lib/firstboot.done, /var/lib/firstboot-online.done) are
# written ONLY on success, so a failed phase is retried on the next boot and
# a half-completed phase is never mistaken for done. Individual steps are
# additionally idempotent (skip-if-present guards) so a re-run after a
# partial failure doesn't redo completed work.
#
# Nothing machine-specific is baked in: dump paths are the well-known
# /var/lib/restic-backup/ names; the maildir, tag dumps, Immich compose file
# and the owning user are discovered from the restored tree; NFS mountpoints
# come from /etc/fstab.

set -euo pipefail

BACKUP_DIR=/var/lib/restic-backup
OFFLINE_SENTINEL=/var/lib/firstboot.done
ONLINE_SENTINEL=/var/lib/firstboot-online.done

log()  { printf '[firstboot] %s\n' "$*"; }
warn() { printf '[firstboot] WARN: %s\n' "$*" >&2; }
die()  { printf '[firstboot] ERROR: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# offline: host Postgres
# --------------------------------------------------------------------------
host_postgres() {
  local pgdata dump inst dumpmaj n
  pgdata=$(sed -n 's/^Environment=PGDATA=//p' \
             /usr/lib/systemd/system/postgresql.service 2>/dev/null | head -1)
  pgdata=${pgdata:-/var/lib/pgsql/data}
  dump=$BACKUP_DIR/host-postgresql.sql
  [ -f "$dump" ] || die "host PG dump $dump missing"

  # a pg_dumpall restores into the SAME major version only — guard loudly
  inst=$(postgres --version | grep -oE '[0-9]+' | head -1)
  dumpmaj=$(sed -n 's/^-- Dumped from database version \([0-9][0-9]*\).*/\1/p' "$dump" | head -1)
  [ -n "$inst" ] || die "cannot determine installed postgres version"
  if [ -n "$dumpmaj" ] && [ "$inst" != "$dumpmaj" ]; then
    die "PG major mismatch: installed $inst, dump from $dumpmaj (would need pg_upgrade)"
  fi
  log "host PG: installed major $inst matches dump"

  if [ -s "$pgdata/PG_VERSION" ]; then
    log "host PG: $pgdata already initialised — skipping initdb"
  else
    log "host PG: initdb into $pgdata"
    if command -v postgresql-setup >/dev/null; then
      postgresql-setup --initdb
    else
      install -d -o postgres -g postgres "$pgdata"
      runuser -u postgres -- initdb -D "$pgdata"
    fi
  fi

  # start the cluster directly, NOT via systemctl: this unit is ordered
  # Before=postgresql.service, so a systemctl start here would deadlock.
  if ! runuser -u postgres -- pg_ctl -D "$pgdata" status >/dev/null 2>&1; then
    log "host PG: starting cluster with pg_ctl (stopped again before unit exit)"
    runuser -u postgres -- pg_ctl -D "$pgdata" -w -t 300 \
      -l "$pgdata/firstboot-startup.log" start >/dev/null
  fi

  if [ "$(runuser -u postgres -- psql -tAc \
          "SELECT count(*) FROM messages" mailvec 2>/dev/null || echo 0)" -gt 0 ] 2>/dev/null; then
    log "host PG: mailvec.messages already populated — skipping dump load"
  else
    log "host PG: loading $dump (a few minutes)"
    # /var/lib/restic-backup is mode 0700 root — root opens the dump, the
    # postgres psql reads it via inherited stdin. No ON_ERROR_STOP: restoring
    # a pg_dumpall into a fresh cluster hits benign conflicts (bootstrap
    # role) — the row-count assert below is the real gate, same as the
    # proven manual procedure.
    runuser -u postgres -- psql -q < "$dump" >/dev/null || true
  fi
  n=$(runuser -u postgres -- psql -tAc "SELECT count(*) FROM messages" mailvec) \
    || die "host PG: mailvec.messages assert failed after restore"
  log "host PG: ASSERT mailvec.messages row count = $n"

  runuser -u postgres -- pg_ctl -D "$pgdata" -w -m fast stop >/dev/null
  log "host PG: cluster stopped; postgresql.service takes it from here"
}

# --------------------------------------------------------------------------
# offline: notmuch Xapian index + tags
# --------------------------------------------------------------------------
notmuch_rebuild() {
  local ndir user xapian artifact dump count
  ndir=$(find /home -maxdepth 4 -type d -name .notmuch 2>/dev/null | head -1)
  [ -n "$ndir" ] || die "no .notmuch dir found under /home"
  user=$(stat -c %U "$ndir")
  xapian=$ndir/xapian
  log "notmuch: database dir $ndir (user $user)"

  artifact=$BACKUP_DIR/notmuch-xapian.tar.zst
  if [ -f "$artifact" ] && [ -z "$(ls -A "$xapian" 2>/dev/null || true)" ]; then
    # M6 fast-restore artifact (weekly compacted index): extract (~1 min)
    # instead of a full reparse. The tarball holds a top-level xapian/ dir.
    log "notmuch: extracting fast-restore artifact $artifact"
    mkdir -p "$ndir"
    zstd -dc "$artifact" | tar -x -C "$ndir"
    chown -R "$user:$(id -gn "$user")" "$ndir"
  fi

  if [ -n "$(ls -A "$xapian" 2>/dev/null || true)" ]; then
    log "notmuch: index present — 'notmuch new' is incremental (fast)"
  else
    log "notmuch: no index — full 'notmuch new' reparse of the maildir; this is the long pole (10s of minutes)"
  fi
  # The maildir's post-new hook calls into the diary-scripts .venv, which is
  # an ONLINE-phase artifact (uv sync needs the network) — park the hooks dir
  # for this one run. Tag state is reapplied by notmuch restore below anyway,
  # and the hook's normal cadence resumes with mail-sync after first boot.
  local hooks=$ndir/hooks parked= nm_rc=0
  if [ -d "$hooks" ]; then
    parked=$ndir/hooks.firstboot-parked
    mv "$hooks" "$parked"
  fi
  runuser -u "$user" -- notmuch new || nm_rc=$?
  [ -z "$parked" ] || mv "$parked" "$hooks"
  [ "$nm_rc" -eq 0 ] || die "notmuch new failed (rc=$nm_rc)"

  dump=$(find /home -maxdepth 6 -path '*notmuch-dumps*' -name 'tags-*.dump' 2>/dev/null | sort | tail -1)
  if [ -n "$dump" ]; then
    log "notmuch: restoring tags from $dump"
    runuser -u "$user" -- notmuch restore --input="$dump"
  else
    warn "notmuch: no tag dump found — tags NOT restored"
  fi
  count=$(runuser -u "$user" -- notmuch count '*') \
    || die "notmuch: count failed after rebuild"
  log "notmuch: ASSERT message count = $count"
}

# --------------------------------------------------------------------------
# online: NFS + fscache
# --------------------------------------------------------------------------
nfs_and_fscache() {
  # The NFS photo library's fstab entry carries the `fsc` option and
  # x-systemd.requires=cachefilesd.service, so the cache daemon must be up
  # for the mount to succeed. The cache lives on a dedicated xfs disk
  # (LABEL=fscache; a second virtio disk on the real box) which is NOT part
  # of the image. Three cases: labeled disk present -> just mount; blank
  # second disk -> mkfs it (what immich/setup-fscache itself would do; the
  # fstab line, drop-in and package are already in the restored tree); no
  # disk -> warn, the NFS mount may fail until a cache disk is attached.
  local mp
  if blkid -L fscache >/dev/null 2>&1; then
    log "fscache: labeled cache disk present"
  elif [ -b /dev/vdb ] && [ -z "$(blkid -o value -s TYPE /dev/vdb 2>/dev/null)" ]; then
    log "fscache: blank /dev/vdb — mkfs.xfs -L fscache"
    mkfs.xfs -f -L fscache /dev/vdb >/dev/null
  else
    warn "fscache: no cache disk found — NFS library will run without local cache"
  fi
  if blkid -L fscache >/dev/null 2>&1; then
    mountpoint -q /var/cache/fscache || mount /var/cache/fscache
    systemctl start cachefilesd.service || warn "cachefilesd failed to start"
  fi

  while read -r mp; do
    [ -n "$mp" ] || continue
    if mountpoint -q "$mp"; then
      log "nfs: $mp already mounted"
    else
      log "nfs: mounting $mp"
      mount "$mp"   # network-dependent: fails on the isolated verify boot (expected)
    fi
  done < <(awk '$3=="nfs" && $1 !~ /^#/{print $2}' /etc/fstab)
}

# --------------------------------------------------------------------------
# online: Immich docker stack + in-container DB restore
# --------------------------------------------------------------------------
find_immich_compose() {
  local f
  while IFS= read -r f; do
    if grep -q 'immich_postgres' "$f" 2>/dev/null; then printf '%s\n' "$f"; return 0; fi
  done < <(find /home -maxdepth 4 -name 'docker-compose.yml' 2>/dev/null)
  return 1
}

immich_stack() {
  local compose dir dbuser dbname dump rows
  compose=$(find_immich_compose) || die "no immich compose file found under /home"
  dir=$(dirname "$compose")
  log "immich: compose file $compose"

  # docker may have failed at boot (its drop-in requires the NFS mount) —
  # start it now that the mount is up.
  systemctl start docker.service

  log "immich: docker compose pull (network)"
  docker compose -f "$compose" pull

  dbuser=$(sed -n 's/^DB_USERNAME=//p' "$dir/.env" 2>/dev/null | head -1); dbuser=${dbuser:-postgres}
  dbname=$(sed -n 's/^DB_DATABASE_NAME=//p' "$dir/.env" 2>/dev/null | head -1); dbname=${dbname:-immich}

  # bring up ONLY the database first: if immich-server starts against the
  # empty cluster it runs its own migrations and the pg_dumpall restore then
  # fights them. Restore first, full stack after.
  docker compose -f "$compose" up -d database

  log "immich: waiting for immich_postgres to accept connections"
  for _ in $(seq 1 90); do
    docker exec immich_postgres pg_isready -U "$dbuser" -q && break
    sleep 2
  done
  docker exec immich_postgres pg_isready -U "$dbuser" -q \
    || die "immich_postgres never became ready"

  dump=$BACKUP_DIR/immich-postgresql.sql
  [ -f "$dump" ] || die "immich dump $dump missing"
  rows=$(docker exec immich_postgres psql -U "$dbuser" -d "$dbname" -tAc \
    "SELECT count(*) FROM asset" 2>/dev/null || echo 0)
  if [ "${rows:-0}" -gt 0 ]; then
    log "immich: DB already populated (asset=$rows) — skipping dump restore"
  else
    log "immich: restoring $dump into the fresh container cluster (the vchord index build is single-threaded — patience)"
    # as with the host dump: tolerate benign conflicts, gate on the row count
    docker exec -i immich_postgres psql -q -U "$dbuser" -d postgres -f - < "$dump" >/dev/null || true
    rows=$(docker exec immich_postgres psql -U "$dbuser" -d "$dbname" -tAc \
      "SELECT count(*) FROM asset" 2>/dev/null || echo 0)
  fi
  [ "${rows:-0}" -gt 0 ] || die "immich: asset table empty after restore"
  log "immich: ASSERT asset row count = $rows"

  log "immich: bringing up the full stack"
  docker compose -f "$compose" up -d
  docker compose -f "$compose" ps
}

# --------------------------------------------------------------------------
# online: gitignored user tooling (.venv + Playwright browsers)
# --------------------------------------------------------------------------
user_tooling() {
  # Restore-gap lessons (2026-06-24 playwright, 2026-07-20 ~/go): anything
  # under an excluded cache dir or gitignored path that a service needs at
  # runtime must be re-provisioned, not assumed restored. The remaining two
  # are the diary-scripts .venv and the Playwright chromium build; both need
  # the network, hence the online phase.
  local scripts home user uv
  scripts=$(find /home -maxdepth 3 -type d -name diary-scripts 2>/dev/null | head -1)
  if [ -z "$scripts" ]; then
    warn "no diary-scripts dir under /home — skipping venv/playwright"
    return 0
  fi
  home=$(dirname "$(dirname "$scripts")")
  user=$(stat -c %U "$home")
  uv=$home/.local/bin/uv
  [ -x "$uv" ] || uv=$(command -v uv 2>/dev/null) || {
    warn "uv not found — skipping venv/playwright"; return 0; }

  if [ -x "$scripts/.venv/bin/python" ]; then
    log "venv: $scripts/.venv already present — skipping uv sync"
  else
    log "venv: uv sync --frozen in $scripts"
    runuser -u "$user" -- "$uv" sync --frozen --project "$scripts"
  fi
  log "playwright: installing chromium-headless-shell (user $user)"
  runuser -u "$user" -- "$uv" run --frozen --no-sync --project "$scripts" \
    playwright install chromium-headless-shell
}

# --------------------------------------------------------------------------
phase=${1:-}
case "$phase" in
  offline)
    [ -e "$OFFLINE_SENTINEL" ] && { log "offline phase already done"; exit 0; }
    host_postgres
    notmuch_rebuild
    touch "$OFFLINE_SENTINEL"
    systemctl disable firstboot-reconstitute-offline.service 2>/dev/null || true
    log "offline phase complete — sentinel written, unit disabled"
    ;;
  online)
    [ -e "$ONLINE_SENTINEL" ] && { log "online phase already done"; exit 0; }
    [ -e "$OFFLINE_SENTINEL" ] \
      || die "offline sentinel missing — refusing to run the online phase"
    nfs_and_fscache
    immich_stack
    user_tooling
    touch "$ONLINE_SENTINEL"
    systemctl disable firstboot-reconstitute-online.service 2>/dev/null || true
    log "online phase complete — sentinel written, unit disabled"
    ;;
  *)
    echo "usage: firstboot-reconstitute offline|online" >&2
    exit 64
    ;;
esac
