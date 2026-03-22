"""Tests for error detection from log lines."""

from __future__ import annotations

from repofix.fixing.detector import detect_errors, is_fatal_exit


def _lines(texts: list[str], source: str = "stderr") -> list[tuple[str, str]]:
    return [(source, t) for t in texts]


def test_detects_missing_js_module() -> None:
    logs = _lines(["Error: Cannot find module 'express'"])
    signals = detect_errors(logs)
    assert any(s.error_type == "missing_dependency" for s in signals)


def test_detects_port_conflict() -> None:
    logs = _lines(["Error: listen EADDRINUSE: address already in use :::3000"])
    signals = detect_errors(logs)
    assert any(s.error_type == "port_conflict" for s in signals)


def test_detects_python_missing_module() -> None:
    logs = _lines(["ModuleNotFoundError: No module named 'uvicorn'"])
    signals = detect_errors(logs)
    assert any(s.error_type == "missing_dependency" for s in signals)


def test_detects_missing_env_var() -> None:
    logs = _lines(["KeyError: 'DATABASE_URL'"])
    signals = detect_errors(logs)
    assert any(s.error_type == "missing_env_var" for s in signals)


def test_detects_build_failure() -> None:
    logs = _lines(["Build failed with errors."])
    signals = detect_errors(logs)
    assert any(s.error_type == "build_failure" for s in signals)


def test_noise_filtered_out() -> None:
    logs = _lines(["DeprecationWarning: Using Buffer without 'new' is deprecated"])
    signals = detect_errors(logs)
    assert len(signals) == 0


def test_deduplication_of_same_error_type() -> None:
    logs = _lines([
        "Build failed with errors.",
        "Build failed again.",
    ])
    signals = detect_errors(logs)
    build_failures = [s for s in signals if s.error_type == "build_failure"]
    assert len(build_failures) == 1


def test_multiple_missing_deps_preserved() -> None:
    logs = _lines([
        "Error: Cannot find module 'express'",
        "Error: Cannot find module 'dotenv'",
    ])
    signals = detect_errors(logs)
    dep_signals = [s for s in signals if s.error_type == "missing_dependency"]
    assert len(dep_signals) == 2


def test_is_fatal_exit_true() -> None:
    assert is_fatal_exit(1, []) is True


def test_is_fatal_exit_false_for_sigint() -> None:
    assert is_fatal_exit(130, []) is False


def test_is_fatal_exit_false_for_zero() -> None:
    assert is_fatal_exit(0, []) is False


def test_detects_bin_sh_pnpm_not_found() -> None:
    """Debian dash: /bin/sh: 1: pnpm: not found"""
    logs = _lines(["/bin/sh: 1: pnpm: not found"])
    signals = detect_errors(logs)
    assert any(s.error_type == "missing_tool" for s in signals)


def test_detects_node_openssl_legacy() -> None:
    logs = _lines(["Error: error:0308010C:digital envelope routines::unsupported"])
    assert any(s.error_type == "node_openssl_legacy" for s in detect_errors(logs))


def test_detects_pip_resolution() -> None:
    logs = _lines(["ERROR: ResolutionImpossible — conflicting dependencies."])
    assert any(s.error_type == "pip_resolution" for s in detect_errors(logs))


def test_detects_git_remote_auth() -> None:
    logs = _lines(["remote: Repository not found."])
    assert any(s.error_type == "git_remote_auth" for s in detect_errors(logs))


def test_detects_poetry_lock_drift() -> None:
    logs = _lines(["pyproject.toml changed significantly since poetry.lock was last generated."])
    assert any(s.error_type == "lock_file_conflict" for s in detect_errors(logs))


def test_detects_corepack_required() -> None:
    logs = _lines(["Please run corepack enable before using pnpm."])
    assert any(s.error_type == "corepack_required" for s in detect_errors(logs))


def test_detects_package_manager_wrong() -> None:
    logs = _lines(["Usage Error: The project contains a yarn.lock file"])
    assert any(s.error_type == "package_manager_wrong" for s in detect_errors(logs))


def test_detects_engines_strict_ebadengine() -> None:
    logs = _lines(["npm error code EBADENGINE"])
    assert any(s.error_type == "engines_strict" for s in detect_errors(logs))


def test_detects_glibc_toolchain() -> None:
    logs = _lines(["version `GLIBC_2.34' not found (required by ...)"])
    assert any(s.error_type == "glibc_toolchain" for s in detect_errors(logs))


def test_ebadengine_not_treated_as_npm_warn_noise() -> None:
    """npm warn.* is usually noise, but EBADENGINE must still surface."""
    logs = _lines(["npm warn EBADENGINE Unsupported engine {"])
    assert any(s.error_type == "engines_strict" for s in detect_errors(logs))


def test_detects_gpu_cuda_runtime() -> None:
    logs = _lines(["RuntimeError: No CUDA GPUs are available"])
    assert any(s.error_type == "gpu_cuda_runtime" for s in detect_errors(logs))


def test_detects_git_lfs() -> None:
    logs = _lines(["git-lfs filter-process: stdin closed unexpectedly"])
    assert any(s.error_type == "git_lfs_error" for s in detect_errors(logs))


def test_detects_playwright_browsers() -> None:
    logs = _lines(["Error: Executable doesn't exist at /home/user/.cache/ms-playwright/"])
    assert any(s.error_type == "playwright_browsers" for s in detect_errors(logs))


def test_detects_prisma_p1001() -> None:
    logs = _lines(['Error: P1001: Can\'t reach database server at `localhost`:`5432`'])
    assert any(s.error_type == "database_error" for s in detect_errors(logs))


def test_detects_docker_failed_to_solve() -> None:
    logs = _lines(["ERROR: failed to solve: process \"/bin/sh -c npm ci\" did not complete successfully"])
    assert any(s.error_type == "docker_error" for s in detect_errors(logs))


def test_detects_pnpm_outdated_lockfile() -> None:
    logs = _lines(["ERR_PNPM_OUTDATED_LOCKFILE  Run pnpm install to update the lockfile"])
    assert any(s.error_type == "lock_file_conflict" for s in detect_errors(logs))


def test_detects_poetry_solver_problem() -> None:
    logs = _lines(["SolverProblemError: Because no versions of django match"])
    assert any(s.error_type == "pip_resolution" for s in detect_errors(logs))


def test_detects_esbuild_resolve_missing_dep() -> None:
    logs = _lines(['✘ [ERROR] Could not resolve "lodash"'])
    assert any(s.error_type == "missing_dependency" for s in detect_errors(logs))


def test_detects_esbuild_resolve_wrong_entry() -> None:
    logs = _lines(['Could not resolve "./missing-file"'])
    assert any(s.error_type == "wrong_entry_point" for s in detect_errors(logs))


def test_detects_python_shared_lib_import_error() -> None:
    logs = _lines(["ImportError: libssl.so.3: cannot open shared object file: No such file or directory"])
    assert any(s.error_type == "system_dependency" for s in detect_errors(logs))


def test_detects_deno_module_not_found() -> None:
    logs = _lines(["""error: Module not found "file:///tmp/foo.ts"."""])
    assert any(s.error_type == "missing_dependency" for s in detect_errors(logs))


def test_detects_vite_build_failure() -> None:
    logs = _lines(["vite v5.0.0 build failed in 120ms"])
    assert any(s.error_type == "build_failure" for s in detect_errors(logs))
