#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="$BASE_DIR/state"
PROFILE_DIR="$BASE_DIR/host-chrome-profile"
PID_FILE="$STATE_DIR/host-chrome.pid"
CHROME_APP="${CHROME_APP:-/Applications/Google Chrome.app}"
CDP_PORT="${CDP_PORT:-9223}"
CDP_URL="http://127.0.0.1:$CDP_PORT/json/version"
OPEN_CMD="${OPEN_CMD:-/usr/bin/open}"

mkdir -p "$STATE_DIR" "$PROFILE_DIR"

if [[ ! -d "$CHROME_APP" ]]; then
  echo "Chrome app not found at: $CHROME_APP" >&2
  exit 1
fi

find_existing_pid() {
  pgrep -f -- "--user-data-dir=$PROFILE_DIR" | head -n 1
}

if curl -fsS "$CDP_URL" >/dev/null 2>&1; then
  EXISTING_PID="$(find_existing_pid || true)"
  if [[ -n "${EXISTING_PID:-}" ]]; then
    echo "$EXISTING_PID" > "$PID_FILE"
    echo "Host Chrome already running with PID $EXISTING_PID on http://127.0.0.1:$CDP_PORT"
  else
    rm -f "$PID_FILE"
    echo "Chrome debug endpoint already responding on http://127.0.0.1:$CDP_PORT"
  fi
  exit 0
fi

rm -f "$PID_FILE"
"$OPEN_CMD" -na "$CHROME_APP" --args \
  --remote-debugging-port="$CDP_PORT" \
  --user-data-dir="$PROFILE_DIR" \
  --profile-directory="Default" \
  --disable-blink-features=AutomationControlled \
  --no-first-run \
  --no-default-browser-check \
  --new-window \
  "https://x.com/home" \
  >"$STATE_DIR/host-chrome.log" 2>&1

for _ in $(seq 1 30); do
  if curl -fsS "$CDP_URL" >/dev/null 2>&1; then
    PID="$(find_existing_pid || true)"
    if [[ -n "${PID:-}" ]]; then
      echo "$PID" > "$PID_FILE"
      echo "Started host Chrome with PID $PID on http://127.0.0.1:$CDP_PORT"
    else
      echo "Started host Chrome on http://127.0.0.1:$CDP_PORT"
    fi
    exit 0
  fi
  sleep 1
done

echo "Chrome did not expose CDP on http://127.0.0.1:$CDP_PORT within 30s" >&2
exit 1
