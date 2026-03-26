#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$BASE_DIR/state/host-chrome.pid"
PROFILE_DIR="$BASE_DIR/host-chrome-profile"

find_existing_pids() {
  pgrep -f -- "--user-data-dir=$PROFILE_DIR" || true
}

STOPPED=0

if [[ ! -f "$PID_FILE" ]]; then
  PIDS="$(find_existing_pids)"
  if [[ -z "${PIDS:-}" ]]; then
    echo "No tracked host Chrome PID file found"
    exit 0
  fi
else
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "Stopped host Chrome PID $PID"
    STOPPED=1
  fi
fi

for PID in $(find_existing_pids); do
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "Stopped host Chrome PID $PID"
    STOPPED=1
  fi
done

if [[ "$STOPPED" -eq 0 ]]; then
  echo "Tracked host Chrome PID was not running"
fi

rm -f "$PID_FILE"
