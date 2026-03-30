#!/usr/bin/env bash
# Fix volume-mount ownership on first start.
# Docker creates volume-mount directories as root, but the container runs as seluser.
mkdir -p /home/seluser/chrome-profile
chown -R seluser:seluser /home/seluser/chrome-profile
# State directory is a volume mount — ensure writable by seluser
mkdir -p /opt/x-automation/state
chown -R seluser:seluser /opt/x-automation/state
