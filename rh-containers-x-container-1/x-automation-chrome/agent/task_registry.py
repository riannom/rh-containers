"""Centralized task registry — single source of truth for task types, scripts, and output files."""
from __future__ import annotations

# Each task maps to its properties. Adding a new task only requires updating this dict.
TASKS: dict[str, dict] = {
    "verify_session": {
        "type": "read-only",
        "script": "verify_session.py",
        "output": "verify_session.json",
    },
    "create_list": {
        "type": "read-only",
        "script": "create_list.py",
        "output": "create_list.json",
    },
    "collect_feeds": {
        "type": "read-only",
        "script": "collect_feeds.py",
        "output": "collect_feeds.json",
    },
    "collect_relationships": {
        "type": "read-only",
        "script": "collect_relationships.py",
        "output": "collect_relationships.json",
    },
    "pull_avatars": {
        "type": "read-only",
        "script": "pull_avatars.py",
        "output": "pull_avatars.json",
    },
    "vet_candidate": {
        "type": "read-only",
        "script": "vet_candidate.py",
        "output": "vet_candidate.json",
    },
    "scrape_list_members": {
        "type": "read-only",
        "script": "scrape_list_members.py",
        "output": "scrape_list_members.json",
    },
    "scrape_following": {
        "type": "read-only",
        "script": "scrape_following.py",
        "output": "scrape_following.json",
    },
    "debug_list_dialog": {
        "type": "read-only",
        "script": "debug_list_dialog.py",
        "output": "debug_list_dialog.json",
    },
    "follow_accounts": {
        "type": "mutating",
        "script": "follow_accounts.py",
        "output": "follow_accounts.json",
    },
    "manage_list": {
        "type": "mutating",
        "script": "manage_list.py",
        "output": "manage_list.json",
    },
}

# Derived sets for convenience
READ_ONLY_TASKS = frozenset(name for name, info in TASKS.items() if info["type"] == "read-only")
MUTATING_TASKS = frozenset(name for name, info in TASKS.items() if info["type"] == "mutating")
ALL_TASKS = frozenset(TASKS.keys())


def script_for(task: str) -> str:
    """Return the runner script filename for a task."""
    return TASKS[task]["script"]


def output_file_for(task: str) -> str:
    """Return the expected output filename for a task."""
    return TASKS[task]["output"]
