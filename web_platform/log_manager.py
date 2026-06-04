"""Per-profile log buffer + WebSocket broadcast relay."""

import asyncio
import os
import queue
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

# Thread-safe queue: booking threads push here, async relay drains it
_log_queue: queue.Queue = queue.Queue()

# Per-profile log state
_profile_logs: dict[int, "ProfileLog"] = {}

# Dashboard WebSocket clients
_dashboard_clients: set[WebSocket] = set()


@dataclass
class ProfileLog:
    buffer: deque = field(default_factory=lambda: deque(maxlen=500))
    ws_clients: set = field(default_factory=set)


def get_profile_log(profile_id: int) -> ProfileLog:
    if profile_id not in _profile_logs:
        _profile_logs[profile_id] = ProfileLog()
    return _profile_logs[profile_id]


def remove_profile_log(profile_id: int):
    _profile_logs.pop(profile_id, None)


_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

# Per-profile active log session: {profile_id: "2026-03-16_08-39-55"}
_active_sessions: dict[int, str] = {}


def start_log_session(profile_id: int):
    """Start a new log session file. Call before each booking run."""
    _active_sessions[profile_id] = time.strftime("%Y-%m-%d_%H-%M-%S")


def emit(profile_id: int, msg: str, status: str = None):
    """Called from any thread (including booking threads). Thread-safe.
    Also persists to log file for post-analysis."""
    ts = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
    entry = {"ts": ts, "msg": msg}
    if status:
        entry["status"] = status
    _log_queue.put((profile_id, entry))

    # Persist to file: logs/profile_{id}_{session}.log
    # Auto-start session if not started
    if profile_id not in _active_sessions:
        start_log_session(profile_id)
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        session = _active_sessions[profile_id]
        log_file = os.path.join(_LOG_DIR, f"profile_{profile_id}_{session}.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def add_dashboard_client(ws: WebSocket):
    _dashboard_clients.add(ws)


def remove_dashboard_client(ws: WebSocket):
    _dashboard_clients.discard(ws)


async def relay_loop():
    """Async coroutine: drain log queue and broadcast to WebSocket clients.
    Run this as a background task in FastAPI lifespan."""
    global _dashboard_clients
    while True:
        try:
            while not _log_queue.empty():
                try:
                    profile_id, entry = _log_queue.get_nowait()
                except queue.Empty:
                    break

                plog = get_profile_log(profile_id)
                plog.buffer.append(entry)

                # Broadcast to profile log subscribers (copy set to avoid mutation during iteration)
                dead = set()
                for ws in list(plog.ws_clients):
                    try:
                        await ws.send_json(entry)
                    except Exception:
                        dead.add(ws)
                plog.ws_clients -= dead

                # Broadcast to dashboard subscribers (copy set)
                dashboard_entry = {"profile_id": profile_id, **entry}
                dead_dash = set()
                for ws in list(_dashboard_clients):
                    try:
                        await ws.send_json(dashboard_entry)
                    except Exception:
                        dead_dash.add(ws)
                _dashboard_clients -= dead_dash

            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return  # Clean shutdown
        except Exception as e:
            print(f"[log_manager] relay_loop error (continuing): {e}")
            await asyncio.sleep(0.1)  # Brief pause then continue
