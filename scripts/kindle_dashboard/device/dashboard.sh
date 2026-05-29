#!/bin/sh
# /mnt/us/dashboard/dashboard.sh  —  THIN BOOTSTRAP
#
# Device-side primitives ONLY (WiFi resume + RTC suspend + fetch-and-exec).
# All policy — what to render, the refresh cadence, maintenance/stay-awake —
# lives in control.sh, which this loop fetches fresh from the server on every
# wake and executes. So behaviour is changed server-side (edit control.sh in
# the diary repo) without ever redeploying this file. This file should change
# rarely; when it must, flip the server maintenance flag so the device stays
# awake and is reachable for an SSH deploy (no wake-window racing).
#
# Canonical copy: diary repo scripts/kindle_dashboard/device/dashboard.sh.
# Runs under BusyBox ash. Deploy to /mnt/us/dashboard/dashboard.sh.
#
# SECURITY: this curl|exec's a script as root over plaintext LAN HTTP. That
# is RCE for anyone who can serve on $BASE on the LAN — acceptable for a
# trusted home LAN, not beyond it. The marker check below only guards against
# executing a truncated download or an error page, not a hostile server.
#
# Each wake:
#   1. wifi_up                      — re-associate wlan0 after resume
#   2. fetch_control                — GET $BASE/control.sh, verify marker
#   3. sh control.sh                — kiosk-kill, PNG fetch+render, and write
#                                     the next-sleep decision to $DECISION
#   4. suspend $DECISION seconds    — or, if it's 0, stay awake (maintenance)
# Failures never brick the loop: a bad control fetch just renders nothing this
# cycle and retries after $FALLBACK_INTERVAL.

CONFIG=/mnt/us/dashboard/dashboard.conf
[ -f "$CONFIG" ] && . "$CONFIG"
BASE="${BASE:-http://10.0.0.206:8801}"
RTC="${RTC:-rtc1}"                       # SNVS RTC; rtc0 (bd71827) has no wakeup
BOOT_RETRY="${BOOT_RETRY:-15}"
FALLBACK_INTERVAL="${FALLBACK_INTERVAL:-900}"
MAINT_SLEEP="${MAINT_SLEEP:-30}"
KIOSK="${KIOSK:-1}"
SIGN_PUBKEY="${SIGN_PUBKEY:-/mnt/us/dashboard/sign-ec.pub}"   # ECDSA P-256 PEM
export BASE KIOSK                        # control.sh inherits these
CONTROL=/tmp/control.sh
DECISION=/tmp/kindle_decision
LOG=/mnt/us/dashboard/dashboard.log

log() { echo "$(date '+%F %T') [boot] $*" >> "$LOG"; }
[ -f "$LOG" ] && { tail -n 500 "$LOG" > "$LOG.t" && mv "$LOG.t" "$LOG"; }
mkdir -p "$(dirname "$LOG")"
log "bootstrap starting (base=$BASE rtc=$RTC)"

# Re-associate wlan0 after a resume. ~2-3 s via wpa_cli reconnect; escalates
# to an interface bounce if slow. Never blocks forever.
wifi_up() {
  wpa_cli reconnect >/dev/null 2>&1
  i=0
  while [ $i -lt 20 ]; do
    ifconfig wlan0 2>/dev/null | grep -q "inet addr" && return 0
    sleep 2; i=$((i+2))
  done
  log "wlan0 slow — bouncing interface"
  ifconfig wlan0 down >/dev/null 2>&1; sleep 1; ifconfig wlan0 up >/dev/null 2>&1
  wpa_cli reconnect >/dev/null 2>&1
  i=0
  while [ $i -lt 30 ]; do
    ifconfig wlan0 2>/dev/null | grep -q "inet addr" && return 0
    sleep 2; i=$((i+2))
  done
  log "!! wlan0 did not return — will retry next cycle"
  return 1
}

# Suspend to RAM for $1 seconds, waking on the SNVS RTC. E-ink holds the
# frame at ~0 W. The power button (snvs-powerkey) also wakes early. Falls
# back to plain sleep if rtcwake fails so the loop never busy-spins.
suspend_for() {
  secs="$1"
  if rtcwake -d "$RTC" -m mem -s "$secs" >/dev/null 2>&1; then
    log "resumed from suspend (${secs}s)"
  else
    log "rtcwake failed (rtc=$RTC) — sleeping ${secs}s awake"
    sleep "$secs"
  fi
}

# Fetch control.sh and AUTHENTICATE it before trusting it. The server signs
# the exact served bytes with an ECDSA P-256 key (X-Control-Sig: base64 DER,
# SHA-256); we verify against the public key deployed over SSH. A bad
# network fetch, missing/forged signature, or missing marker all return 1 so
# the bootstrap skips execution this cycle. Returns 0 only on a verified
# script.
fetch_control() {
  hdr=/tmp/control.hdr; sig=/tmp/control.sig
  rm -f "$CONTROL.new" "$hdr" "$sig"
  curl -fsS --max-time 30 -D "$hdr" -o "$CONTROL.new" "$BASE/control.sh" 2>/dev/null || return 1
  [ -s "$CONTROL.new" ] || return 1
  head -1 "$CONTROL.new" | grep -q '^#!CONTROL/' || { log "control.sh missing marker — refusing"; return 1; }
  if [ ! -f "$SIGN_PUBKEY" ]; then
    log "!! signing pubkey $SIGN_PUBKEY missing — refusing unsigned control"
    return 1
  fi
  grep -i '^x-control-sig:' "$hdr" | awk '{print $2}' | tr -d '\r' | openssl base64 -d -A > "$sig" 2>/dev/null
  [ -s "$sig" ] || { log "!! no X-Control-Sig header — refusing"; return 1; }
  if ! openssl dgst -sha256 -verify "$SIGN_PUBKEY" -signature "$sig" "$CONTROL.new" >/dev/null 2>&1; then
    log "!! control.sh signature verify FAILED — refusing"
    return 1
  fi
  mv "$CONTROL.new" "$CONTROL"
  return 0
}

booted=0
while :; do
  wifi_up
  rm -f "$DECISION"
  if fetch_control; then
    sh "$CONTROL"            # renders + writes $DECISION
    booted=1
  else
    log "control fetch failed (base=$BASE)"
  fi

  if [ "$booted" = "0" ]; then
    # Never fetched successfully yet (boot / WiFi still associating, 4-6 min
    # cold). Stay awake and retry quickly rather than suspend.
    sleep "$BOOT_RETRY"
    continue
  fi

  secs=$(cat "$DECISION" 2>/dev/null)
  case "$secs" in ''|*[!0-9]*) secs="$FALLBACK_INTERVAL" ;; esac
  if [ "$secs" = "0" ]; then
    log "maintenance — staying awake (${MAINT_SLEEP}s tick)"
    sleep "$MAINT_SLEEP"
  else
    suspend_for "$secs"
  fi
done
