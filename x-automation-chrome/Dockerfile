FROM selenium/standalone-chrome:latest

# Production target: real Google Chrome in an isolated browser container.
# On Apple Silicon hosts this image runs under linux/amd64 emulation because
# Google Chrome for Linux is not distributed as a native arm64 container target.
#
# We keep the Selenium image only for its Xvfb + VNC + noVNC stack (via
# supervisord), but replace the Selenium server with a direct Google Chrome
# launch exposing CDP.

USER root

# System packages: socat for CDP proxy, Chrome, Python, Node.js
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       socat google-chrome-stable python3-pip curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && pip3 install --break-system-packages aiohttp mcp \
    && rm -rf /var/lib/apt/lists/*

# Install chrome-devtools-mcp globally so runners can find it
RUN npm install -g chrome-devtools-mcp

# Replace Selenium standalone with direct Google Chrome + CDP + socat proxy.
COPY config/chrome-direct.conf /etc/supervisor/conf.d/selenium.conf
COPY scripts/start-chrome-direct.sh /opt/bin/start-chrome-direct.sh
RUN chmod +x /opt/bin/start-chrome-direct.sh

# Copy x-automation agent, runner, and WS server into the container.
# The WS server wraps the agent's task dispatch over WebSocket so the
# scheduler can submit tasks from a remote machine.
COPY agent/ /opt/x-automation/agent/
COPY runner/ /opt/x-automation/runner/
COPY api/ /opt/x-automation/api/
RUN mkdir -p /opt/x-automation/tasks/{pending,active,completed,failed} \
             /opt/x-automation/out \
             /opt/x-automation/state \
    && chown -R seluser:seluser /opt/x-automation

EXPOSE 18000

USER seluser

# Use the base image entrypoint (supervisord via entry_point.sh)
