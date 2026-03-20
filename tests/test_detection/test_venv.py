"""Tests for isolated environment setup."""

from __future__ import annotations

import os
from pathlib import Path

from repofix.detection.stack import StackInfo
from repofix.env import venv as venv_mgr


def _stack(runtime: str) -> StackInfo:
    return StackInfo(language="Test", framework="Test", project_type="backend", runtime=runtime)


def test_needs_venv_for_python() -> None:
    assert venv_mgr.needs_venv(_stack("python")) is True
    assert venv_mgr.needs_venv(_stack("pip")) is True


def test_needs_venv_false_for_node() -> None:
    assert venv_mgr.needs_venv(_stack("node")) is False


def test_needs_venv_false_for_go() -> None:
    assert venv_mgr.needs_venv(_stack("go")) is False


def test_needs_venv_false_for_docker() -> None:
    assert venv_mgr.needs_venv(_stack("docker")) is False


def test_python_venv_created(tmp_path: Path) -> None:
    env = venv_mgr.setup(tmp_path, _stack("python"))
    assert venv_mgr.venv_exists(tmp_path), "venv should have been created"
    assert "VIRTUAL_ENV" in env
    assert "PATH" in env
    assert str(venv_mgr.venv_bin(tmp_path)) in env["PATH"]


def test_python_venv_reused(tmp_path: Path) -> None:
    """Second call should reuse the existing venv without error."""
    venv_mgr.setup(tmp_path, _stack("python"))
    mtime_before = venv_mgr.venv_python(tmp_path).stat().st_mtime
    venv_mgr.setup(tmp_path, _stack("python"))
    mtime_after = venv_mgr.venv_python(tmp_path).stat().st_mtime
    assert mtime_before == mtime_after, "python binary should not be recreated"


def test_venv_path_prepended_to_path(tmp_path: Path) -> None:
    env = venv_mgr.setup(tmp_path, _stack("python"))
    path_entries = env["PATH"].split(os.pathsep)
    assert path_entries[0] == str(venv_mgr.venv_bin(tmp_path))


def test_pip_no_user_install_set(tmp_path: Path) -> None:
    env = venv_mgr.setup(tmp_path, _stack("python"))
    assert env.get("PIP_NO_USER_INSTALL") == "1"


def test_node_env_sets_local_bin(tmp_path: Path) -> None:
    env = venv_mgr.setup(tmp_path, _stack("node"))
    assert "PATH" in env
    assert "node_modules" in env["PATH"]


def test_ruby_env_sets_bundle_path(tmp_path: Path) -> None:
    env = venv_mgr.setup(tmp_path, _stack("ruby"))
    assert "BUNDLE_PATH" in env
    assert "vendor" in env["BUNDLE_PATH"]


def test_go_env_returns_empty(tmp_path: Path) -> None:
    env = venv_mgr.setup(tmp_path, _stack("go"))
    assert env == {}


def test_docker_env_returns_empty(tmp_path: Path) -> None:
    env = venv_mgr.setup(tmp_path, _stack("docker"))
    assert env == {}
