#!/usr/bin/env python3
"""
Task polling loop. Watches tasks/pending/ for JSON task files,
dispatches them via the runner, writes results to tasks/completed/ or tasks/failed/.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import traceback
import os
from datetime import datetime, timezone
from pathlib import Path

from agent.task_registry import READ_ONLY_TASKS, MUTATING_TASKS, ALL_TASKS

TASKS_DIR = Path(__file__).resolve().parent.parent / "tasks"
PENDING = TASKS_DIR / "pending"
ACTIVE = TASKS_DIR / "active"
COMPLETED = TASKS_DIR / "completed"
FAILED = TASKS_DIR / "failed"

POLL_INTERVAL = 2  # seconds


def validate_task(task: dict) -> dict:
    """Validate task shape and enforce collection-safe defaults."""
    if not isinstance(task, dict):
        raise ValueError("Task payload must be a JSON object")

    task_type = task.get("type")
    if task_type not in ALL_TASKS:
        raise ValueError(f"Unsupported task type: {task_type}")

    params = task.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("Task params must be a JSON object")

    if task_type in MUTATING_TASKS and os.environ.get("X_AUTOMATION_ALLOW_MUTATIONS") != "1":
        raise ValueError(
            f"Task type '{task_type}' is disabled by default. "
            "Set X_AUTOMATION_ALLOW_MUTATIONS=1 to enable mutating browser actions."
        )

    if task_type == "follow_accounts":
        handles = params.get("handles", [])
        if not isinstance(handles, list) or not all(isinstance(h, str) and h.strip() for h in handles):
            raise ValueError("follow_accounts requires a non-empty string list in params.handles")

    elif task_type == "collect_feeds":
        list_urls = params.get("list_urls", [])
        labels = params.get("labels", [])
        if not isinstance(list_urls, list) or not all(isinstance(u, str) and u.strip() for u in list_urls):
            raise ValueError("collect_feeds requires params.list_urls as a string list")
        if labels and (
            not isinstance(labels, list)
            or not all(isinstance(label, str) and label.strip() for label in labels)
        ):
            raise ValueError("collect_feeds params.labels must be a string list when provided")

    elif task_type == "collect_relationships":
        handles = params.get("handles", [])
        if not isinstance(handles, list) or not all(isinstance(h, str) and h.strip() for h in handles):
            raise ValueError("collect_relationships requires params.handles as a string list")

    elif task_type == "pull_avatars":
        handles = params.get("handles", [])
        if not isinstance(handles, list) or not all(isinstance(h, str) and h.strip() for h in handles):
            raise ValueError("pull_avatars requires params.handles as a string list")

    elif task_type == "manage_list":
        list_url = params.get("list_url")
        if not isinstance(list_url, str) or not list_url.strip():
            raise ValueError("manage_list requires params.list_url")
    elif task_type == "scrape_list_members":
        list_url = params.get("list_url")
        if not isinstance(list_url, str) or not list_url.strip():
            raise ValueError("scrape_list_members requires params.list_url")
    elif task_type == "scrape_following":
        account = params.get("account")
        if not isinstance(account, str) or not account.strip():
            raise ValueError("scrape_following requires params.account")
    elif task_type == "create_list":
        list_name = params.get("list_name")
        if not isinstance(list_name, str) or not list_name.strip():
            raise ValueError("create_list requires params.list_name")

    elif task_type == "vet_candidate":
        handles = params.get("handles", [])
        if not isinstance(handles, list) or not all(isinstance(h, str) and h.strip() for h in handles):
            raise ValueError("vet_candidate requires params.handles as a string list")

    return task


async def process_task(task_path: Path):
    """Process a single task file."""
    from runner import run_task

    task_id = task_path.stem
    active_path = ACTIVE / task_path.name

    # Move to active
    shutil.move(str(task_path), str(active_path))
    print(f"[{task_id}] Processing...", flush=True)

    try:
        task = validate_task(json.loads(active_path.read_text()))
        result = await run_task(task)
        result["task_id"] = task_id
        result["completed_at"] = datetime.now(timezone.utc).isoformat()

        (COMPLETED / task_path.name).write_text(json.dumps(result, indent=2))
        active_path.unlink()
        print(f"[{task_id}] Completed", flush=True)

    except Exception as e:
        error_result = {
            "task_id": task_id,
            "status": "failed",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        (FAILED / task_path.name).write_text(json.dumps(error_result, indent=2))
        if active_path.exists():
            active_path.unlink()
        print(f"[{task_id}] Failed: {e}", flush=True)


async def poll_loop():
    """Main polling loop."""
    for d in [PENDING, ACTIVE, COMPLETED, FAILED]:
        d.mkdir(parents=True, exist_ok=True)

    print(f"Agent polling {PENDING} for tasks...", flush=True)

    while True:
        tasks = sorted(PENDING.glob("*.json"))
        if tasks:
            await process_task(tasks[0])
        else:
            await asyncio.sleep(POLL_INTERVAL)


def run_once(task_path: str):
    """Process a single task file and exit."""
    for d in [PENDING, ACTIVE, COMPLETED, FAILED]:
        d.mkdir(parents=True, exist_ok=True)
    asyncio.run(process_task(Path(task_path)))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Single task mode: python main.py path/to/task.json
        run_once(sys.argv[1])
    else:
        # Polling mode
        asyncio.run(poll_loop())
