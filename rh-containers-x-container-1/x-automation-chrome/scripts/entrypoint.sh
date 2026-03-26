#!/usr/bin/env bash
set -e

# Start Xvfb (virtual display)
export DISPLAY=:99
Xvfb :99 -screen 0 1440x900x24 &
sleep 1

# Start VNC server (x11vnc) on display :99
x11vnc -display :99 -forever -shared -rfbport 5900 -nopw &
sleep 1

# Start noVNC (websocket proxy) on port 7900
/opt/bin/noVNC/utils/novnc_proxy --vnc localhost:5900 --listen 7900 &
sleep 1

# Chromium ignores --remote-debugging-address on some builds.
# Use socat to forward 0.0.0.0:9222 → 127.0.0.1:19222 so the host can reach CDP.
socat TCP-LISTEN:9222,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:19222 &

# Legacy prototype entrypoint. Production target is Google Chrome, not Chromium.
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

# Launch Google Chrome with CDP on loopback port 19222
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
