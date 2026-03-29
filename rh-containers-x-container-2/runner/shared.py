"""Shared utilities for x-automation runner scripts."""
from __future__ import annotations

import json
import os
from pathlib import Path


OUT_DIR = Path(os.environ.get("X_AUTOMATION_OUT_DIR", Path(__file__).resolve().parent.parent / "out"))
OUT_DIR.mkdir(parents=True, exist_ok=True)


def resolve_browser_url() -> str:
    """Resolve the Chrome DevTools Protocol URL from environment variables."""
    return os.environ.get("BROWSER_URL") or os.environ.get("CDP_URL") or "http://127.0.0.1:9222"


def write_json(name: str, payload: dict) -> None:
    """Write a JSON result file to OUT_DIR."""
    (OUT_DIR / name).write_text(json.dumps(payload, indent=2))
