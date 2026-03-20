"""Process registry — track running and recently-stopped apps."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from repofix import config as cfg

_lock = threading.Lock()


@dataclass
class ProcessEntry:
    name: str
    pid: int
    repo_url: str
    repo_path: str
    run_command: str
    log_file: str
    started_at: float
    status: str                       # "running" | "stopped" | "crashed"
    app_url: str | None = None
    stack: str = "unknown"
    port: int | None = None
    env: dict[str, str] = field(default_factory=dict)

    def uptime_s(self) -> float | None:
        if self.status != "running":
            return None
        return time.time() - self.started_at

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_raw() -> dict[str, Any]:
    cfg.ensure_dirs_only()
    if not cfg.PROCESS_REGISTRY.exists():
        return {}
    try:
        return json.loads(cfg.PROCESS_REGISTRY.read_text())
    except Exception:
        return {}


def _save_raw(data: dict[str, Any]) -> None:
    cfg.ensure_dirs_only()
    cfg.PROCESS_REGISTRY.write_text(json.dumps(data, indent=2))


def _entry_from_dict(d: dict[str, Any]) -> ProcessEntry:
    return ProcessEntry(
        name=d["name"],
        pid=d["pid"],
        repo_url=d.get("repo_url", ""),
        repo_path=d.get("repo_path", ""),
        run_command=d.get("run_command", ""),
        log_file=d.get("log_file", ""),
        started_at=d.get("started_at", 0.0),
        status=d.get("status", "stopped"),
        app_url=d.get("app_url"),
        stack=d.get("stack", "unknown"),
        port=d.get("port"),
        env=d.get("env", {}),
    )


# ── Public API ────────────────────────────────────────────────────────────────

def _unique_name(base: str, existing: set[str]) -> str:
    """Return base if not taken, else base-2, base-3, …"""
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def register(entry: ProcessEntry) -> str:
    """
    Add an entry to the registry.  If an entry with the same name already
    exists and is still running, a numeric suffix is appended.
    Returns the final name used.
    """
    with _lock:
        data = _load_raw()
        existing_running = {
            k for k, v in data.items()
            if v.get("status") == "running" and is_alive(v.get("pid", 0))
        }
        name = _unique_name(entry.name, existing_running)
        entry.name = name
        data[name] = entry.as_dict()
        _save_raw(data)
    return name


def set_status(name: str, status: str) -> None:
    with _lock:
        data = _load_raw()
        if name in data:
            data[name]["status"] = status
            _save_raw(data)


def get_all() -> list[ProcessEntry]:
    data = _load_raw()
    return [_entry_from_dict(v) for v in data.values()]


def get_by_name(name: str) -> ProcessEntry | None:
    data = _load_raw()
    raw = data.get(name)
    return _entry_from_dict(raw) if raw else None


def remove(name: str) -> None:
    with _lock:
        data = _load_raw()
        data.pop(name, None)
        _save_raw(data)


def reconcile() -> list[ProcessEntry]:
    """
    Check every 'running' entry against the real process table.
    Entries whose PID is dead are flipped to 'crashed'.
    Returns the reconciled list.
    """
    with _lock:
        data = _load_raw()
        changed = False
        for entry_dict in data.values():
            if entry_dict.get("status") == "running":
                if not is_alive(entry_dict.get("pid", 0)):
                    entry_dict["status"] = "crashed"
                    changed = True
        if changed:
            _save_raw(data)
    return [_entry_from_dict(v) for v in data.values()]


def is_alive(pid: int) -> bool:
    """Return True if a process with this PID is currently running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)   # signal 0 = existence check, no actual signal
        return True
    except (ProcessLookupError, PermissionError):
        return False


def log_path_for(name: str) -> Path | None:
    """Return the most-recent log file for a given process name."""
    logs_dir = cfg.LOGS_DIR
    if not logs_dir.exists():
        return None
    matches = sorted(logs_dir.glob(f"{name}-*.log"), reverse=True)
    return matches[0] if matches else None


def make_log_path(name: str) -> Path:
    """Create a fresh timestamped log file path for a new run."""
    cfg.ensure_dirs_only()
    ts = int(time.time())
    return cfg.LOGS_DIR / f"{name}-{ts}.log"
