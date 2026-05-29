#!CONTROL/1
# control.sh — THE CONTROL PLANE for the wall Kindle.
#
# Served by kindle_dashboard (GET $BASE/control.sh) and executed on the
# device by the thin bootstrap on every wake. Edit THIS file in the diary
# repo to change device behaviour — what to render, the refresh cadence,
# maintenance/stay-awake — and the device picks it up on its next wake. No
# redeploy of the device-side bootstrap needed.
#
# The server signs the exact bytes it serves with an ECDSA P-256 key; the
# bootstrap verifies that signature against the public key deployed over SSH
# before it ever runs this file. So this script is authenticated, not just
# trusted-because-LAN. (The leading #!CONTROL/1 marker is a cheap
# pre-check; the signature is the real gate.)
#
# Runs under BusyBox ash. Inherits from the bootstrap: $BASE, $KIOSK.
# Contract: write the next-sleep decision (integer seconds; 0 = stay awake
# for maintenance) to $DECISION (/tmp/kindle_decision). Anything else and
# the bootstrap falls back to a safe default.

BASE="${BASE:-http://10.0.0.206:8801}"
KIOSK="${KIOSK:-1}"
DECISION=/tmp/kindle_decision
LOG=/mnt/us/dashboard/dashboard.log
TMP=/tmp/dashboard.png
ETAG_FILE=/mnt/us/dashboard/last.etag
HEADERS=/tmp/dashboard.headers
FULL_DONE=/mnt/us/dashboard/last-full-refresh

# --- policy knobs (server-tunable: edit here, lands next wake) ---
FULL_HOUR=3                       # daily full e-ink refresh after this local hour
PEAK_INTERVAL=300                 # 5 min wake cadence inside PEAK_WINDOWS
OFFPEAK_INTERVAL=900              # 15 min otherwise
PEAK_WINDOWS="6-9 15-20"          # half-open local-hour ranges (rain-watch windows)

log() { echo "$(date '+%F %T') [ctrl] $*" >> "$LOG"; }

# --- maintenance? a server flag forces stay-awake so we can SSH in calmly ---
maint=$(curl -fsS --max-time 10 "$BASE/control/maintenance" 2>/dev/null | tr -d '\r\n ')
if [ "$maint" = "1" ]; then
  log "maintenance flag set — requesting stay-awake"
  echo 0 > "$DECISION"
fi

# --- kiosk kill (these can (re)start late at boot) ---
if [ "$KIOSK" = "1" ]; then
  for svc in framework pillow statusbar webreader appmgrd; do
    initctl stop "$svc" >/dev/null 2>&1
  done
fi

# --- fetch the PNG (conditional GET) and render ---
etag=""; [ -f "$ETAG_FILE" ] && etag=$(cat "$ETAG_FILE")
rm -f "$HEADERS"
if [ -n "$etag" ]; then
  status=$(curl -sSL --max-time 30 -H "If-None-Match: $etag" -D "$HEADERS" -w '%{http_code}' -o "$TMP.new" "$BASE/dashboard.png" 2>/dev/null)
else
  status=$(curl -sSL --max-time 30 -D "$HEADERS" -w '%{http_code}' -o "$TMP.new" "$BASE/dashboard.png" 2>/dev/null)
fi
case "$status" in
  304) log "unchanged (304)" ;;
  200)
    if [ -s "$TMP.new" ]; then
      mv "$TMP.new" "$TMP"
      grep -i '^etag:' "$HEADERS" | awk '{print $2}' | tr -d '\r' > "$ETAG_FILE"
      today=$(date '+%Y-%m-%d'); hour=$(date '+%H'); hour=${hour#0}; [ -z "$hour" ] && hour=0
      last=""; [ -f "$FULL_DONE" ] && last=$(cat "$FULL_DONE")
      if [ "$last" != "$today" ] && [ "$hour" -ge "$FULL_HOUR" ]; then
        eips -c >/dev/null 2>&1; eips -g "$TMP" -f >/dev/null 2>&1
        echo "$today" > "$FULL_DONE"; log "render (full)"
      else
        eips -g "$TMP" >/dev/null 2>&1; log "render (partial)"
      fi
    else
      log "200 but empty body"
    fi
    ;;
  *) log "fetch status=$status" ;;
esac

# frontlight off (kept off; see KINDLE.md for an evening branch idea)
lipc-set-prop com.lab126.powerd flIntensity 0 >/dev/null 2>&1

# --- decide next interval (unless maintenance already wrote 0) ---
if [ ! -s "$DECISION" ]; then
  h=$(date '+%H'); h=${h#0}; [ -z "$h" ] && h=0
  secs="$OFFPEAK_INTERVAL"
  for w in $PEAK_WINDOWS; do
    s=${w%-*}; e=${w#*-}
    if [ "$h" -ge "$s" ] && [ "$h" -lt "$e" ]; then secs="$PEAK_INTERVAL"; break; fi
  done
  echo "$secs" > "$DECISION"
fi
