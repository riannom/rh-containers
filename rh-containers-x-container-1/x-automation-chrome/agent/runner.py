"""
Deterministic task execution for x-automation.
Uses host-side Playwright over CDP to drive Google Chrome on the host.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import shutil
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
RUNNER_DIR = BASE_DIR / "runner"
OUT_DIR = BASE_DIR / "out"
STATE_DIR = BASE_DIR / "state"
SEEN_IDS_FILE = STATE_DIR / "seen_tweet_ids.json"
VENV_PYTHON = BASE_DIR / ".venv" / "bin" / "python"

TASK_TO_SCRIPT = {
    "verify_session": "verify_session.py",
    "create_list": "create_list.py",
    "collect_feeds": "collect_feeds.py",
    "collect_relationships": "collect_relationships.py",
    "pull_avatars": "pull_avatars.py",
    "follow_accounts": "follow_accounts.py",
    "manage_list": "manage_list.py",
    "vet_candidate": "vet_candidate.py",
    "scrape_list_members": "scrape_list_members.py",
    "scrape_list_counts": "scrape_list_counts.py",
    "scrape_following": "scrape_following.py",
}


def _resolve_node_bin() -> str:
    override = os.environ.get("NODE_BIN")
    if override:
        return override

    discovered = shutil.which("node")
    if discovered:
        return discovered

    for candidate in ("/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if Path(candidate).exists():
            return candidate

    raise RuntimeError("Could not locate node binary")


def _script_for(task_type: str) -> str:
    script = TASK_TO_SCRIPT.get(task_type)
    if not script:
        raise ValueError(f"Unsupported deterministic task type: {task_type}")
    return script


def _output_file_for(task_type: str) -> Path:
    if task_type == "verify_session":
        return OUT_DIR / "verify_session.json"
    if task_type == "create_list":
        return OUT_DIR / "create_list.json"
    if task_type == "collect_feeds":
        return OUT_DIR / "collect_feeds.json"
    if task_type == "collect_relationships":
        return OUT_DIR / "collect_relationships.json"
    if task_type == "pull_avatars":
        return OUT_DIR / "pull_avatars.json"
    if task_type == "follow_accounts":
        return OUT_DIR / "follow_accounts.json"
    if task_type == "manage_list":
        return OUT_DIR / "manage_list.json"
    if task_type == "vet_candidate":
        return OUT_DIR / "vet_candidate.json"
    if task_type == "scrape_list_members":
        return OUT_DIR / "scrape_list_members.json"
    if task_type == "scrape_list_counts":
        return OUT_DIR / "scrape_list_counts.json"
    if task_type == "scrape_following":
        return OUT_DIR / "scrape_following.json"
    raise ValueError(f"No output file mapping for task type: {task_type}")


def _build_env(task: dict) -> dict[str, str]:
    task_type = task["type"]
    params = task.get("params", {})
    env = os.environ.copy()
    node_bin = _resolve_node_bin()
    env["NODE_BIN"] = node_bin
    node_dir = str(Path(node_bin).parent)
    current_path = env.get("PATH", "")
    path_parts = [part for part in current_path.split(os.pathsep) if part]
    if node_dir not in path_parts:
        env["PATH"] = os.pathsep.join([node_dir, *path_parts]) if path_parts else node_dir
    env["CDP_URL"] = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
    env["BROWSER_URL"] = os.environ.get("BROWSER_URL", env["CDP_URL"])
    if env["BROWSER_URL"].startswith("http://") or env["BROWSER_URL"].startswith("https://"):
        try:
            payload = subprocess.check_output(
                ["curl", "-s", env["BROWSER_URL"].rstrip("/") + "/json/version"],
                text=True,
            )
            ws_endpoint = json.loads(payload).get("webSocketDebuggerUrl")
            if ws_endpoint:
                env["BROWSER_WS_ENDPOINT"] = ws_endpoint
        except Exception:
            pass
    env["X_AUTOMATION_OUT_DIR"] = str(OUT_DIR)

    if task_type == "collect_feeds":
        env["X_LIST_URLS_JSON"] = json.dumps(params.get("list_urls", []))
        env["X_LIST_LABELS_JSON"] = json.dumps(params.get("labels", []))
        env["X_COLLECT_FEED"] = "1" if params.get("collect_feed") else "0"
        env["X_MAX_SCROLLS"] = str(params.get("max_scrolls", 8))
        if "max_scrolls_min" in params:
            env["X_MAX_SCROLLS_MIN"] = str(params["max_scrolls_min"])
        if "max_scrolls_max" in params:
            env["X_MAX_SCROLLS_MAX"] = str(params["max_scrolls_max"])
        env["X_SEEN_IDS_FILE"] = os.environ.get("X_SEEN_IDS_FILE_OVERRIDE", str(SEEN_IDS_FILE))
    elif task_type == "collect_relationships":
        env["X_REL_HANDLES_JSON"] = json.dumps(params.get("handles", []))
        env["X_REL_DIRECTION"] = str(params.get("direction", "both"))
        env["X_REL_EDGE_LIMIT"] = str(params.get("edge_limit", 100))
        env["X_REL_EDGE_SCROLLS"] = str(params.get("edge_scrolls", 5))
        if "profile_scrolls_min" in params:
            env["X_REL_PROFILE_SCROLLS_MIN"] = str(params["profile_scrolls_min"])
        if "profile_scrolls_max" in params:
            env["X_REL_PROFILE_SCROLLS_MAX"] = str(params["profile_scrolls_max"])
        if "edge_scrolls_min" in params:
            env["X_REL_EDGE_SCROLLS_MIN"] = str(params["edge_scrolls_min"])
        if "edge_scrolls_max" in params:
            env["X_REL_EDGE_SCROLLS_MAX"] = str(params["edge_scrolls_max"])
    elif task_type == "pull_avatars":
        env["X_AVATAR_HANDLES_JSON"] = json.dumps(params.get("handles", []))
    elif task_type == "follow_accounts":
        env["X_FOLLOW_HANDLES_JSON"] = json.dumps(params.get("handles", []))
        if "action" in params:
            env["X_FOLLOW_ACTION"] = str(params["action"])
    elif task_type == "create_list":
        env["X_LIST_NAME"] = str(params.get("list_name", ""))
        env["X_LIST_DESC"] = str(params.get("list_description", ""))
        env["X_LIST_PRIVATE"] = "1" if params.get("private", True) else "0"
    elif task_type == "manage_list":
        env["X_LIST_URL"] = str(params.get("list_url", ""))
        env["X_LIST_ADD_JSON"] = json.dumps(params.get("add", []))
        env["X_LIST_REMOVE_JSON"] = json.dumps(params.get("remove", []))
        if "list_name" in params:
            env["X_LIST_NAME_OVERRIDE"] = str(params["list_name"])
        if "per_handle_timeout" in params:
            env["X_MANAGE_LIST_PER_HANDLE_TIMEOUT"] = str(params["per_handle_timeout"])
        if "session_timeout" in params:
            env["X_MANAGE_LIST_SESSION_TIMEOUT"] = str(params["session_timeout"])
        if params.get("force_add"):
            env["X_MANAGE_LIST_FORCE_ADD"] = "1"
    elif task_type == "vet_candidate":
        env["X_VET_HANDLES_JSON"] = json.dumps(params.get("handles", []))
        env["X_VET_MAX_SCROLLS"] = str(params.get("max_scrolls", 6))
        if "max_scrolls_min" in params:
            env["X_VET_MAX_SCROLLS_MIN"] = str(params["max_scrolls_min"])
        if "max_scrolls_max" in params:
            env["X_VET_MAX_SCROLLS_MAX"] = str(params["max_scrolls_max"])
    elif task_type == "scrape_list_members":
        env["X_LIST_URL"] = str(params.get("list_url", ""))
        if params.get("skip_members"):
            env["X_SCRAPE_SKIP_MEMBERS"] = "1"
        if params.get("desired_handles"):
            env["X_DESIRED_HANDLES_JSON"] = json.dumps(params["desired_handles"])
        if "max_scrolls" in params:
            env["X_SCRAPE_MAX_SCROLLS"] = str(params["max_scrolls"])
        if "session_timeout" in params:
            env["X_SCRAPE_SESSION_TIMEOUT"] = str(params["session_timeout"])
    elif task_type == "scrape_list_counts":
        env["X_ACCOUNT_HANDLE"] = str(params.get("account_handle", ""))
        if params.get("list_urls"):
            env["X_LIST_URLS_JSON"] = json.dumps(params["list_urls"])
        if "session_timeout" in params:
            env["X_SCRAPE_SESSION_TIMEOUT"] = str(params["session_timeout"])
    elif task_type == "scrape_following":
        env["X_FOLLOWING_ACCOUNT"] = str(params.get("account", ""))
        if "max_scrolls" in params:
            env["X_SCRAPE_MAX_SCROLLS"] = str(params["max_scrolls"])
        if "session_timeout" in params:
            env["X_SCRAPE_SESSION_TIMEOUT"] = str(params["session_timeout"])

    return env


async def run_task(task: dict) -> dict:
    """Execute a supported task by attaching Playwright to host Google Chrome over CDP."""
    task_type = task["type"]
    script = _script_for(task_type)
    output_file = _output_file_for(task_type)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if output_file.exists():
        output_file.unlink()

    if script.endswith(".py"):
        command = str(VENV_PYTHON if VENV_PYTHON.exists() else "python3")
    else:
        command = _resolve_node_bin()

    proc = await asyncio.create_subprocess_exec(
        command,
        str(RUNNER_DIR / script),
        cwd=str(RUNNER_DIR),
        env=_build_env(task),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"{script} exited with code {proc.returncode}: {(stderr or stdout).decode().strip()}"
        )

    if not output_file.exists():
        raise RuntimeError(
            f"{script} completed without producing expected output file {output_file}"
        )

    result = json.loads(output_file.read_text())
    result.setdefault("task_type", task_type)
    if stdout:
        result.setdefault("runner_stdout", stdout.decode().strip())
    if stderr:
        result.setdefault("runner_stderr", stderr.decode().strip())
    return result
