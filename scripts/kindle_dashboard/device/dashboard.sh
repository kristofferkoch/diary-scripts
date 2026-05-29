#!/bin/sh
# /mnt/us/dashboard/dashboard.sh
# Periodically fetch a PNG and render it via eips.
#
# Goals:
#   - Be invisible. Conditional-GET (If-None-Match / ETag) skips the render
#     entirely when the server says nothing has changed.
#   - Don't flash. Use eips's partial-update mode by default; do a full
#     refresh once per day after 03:00 (or on first run) to clear ghosting.
#   - Sip battery. Between refreshes the device suspends to RAM (rtcwake on
#     the SNVS RTC); e-ink holds the last frame at ~0 power. Without this the
#     SoC + WiFi idle awake 24/7 and the battery dies in under a day.
#
# NOTE: the canonical copy of this script lives in the diary repo at
# scripts/kindle_dashboard/device/dashboard.sh. Deploy to the Kindle at
# /mnt/us/dashboard/dashboard.sh. The older exampleuser/jailbreak-kindle
# repo on the dev host is stale (predates ETag + suspend); resync it from here.

CONFIG=/mnt/us/dashboard/dashboard.conf
[ -f "$CONFIG" ] && . "$CONFIG"

URL="${URL:-https://example.invalid/dashboard.png}"
INTERVAL="${INTERVAL:-3600}"
KIOSK="${KIOSK:-0}"
BOOT_RETRY="${BOOT_RETRY:-15}"
RTC="${RTC:-rtc1}"          # SNVS RTC — rtc0 (bd71827) has no wakeup capability
TMP=/tmp/dashboard.png
LOG=/mnt/us/dashboard/dashboard.log
ETAG_FILE=/mnt/us/dashboard/last.etag
HEADERS_FILE=/tmp/dashboard.headers
FULL_REFRESH_DONE_FILE=/mnt/us/dashboard/last-full-refresh
FULL_REFRESH_HOUR=3

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
[ -f "$LOG" ] && { tail -n 500 "$LOG" > "$LOG.t" && mv "$LOG.t" "$LOG"; }
mkdir -p "$(dirname "$LOG")"
log "starting (URL=$URL interval=${INTERVAL}s kiosk=$KIOSK rtc=$RTC)"

kiosk_kill() {
  [ "$KIOSK" = "1" ] || return 0
  for svc in framework pillow statusbar webreader appmgrd; do
    initctl stop "$svc" >/dev/null 2>&1
  done
}

# wifi_up — re-associate wlan0 after a resume. Proven ~2 s via `wpa_cli
# reconnect`; escalates to an interface bounce if no IP within the grace
# window. Never blocks the loop forever — returns regardless so the next
# fetch (and the next suspend/wake) can retry.
wifi_up() {
  wpa_cli reconnect >/dev/null 2>&1
  i=0
  while [ $i -lt 20 ]; do
    ifconfig wlan0 2>/dev/null | grep -q "inet addr" && return 0
    sleep 2; i=$((i+2))
  done
  log "wlan0 slow to return — bouncing interface"
  ifconfig wlan0 down >/dev/null 2>&1; sleep 1; ifconfig wlan0 up >/dev/null 2>&1
  wpa_cli reconnect >/dev/null 2>&1
  i=0
  while [ $i -lt 30 ]; do
    ifconfig wlan0 2>/dev/null | grep -q "inet addr" && return 0
    sleep 2; i=$((i+2))
  done
  log "!! wlan0 did not return within grace window — will retry next cycle"
  return 1
}

# suspend_for SECS — suspend to RAM for SECS, waking on the SNVS RTC. The
# e-ink image persists across suspend at ~0 power. WiFi is torn down by the
# suspend and brought back by wifi_up() on resume. Falls back to plain sleep
# if rtcwake fails so the loop never busy-spins.
suspend_for() {
  secs="$1"
  if rtcwake -d "$RTC" -m mem -s "$secs" >/dev/null 2>&1; then
    log "resumed from suspend (${secs}s)"
    wifi_up
  else
    log "rtcwake failed (rtc=$RTC) — sleeping ${secs}s awake instead"
    sleep "$secs"
  fi
}

# fetch — sends If-None-Match, returns:
#   0 = new content saved to $TMP (and $ETAG_FILE updated)
#   1 = error
#   2 = 304 Not Modified (no render needed)
fetch() {
  etag=""
  [ -f "$ETAG_FILE" ] && etag=$(cat "$ETAG_FILE")
  rm -f "$HEADERS_FILE"
  if [ -n "$etag" ]; then
    status=$(curl -sSL --max-time 30 \
                  -H "If-None-Match: $etag" \
                  -D "$HEADERS_FILE" \
                  -w '%{http_code}' \
                  -o "$TMP.new" "$URL")
  else
    status=$(curl -sSL --max-time 30 \
                  -D "$HEADERS_FILE" \
                  -w '%{http_code}' \
                  -o "$TMP.new" "$URL")
  fi
  rc=$?
  if [ $rc -ne 0 ]; then
    log "curl failed rc=$rc"
    return 1
  fi
  case "$status" in
    304) return 2 ;;
    200)
      if [ ! -s "$TMP.new" ]; then
        log "200 but empty body"
        return 1
      fi
      mv "$TMP.new" "$TMP"
      grep -i '^etag:' "$HEADERS_FILE" \
        | awk '{print $2}' | tr -d '\r' > "$ETAG_FILE"
      return 0
      ;;
    *)
      log "fetch unexpected status=$status"
      return 1
      ;;
  esac
}

needs_full_refresh() {
  today=$(date '+%Y-%m-%d')
  if [ ! -f "$FULL_REFRESH_DONE_FILE" ]; then
    return 0
  fi
  last=$(cat "$FULL_REFRESH_DONE_FILE")
  if [ "$last" = "$today" ]; then
    return 1
  fi
  hour=$(date '+%H')
  if [ "$hour" -ge "$FULL_REFRESH_HOUR" ] 2>/dev/null; then
    return 0
  fi
  return 1
}

render() {
  if needs_full_refresh; then
    eips -c >/dev/null 2>&1
    eips -g "$1" -f >/dev/null 2>&1
    date '+%Y-%m-%d' > "$FULL_REFRESH_DONE_FILE"
    log "render (full)"
  else
    eips -g "$1" >/dev/null 2>&1
    log "render (partial)"
  fi
}

first_ok=0
while :; do
  kiosk_kill
  fetch
  rc=$?
  case "$rc" in
    0)
      render "$TMP"
      log "OK $(wc -c < "$TMP") bytes"
      first_ok=1
      ;;
    2)
      log "unchanged (304)"
      first_ok=1
      ;;
    *)
      log "ERROR fetch failed url=$URL"
      ;;
  esac
  lipc-set-prop com.lab126.powerd flIntensity 0 >/dev/null 2>&1
  if [ "$first_ok" = "0" ]; then
    # Still booting / WiFi associating (cold assoc takes 4-6 min). Stay awake
    # with cheap retries until the first successful fetch.
    sleep "$BOOT_RETRY"
  else
    suspend_for "$INTERVAL"
  fi
done
