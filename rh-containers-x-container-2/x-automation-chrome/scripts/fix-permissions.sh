#!/usr/bin/env bash
# Fix chrome-profile ownership on first start.
# Docker creates volume-mount directories as root, but Chrome runs as seluser.
mkdir -p /home/seluser/chrome-profile
chown -R seluser:seluser /home/seluser/chrome-profile
