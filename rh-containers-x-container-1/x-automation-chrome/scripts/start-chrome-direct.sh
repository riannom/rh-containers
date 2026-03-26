#!/usr/bin/env bash
set -e

# Clean stale profile locks from previous container runs
rm -f /home/seluser/chrome-profile/SingletonLock \
      /home/seluser/chrome-profile/SingletonCookie \
      /home/seluser/chrome-profile/SingletonSocket 2>/dev/null || true

# socat forwards 0.0.0.0:9222 → 127.0.0.1:19222
# (Chrome binds loopback inside the container; socat makes it reachable outside)
socat TCP-LISTEN:9222,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:19222 &

CHROME_BIN=""
for candidate in \
  /usr/bin/google-chrome \
  /usr/bin/google-chrome-stable \
  /opt/google/chrome/google-chrome
do
  if [[ -x "$candidate" ]]; then
    CHROME_BIN="$candidate"
    break
  fi
done

if [[ -z "$CHROME_BIN" ]]; then
  echo "Google Chrome binary not found in container" >&2
  exit 1
fi

exec "$CHROME_BIN" \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --remote-debugging-port=19222 \
  --disable-blink-features=AutomationControlled \
  --user-data-dir=/home/seluser/chrome-profile \
  --window-size=1440,900 \
  --lang=en-US \
  --no-first-run \
  --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  "about:blank"
