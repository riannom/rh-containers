#!/usr/bin/env python3
"""
CLI tool to submit tasks to the x-automation agent.

Usage:
  python submit_task.py verify_session
  python submit_task.py create_list --list-name "Breadth | Tier A Macro" --list-desc "Breadth lane macro feed"
  python submit_task.py follow_accounts --handles user1 user2
  python submit_task.py collect_feeds --list-urls "https://x.com/i/lists/123" --labels tier-a
  python submit_task.py pull_avatars --handles user1 user2
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TASKS_DIR = Path(__file__).resolve().parent.parent / "tasks"
PENDING = TASKS_DIR / "pending"
COMPLETED = TASKS_DIR / "completed"
FAILED = TASKS_DIR / "failed"


def submit(task_type: str, params: dict, wait: bool = True, timeout: int = 300) -> Optional[dict]:
    """Submit a task and optionally wait for completion."""
    for d in [PENDING, COMPLETED, FAILED]:
        d.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    task_id = f"{task_type}-{ts}"
    task = {"type": task_type, "params": params, "submitted_at": datetime.now(timezone.utc).isoformat()}

    task_file = PENDING / f"{task_id}.json"
    task_file.write_text(json.dumps(task, indent=2))
    print(f"Submitted: {task_id}")

    if not wait:
        return None

    completed_file = COMPLETED / f"{task_id}.json"
    failed_file = FAILED / f"{task_id}.json"
    deadline = time.time() + timeout

    while time.time() < deadline:
        if completed_file.exists():
            result = json.loads(completed_file.read_text())
            print(json.dumps(result, indent=2))
            return result
        if failed_file.exists():
            result = json.loads(failed_file.read_text())
            print(json.dumps(result, indent=2), file=sys.stderr)
            return result
        time.sleep(1)

    print(f"Timeout waiting for {task_id}", file=sys.stderr)
    return None


def main():
    ap = argparse.ArgumentParser(description="Submit task to x-automation agent")
    ap.add_argument("task_type", choices=[
        "verify_session", "create_list", "follow_accounts", "manage_list",
        "collect_feeds", "collect_relationships", "pull_avatars",
    ])
    ap.add_argument("--handles", nargs="*", default=[])
    ap.add_argument("--list-urls", nargs="*", default=[])
    ap.add_argument("--labels", nargs="*", default=[])
    ap.add_argument("--list-url", default=None)
    ap.add_argument("--list-name", default=None)
    ap.add_argument("--list-desc", default="")
    ap.add_argument("--private", action="store_true", default=True)
    ap.add_argument("--add", nargs="*", default=[])
    ap.add_argument("--remove", nargs="*", default=[])
    ap.add_argument("--direction", default="both", choices=["following", "followers", "both"])
    ap.add_argument("--edge-limit", type=int, default=100)
    ap.add_argument("--max-scrolls", type=int, default=8)
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    params = {}
    if args.task_type == "follow_accounts":
        params = {"handles": args.handles, "verify": True}
    elif args.task_type == "create_list":
        params = {"list_name": args.list_name, "list_description": args.list_desc, "private": args.private}
    elif args.task_type == "manage_list":
        params = {"list_url": args.list_url, "add": args.add, "remove": args.remove}
    elif args.task_type == "collect_feeds":
        params = {"list_urls": args.list_urls, "labels": args.labels, "max_scrolls": args.max_scrolls}
    elif args.task_type == "collect_relationships":
        params = {"handles": args.handles, "direction": args.direction, "edge_limit": args.edge_limit}
    elif args.task_type == "pull_avatars":
        params = {"handles": args.handles}
    # verify_session has no params

    submit(args.task_type, params, wait=not args.no_wait, timeout=args.timeout)


if __name__ == "__main__":
    main()
