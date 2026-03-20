"""SQLite-backed fix memory store — learn from past successes."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from repofix import config as cfg
from repofix.fixing.classifier import ClassifiedError
from repofix.fixing.rules import FixAction


def _db_path() -> Path:
    cfg.ensure_dirs_only()
    return cfg.MEMORY_DB


@contextmanager
def _connect() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    """Create tables if they don't exist."""
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fixes (
                error_fingerprint  TEXT PRIMARY KEY,
                error_type         TEXT NOT NULL,
                fix_json           TEXT NOT NULL,
                success_count      INTEGER DEFAULT 0,
                failure_count      INTEGER DEFAULT 0,
                last_applied       REAL,
                stack              TEXT
            );

            CREATE TABLE IF NOT EXISTS runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_url     TEXT,
                stack        TEXT,
                timestamp    REAL NOT NULL,
                success      INTEGER NOT NULL,
                duration_s   REAL,
                fix_count    INTEGER DEFAULT 0,
                notes        TEXT
            );

            CREATE TABLE IF NOT EXISTS branch_states (
                repo_key         TEXT NOT NULL,
                branch           TEXT NOT NULL,
                dep_hash         TEXT NOT NULL,
                env_dir          TEXT NOT NULL,
                stack_json       TEXT NOT NULL DEFAULT '{}',
                commands_json    TEXT NOT NULL DEFAULT '{}',
                installed_at     REAL NOT NULL,
                install_success  INTEGER NOT NULL DEFAULT 1,
                dep_files        TEXT NOT NULL DEFAULT '[]',
                build_success    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (repo_key, branch)
            );
        """)
        # Migrate existing databases that predate the build_success column.
        try:
            conn.execute(
                "ALTER TABLE branch_states ADD COLUMN build_success INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already exists


def lookup_fix(error: ClassifiedError) -> FixAction | None:
    """Return a cached FixAction if one exists with a positive success rate."""
    init()
    fingerprint = error.fingerprint()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM fixes WHERE error_fingerprint = ?", (fingerprint,)
        ).fetchone()
    if not row:
        return None

    success = row["success_count"]
    total = success + row["failure_count"]
    if total == 0:
        return None

    success_rate = success / total
    if success_rate < 0.4:
        return None

    try:
        data = json.loads(row["fix_json"])
        action = FixAction(**data)
        action.source = "memory"
        return action
    except Exception:
        return None


def record_fix(
    error: ClassifiedError,
    action: FixAction,
    success: bool,
    stack: str = "",
) -> None:
    """Record whether a fix attempt succeeded or failed."""
    init()
    fingerprint = error.fingerprint()
    fix_json = json.dumps({
        "description": action.description,
        "commands": action.commands,
        "env_updates": action.env_updates,
        "port_override": action.port_override,
        "next_step": action.next_step,
        "source": action.source,
    })
    now = time.time()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM fixes WHERE error_fingerprint = ?", (fingerprint,)
        ).fetchone()
        if existing:
            if success:
                conn.execute(
                    "UPDATE fixes SET success_count = success_count + 1, last_applied = ?, fix_json = ? WHERE error_fingerprint = ?",
                    (now, fix_json, fingerprint),
                )
            else:
                conn.execute(
                    "UPDATE fixes SET failure_count = failure_count + 1, last_applied = ? WHERE error_fingerprint = ?",
                    (now, fingerprint),
                )
        else:
            conn.execute(
                """INSERT INTO fixes (error_fingerprint, error_type, fix_json, success_count, failure_count, last_applied, stack)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (fingerprint, error.error_type, fix_json, 1 if success else 0, 0 if success else 1, now, stack),
            )


def record_run(
    repo_url: str,
    stack: str,
    success: bool,
    duration_s: float,
    fix_count: int = 0,
    notes: str = "",
) -> None:
    init()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO runs (repo_url, stack, timestamp, success, duration_s, fix_count, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (repo_url, stack, time.time(), 1 if success else 0, duration_s, fix_count, notes),
        )


def get_recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        ts = datetime.fromtimestamp(d["timestamp"], tz=timezone.utc)
        d["when"] = ts.strftime("%Y-%m-%d %H:%M")
        d["success"] = bool(d["success"])
        result.append(d)
    return result


def clear_all() -> None:
    init()
    with _connect() as conn:
        conn.execute("DELETE FROM fixes")
        conn.execute("DELETE FROM runs")
        conn.execute("DELETE FROM branch_states")


# ── Branch state cache ────────────────────────────────────────────────────────

def save_branch_state(
    repo_key: str,
    branch: str,
    dep_hash: str,
    env_dir: str,
    stack_json: str = "{}",
    commands_json: str = "{}",
    dep_files: str = "[]",
    install_success: bool = True,
    build_success: bool = False,
) -> None:
    """Insert or replace the cached state for a (repo, branch) pair."""
    init()
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO branch_states
                (repo_key, branch, dep_hash, env_dir, stack_json, commands_json,
                 installed_at, install_success, dep_files, build_success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_key, branch) DO UPDATE SET
                dep_hash        = excluded.dep_hash,
                env_dir         = excluded.env_dir,
                stack_json      = excluded.stack_json,
                commands_json   = excluded.commands_json,
                installed_at    = excluded.installed_at,
                install_success = excluded.install_success,
                dep_files       = excluded.dep_files,
                build_success   = excluded.build_success
            """,
            (
                repo_key, branch, dep_hash, env_dir,
                stack_json, commands_json, now,
                1 if install_success else 0, dep_files,
                1 if build_success else 0,
            ),
        )


def get_branch_state(repo_key: str, branch: str) -> dict[str, Any] | None:
    """Return the cached state dict for (repo_key, branch), or None."""
    init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM branch_states WHERE repo_key = ? AND branch = ?",
            (repo_key, branch),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["install_success"] = bool(d["install_success"])
    d["build_success"] = bool(d.get("build_success", 0))
    ts = datetime.fromtimestamp(d["installed_at"], tz=timezone.utc)
    d["installed_when"] = ts.strftime("%Y-%m-%d %H:%M")
    return d


def list_branch_states(repo_key: str | None = None) -> list[dict[str, Any]]:
    """List all cached branch states, optionally filtered by repo."""
    init()
    with _connect() as conn:
        if repo_key:
            rows = conn.execute(
                "SELECT * FROM branch_states WHERE repo_key = ? ORDER BY installed_at DESC",
                (repo_key,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM branch_states ORDER BY installed_at DESC"
            ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["install_success"] = bool(d["install_success"])
        ts = datetime.fromtimestamp(d["installed_at"], tz=timezone.utc)
        d["installed_when"] = ts.strftime("%Y-%m-%d %H:%M")
        result.append(d)
    return result


def delete_branch_state(repo_key: str, branch: str) -> bool:
    """Delete a single branch cache entry. Returns True if a row was deleted."""
    init()
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM branch_states WHERE repo_key = ? AND branch = ?",
            (repo_key, branch),
        )
    return cursor.rowcount > 0


def clear_branch_states(repo_key: str | None = None) -> int:
    """Delete branch cache entries. Returns number of rows deleted."""
    init()
    with _connect() as conn:
        if repo_key:
            cursor = conn.execute(
                "DELETE FROM branch_states WHERE repo_key = ?", (repo_key,)
            )
        else:
            cursor = conn.execute("DELETE FROM branch_states")
    return cursor.rowcount
