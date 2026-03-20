"""Tests for rule-based fix strategies."""

from __future__ import annotations

from pathlib import Path

import pytest

from repofix.detection.stack import StackInfo
from repofix.fixing.classifier import ClassifiedError
from repofix.fixing.detector import ErrorSignal
from repofix.fixing.rules import FixAction, apply_rule


def _node_stack() -> StackInfo:
    return StackInfo(language="Node.js", framework="Express", project_type="backend", runtime="node")


def _python_stack() -> StackInfo:
    return StackInfo(language="Python", framework="FastAPI", project_type="backend", runtime="python")


def _make_classified(error_type: str, extracted: dict, line: str = "") -> ClassifiedError:
    signal = ErrorSignal(raw_line=line or error_type, source="stderr", error_type=error_type)
    return ClassifiedError(error_type=error_type, description=line, signal=signal, extracted=extracted)


def test_fix_missing_npm_dependency(tmp_path: Path) -> None:
    error = _make_classified("missing_dependency", {"package": "express", "runtime": "node"})
    action = apply_rule(error, _node_stack(), tmp_path)
    assert action is not None
    assert any("npm install express" in cmd for cmd in action.commands)


def test_fix_missing_npm_dependency_with_yarn(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").write_text("")
    error = _make_classified("missing_dependency", {"package": "lodash", "runtime": "node"})
    action = apply_rule(error, _node_stack(), tmp_path)
    assert action is not None
    assert any("yarn add lodash" in cmd for cmd in action.commands)


def test_fix_missing_python_dependency(tmp_path: Path) -> None:
    error = _make_classified("missing_dependency", {"package": "httpx", "runtime": "python"})
    action = apply_rule(error, _python_stack(), tmp_path)
    assert action is not None
    assert any("pip install httpx" in cmd for cmd in action.commands)


def test_fix_python_import_alias(tmp_path: Path) -> None:
    """cv2 maps to opencv-python on PyPI."""
    error = _make_classified("missing_dependency", {"package": "cv2", "runtime": "python"})
    action = apply_rule(error, _python_stack(), tmp_path)
    assert action is not None
    assert any("opencv-python" in cmd for cmd in action.commands)


def test_fix_port_conflict(tmp_path: Path) -> None:
    error = _make_classified("port_conflict", {"port": 3000})
    action = apply_rule(error, _node_stack(), tmp_path)
    assert action is not None
    assert action.port_override == 3000
    assert action.next_step == "rerun"


def test_fix_missing_env_var(tmp_path: Path) -> None:
    error = _make_classified("missing_env_var", {"var_name": "DATABASE_URL"})
    action = apply_rule(error, _python_stack(), tmp_path)
    assert action is not None
    assert "DATABASE_URL" in action.env_updates


def test_no_rule_for_unknown_error(tmp_path: Path) -> None:
    error = _make_classified("generic_error", {})
    action = apply_rule(error, _python_stack(), tmp_path)
    assert action is None


def test_fix_missing_dependency_returns_rerun(tmp_path: Path) -> None:
    error = _make_classified("missing_dependency", {"package": "chalk", "runtime": "node"})
    action = apply_rule(error, _node_stack(), tmp_path)
    assert action is not None
    assert action.next_step == "rerun"


def test_fix_node_openssl_legacy_sets_node_options(tmp_path: Path) -> None:
    error = _make_classified("node_openssl_legacy", {}, "ERR_OSSL_EVP_UNSUPPORTED")
    action = apply_rule(error, _node_stack(), tmp_path)
    assert action is not None
    assert action.env_updates.get("NODE_OPTIONS") == "--openssl-legacy-provider"
    assert action.next_step == "rerun"


def test_fix_lock_file_poetry(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n")
    err = ClassifiedError(
        error_type="lock_file_conflict",
        description="drift",
        signal=ErrorSignal(
            raw_line="pyproject.toml changed significantly since poetry.lock was last generated.",
            source="stderr",
            error_type="lock_file_conflict",
        ),
        extracted={},
    )
    action = apply_rule(err, _python_stack(), tmp_path)
    assert action is not None
    assert any("poetry lock" in cmd for cmd in action.commands)


def test_fix_lock_file_uv_when_uv_lock_present(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "uv.lock").write_text("# lock\n")
    err = ClassifiedError(
        error_type="lock_file_conflict",
        description="sync",
        signal=ErrorSignal(raw_line="Integrity check failed", source="stderr", error_type="lock_file_conflict"),
        extracted={},
    )
    action = apply_rule(err, _python_stack(), tmp_path)
    assert action is not None
    assert any(cmd.strip() == "uv lock" for cmd in action.commands)


def test_fix_pip_resolution(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("a==1\n")
    error = _make_classified("pip_resolution", {}, "ResolutionImpossible")
    action = apply_rule(error, _python_stack(), tmp_path)
    assert action is not None
    assert any("pip install --upgrade pip" in cmd for cmd in action.commands)


def test_fix_corepack_enable(tmp_path: Path) -> None:
    error = _make_classified("corepack_required", {}, "enable corepack")
    action = apply_rule(error, _node_stack(), tmp_path)
    assert action is not None
    assert action.commands == ["corepack enable"]


def test_fix_package_manager_yarn_lock(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").write_text("")
    error = _make_classified("package_manager_wrong", {}, "yarn.lock")
    action = apply_rule(error, _node_stack(), tmp_path)
    assert action is not None
    assert any("yarn install" in cmd for cmd in action.commands)


def test_fix_engines_strict(tmp_path: Path) -> None:
    error = _make_classified(
        "engines_strict",
        {"required": "20", "runtime": "node"},
        "EBADENGINE",
    )
    action = apply_rule(error, _node_stack(), tmp_path)
    assert action is not None
    assert action.env_updates.get("YARN_IGNORE_ENGINES") == "1"
    assert any("engine-strict false" in cmd for cmd in action.commands)


def test_fix_glibc_toolchain(tmp_path: Path) -> None:
    error = _make_classified("glibc_toolchain", {"glibc_need": "2.34"}, "GLIBC_2.34")
    action = apply_rule(error, _python_stack(), tmp_path)
    assert action is not None
    assert any("ldd" in cmd or "manylinux" in cmd for cmd in action.commands)


def test_fix_gpu_cuda_cpu_torch(tmp_path: Path) -> None:
    error = _make_classified("gpu_cuda_runtime", {"runtime": "python"}, "torch.cuda.is_available()")
    action = apply_rule(error, _python_stack(), tmp_path)
    assert action is not None
    assert any("pytorch.org" in cmd for cmd in action.commands)


def test_fix_git_lfs_pull(tmp_path: Path) -> None:
    error = _make_classified("git_lfs_error", {}, "git-lfs")
    action = apply_rule(error, _python_stack(), tmp_path)
    assert action is not None
    assert any("lfs pull" in cmd for cmd in action.commands)


def test_fix_playwright_install(tmp_path: Path) -> None:
    error = _make_classified("playwright_browsers", {"runtime": "node"}, "playwright")
    action = apply_rule(error, _node_stack(), tmp_path)
    assert action is not None
    assert any("playwright install" in cmd for cmd in action.commands)


def test_fix_missing_pnpm_cli(tmp_path: Path) -> None:
    error = _make_classified("missing_tool", {"tool_name": "pnpm"}, "/bin/sh: 1: pnpm: not found")
    action = apply_rule(error, _node_stack(), tmp_path)
    assert action is not None
    assert any("pnpm" in cmd and "corepack" in cmd for cmd in action.commands)
