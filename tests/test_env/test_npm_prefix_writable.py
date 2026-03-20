"""Tests for npm default global prefix writability check."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from repofix.env import venv as venv_mgr


def test_machine_prefix_not_writable_for_usr_local(tmp_path: Path) -> None:
    fake = type("R", (), {"returncode": 0, "stdout": "/usr/local\n"})()
    with patch("subprocess.run", return_value=fake):
        with patch("repofix.env.venv.os.access", return_value=False):
            assert venv_mgr.machine_npm_global_prefix_writable(tmp_path) is False


def test_machine_prefix_writable_when_access_ok(tmp_path: Path) -> None:
    prefix = tmp_path / "npm-global"
    fake = type("R", (), {"returncode": 0, "stdout": str(prefix) + "\n"})()
    with patch("subprocess.run", return_value=fake):
        with patch("repofix.env.venv.os.access", return_value=True):
            assert venv_mgr.machine_npm_global_prefix_writable(tmp_path) is True
