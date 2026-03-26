#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "state" / "private_accounts.json"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"accounts": {}}
    data = json.loads(STATE_FILE.read_text())
    if not isinstance(data, dict):
        return {"accounts": {}}
    data.setdefault("accounts", {})
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description="List deferred private/protected X accounts due for retry.")
    ap.add_argument("--all", action="store_true", help="Show all deferred private accounts, not just due ones")
    ap.add_argument("--list-name", default=None, help="Filter to a specific list label")
    args = ap.parse_args()

    state = load_state()
    now = datetime.now(timezone.utc)
    grouped: dict[str, dict] = {}

    for handle, entry in sorted(state.get("accounts", {}).items()):
        list_name = entry.get("list_name") or "unknown"
        if args.list_name and list_name != args.list_name:
            continue
        retry_at = parse_iso(entry.get("next_retry_after"))
        due = retry_at is None or retry_at <= now
        if not args.all and not due:
            continue
        bucket = grouped.setdefault(
            list_name,
            {
                "list_url": entry.get("list_url"),
                "handles": [],
            },
        )
        bucket["handles"].append(
            {
                "handle": handle,
                "last_checked_at": entry.get("last_checked_at"),
                "next_retry_after": entry.get("next_retry_after"),
                "due": due,
            }
        )

    print(json.dumps(grouped, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
