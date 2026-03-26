#!/usr/bin/env python3
"""WebSocket task server for x-automation containers.

Exposes the existing agent/runner task dispatch over WebSocket so the
researchhub scheduler can submit tasks, receive results, and monitor
container health from a remote machine.

Protocol (JSON messages over a single WS connection):

  Client -> Server:
    {"type": "task",   "task_id": "...", "task_type": "...", "params": {...}}
    {"type": "cancel", "task_id": "..."}
    {"type": "ping"}

  Server -> Client:
    {"type": "result", "task_id": "...", "status": "ok"|"failed", "output": {...}}
    {"type": "health", ...}
    {"type": "pong"}
    {"type": "error",  "message": "..."}

Runs alongside Chrome via supervisord inside the container.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

# ---------------------------------------------------------------------------
# Paths — resolve relative to the x-automation root (one level up from api/)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
AGENT_DIR = BASE_DIR / "agent"

# Add agent dir to path so we can import runner
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from main import validate_task, ALL_TASKS, TASKS_DIR, COMPLETED, FAILED  # noqa: E402
from runner import run_task  # noqa: E402

# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------
HEALTH_BEACON_INTERVAL = int(os.environ.get("WS_HEALTH_INTERVAL", "30"))
LISTEN_HOST = os.environ.get("WS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("WS_LISTEN_PORT", "18000"))
CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:9222")

# Track the currently running task so we can prevent double-dispatch
_active_task: dict | None = None
_active_task_future: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------
def _probe_health() -> dict:
    """Probe Chrome CDP and X session status."""
    health: dict = {
        "type": "health",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "online": False,
        "session": "unknown",
        "account": None,
        "active_task": _active_task.get("task_id") if _active_task else None,
    }
    try:
        raw = subprocess.check_output(
            ["curl", "-s", "--max-time", "3", f"{CDP_URL}/json/version"],
            text=True,
        )
        info = json.loads(raw)
        health["online"] = bool(info.get("webSocketDebuggerUrl"))
        health["browser_version"] = info.get("Browser", "")
    except Exception:
        pass
    return health


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------
async def _execute_task(task_id: str, task_type: str, params: dict) -> dict:
    """Run a task through the existing runner and return the result."""
    global _active_task
    task = {"type": task_type, "params": params}

    try:
        validated = validate_task(task)
        result = await run_task(validated)
        result["task_id"] = task_id
        result["completed_at"] = datetime.now(timezone.utc).isoformat()

        # Also write to completed dir for backward compat
        out_path = COMPLETED / f"{task_id}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2))

        return {"type": "result", "task_id": task_id, "status": result.get("status", "ok"), "output": result}

    except Exception as e:
        error_result = {
            "task_id": task_id,
            "status": "failed",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        out_path = FAILED / f"{task_id}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(error_result, indent=2))

        return {"type": "result", "task_id": task_id, "status": "failed", "output": error_result}

    finally:
        _active_task = None


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------
async def _health_beacon(ws: web.WebSocketResponse):
    """Send periodic health beacons to the connected client."""
    try:
        while not ws.closed:
            health = _probe_health()
            await ws.send_json(health)
            await asyncio.sleep(HEALTH_BEACON_INTERVAL)
    except (ConnectionResetError, asyncio.CancelledError):
        pass


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    global _active_task, _active_task_future

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    print(f"[ws] Client connected from {request.remote}", flush=True)

    # Start health beacon
    beacon = asyncio.create_task(_health_beacon(ws))

    # Send initial health
    await ws.send_json(_probe_health())

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})
                    continue

                msg_type = data.get("type")

                if msg_type == "ping":
                    await ws.send_json({"type": "pong"})

                elif msg_type == "task":
                    task_id = data.get("task_id")
                    task_type = data.get("task_type")
                    params = data.get("params", {})

                    if not task_id or not task_type:
                        await ws.send_json({"type": "error", "message": "task requires task_id and task_type"})
                        continue

                    if task_type not in ALL_TASKS:
                        await ws.send_json({
                            "type": "error",
                            "message": f"Unsupported task type: {task_type}",
                            "task_id": task_id,
                        })
                        continue

                    if _active_task is not None:
                        await ws.send_json({
                            "type": "error",
                            "message": f"Container busy with task {_active_task['task_id']}",
                            "task_id": task_id,
                        })
                        continue

                    _active_task = {"task_id": task_id, "task_type": task_type}
                    await ws.send_json({
                        "type": "ack",
                        "task_id": task_id,
                        "message": f"Task {task_id} accepted",
                    })

                    async def _run_and_send():
                        result = await _execute_task(task_id, task_type, params)
                        if not ws.closed:
                            await ws.send_json(result)

                    _active_task_future = asyncio.create_task(_run_and_send())

                elif msg_type == "cancel":
                    cancel_id = data.get("task_id")
                    if _active_task and _active_task.get("task_id") == cancel_id and _active_task_future:
                        _active_task_future.cancel()
                        _active_task = None
                        await ws.send_json({
                            "type": "result",
                            "task_id": cancel_id,
                            "status": "cancelled",
                            "output": {"status": "cancelled"},
                        })
                    else:
                        await ws.send_json({
                            "type": "error",
                            "message": f"No active task {cancel_id} to cancel",
                        })

                else:
                    await ws.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break

    finally:
        beacon.cancel()
        print(f"[ws] Client disconnected", flush=True)

    return ws


# ---------------------------------------------------------------------------
# HTTP health endpoint (for simple probes / monitoring)
# ---------------------------------------------------------------------------
async def health_handler(request: web.Request) -> web.Response:
    return web.json_response(_probe_health())


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health_handler)
    return app


def main():
    # Ensure task directories exist
    for d in [TASKS_DIR / "pending", TASKS_DIR / "active", TASKS_DIR / "completed", TASKS_DIR / "failed"]:
        d.mkdir(parents=True, exist_ok=True)

    print(f"[ws_server] Starting on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"[ws_server] CDP probe target: {CDP_URL}", flush=True)
    print(f"[ws_server] Health beacon interval: {HEALTH_BEACON_INTERVAL}s", flush=True)

    app = create_app()
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=lambda msg: print(f"[ws_server] {msg}", flush=True))


if __name__ == "__main__":
    main()
