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

BASE="${BASE:-http://192.0.2.10:8801}"
KIOSK="${KIOSK:-1}"
DECISION=/tmp/kindle_decision
LOG=/mnt/us/dashboard/dashboard.log
# last PNG lives on FAT (/mnt/us), not tmpfs: after a reboot the framebuffer
# is wiped white but the ETag cache also survives, so a 304 would otherwise
# leave the device with no image to repaint (white screen until the content
# next changed — seen 2026-08-05).
TMP=/mnt/us/dashboard/last.png
ETAG_FILE=/mnt/us/dashboard/last.etag
HEADERS=/tmp/dashboard.headers
FULL_DONE=/mnt/us/dashboard/last-full-refresh
RENDERED=/tmp/.rendered-this-boot   # tmpfs on purpose: cleared each reboot

# --- policy knobs (server-tunable: edit here, lands next wake) ---
FULL_HOUR=3                       # daily full e-ink refresh after this local hour
PEAK_INTERVAL=300                 # 5 min wake cadence inside PEAK_WINDOWS
OFFPEAK_INTERVAL=900              # 15 min otherwise
PEAK_WINDOWS="6-9 15-20"          # half-open local-hour ranges (rain-watch windows)

log() { echo "$(date '+%F %T') [ctrl] $*" >> "$LOG"; }

# --- battery telemetry: read now, reported to server via X-Batt-*
#     headers on the dashboard.png fetch below (the server writes the durable
#     log on real disk). The device's own /var/log is tmpfs and overwrites a
#     charger-unplug event within ~30 min, so on-device logging can't answer
#     "when was it unplugged" after the fact. bd71827 = moonshine's PMIC.
#     Status spaces → '_' ("Not charging" → "Not_charging") so the value stays
#     a single header token under the unquoted $BATT_HDRS word-split. ---
batt_cap=$(cat /sys/class/power_supply/bd71827_bat/capacity 2>/dev/null)
batt_st=$(cat /sys/class/power_supply/bd71827_bat/status 2>/dev/null | tr ' ' '_')
ac_on=$(cat /sys/class/power_supply/bd71827_ac/online 2>/dev/null)
BATT_HDRS="-H X-Batt-Cap:${batt_cap:-?} -H X-Batt-Status:${batt_st:-?} -H X-Batt-Ac:${ac_on:-?}"

# --- maintenance? a server flag forces stay-awake so we can SSH in calmly ---
maint=$(curl -fsS --max-time 10 "$BASE/control/maintenance" 2>/dev/null | tr -d '\r\n ')
if [ "$maint" = "1" ]; then
  log "maintenance flag set — requesting stay-awake"
  echo 0 > "$DECISION"
fi

# --- kiosk kill (these can (re)start late at boot) ---
# bootactions: stock boot-progress splash app; if it never gets "boot
# complete" (framework is stopped below, so it never does) it can wedge in
# an error loop and keep re-painting its progress bar over our render
# (seen 2026-08-05 after a hard power-cycle).
if [ "$KIOSK" = "1" ]; then
  for svc in framework pillow statusbar webreader appmgrd bootactions; do
    initctl stop "$svc" >/dev/null 2>&1
  done
fi

# --- fetch the PNG (conditional GET) and render ---
etag=""; [ -f "$ETAG_FILE" ] && etag=$(cat "$ETAG_FILE")
rm -f "$HEADERS"
if [ -n "$etag" ]; then
  status=$(curl -sSL --max-time 30 $BATT_HDRS -H "If-None-Match: $etag" -D "$HEADERS" -w '%{http_code}' -o "$TMP.new" "$BASE/dashboard.png" 2>/dev/null)
else
  status=$(curl -sSL --max-time 30 $BATT_HDRS -D "$HEADERS" -w '%{http_code}' -o "$TMP.new" "$BASE/dashboard.png" 2>/dev/null)
fi
case "$status" in
  304)
    if [ ! -f "$RENDERED" ] && [ -s "$TMP" ]; then
      # first fetch after a reboot: boot wiped the framebuffer, so repaint
      # from the persisted PNG even though the content is unchanged.
      eips -c >/dev/null 2>&1; eips -g "$TMP" -f >/dev/null 2>&1
      touch "$RENDERED"; log "unchanged (304) but nothing rendered this boot — render (full)"
    else
      log "unchanged (304)"
    fi ;;
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
      touch "$RENDERED"
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
