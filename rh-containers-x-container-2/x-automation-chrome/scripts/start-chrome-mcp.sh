#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BROWSER_URL="${BROWSER_URL:-http://127.0.0.1:9222}"
PACKAGE_SPEC="${CHROME_MCP_PACKAGE:-chrome-devtools-mcp@latest}"

cd "$BASE_DIR"
exec npx -y "$PACKAGE_SPEC" --browserUrl="$BROWSER_URL" "$@"
