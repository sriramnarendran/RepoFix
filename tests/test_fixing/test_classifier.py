"""Tests for error classifier."""

from __future__ import annotations

from repofix.fixing.classifier import classify
from repofix.fixing.detector import ErrorSignal


def _make_signal(line: str, error_type: str = "missing_dependency") -> ErrorSignal:
    return ErrorSignal(raw_line=line, source="stderr", error_type=error_type, context_lines=[line])


def test_classify_js_missing_module() -> None:
    signal = _make_signal("Error: Cannot find module 'express'")
    err = classify(signal, runtime="node")
    assert err.error_type == "missing_dependency"
    assert err.extracted["package"] == "express"


def test_classify_python_missing_module() -> None:
    signal = _make_signal("ModuleNotFoundError: No module named 'fastapi'", "missing_dependency")
    err = classify(signal, runtime="python")
    assert err.error_type == "missing_dependency"
    assert err.extracted["package"] == "fastapi"


def test_classify_port_conflict() -> None:
    signal = _make_signal("Error: listen EADDRINUSE: address already in use :::3000", "port_conflict")
    err = classify(signal, runtime="node")
    assert err.error_type == "port_conflict"
    assert err.extracted["port"] == 3000


def test_classify_missing_env_var_keyerror() -> None:
    signal = _make_signal("KeyError: 'DATABASE_URL'", "missing_env_var")
    err = classify(signal, runtime="python")
    assert err.error_type == "missing_env_var"
    assert err.extracted["var_name"] == "DATABASE_URL"


def test_classify_missing_env_var_process_env() -> None:
    signal = _make_signal("process.env.API_KEY is undefined", "missing_env_var")
    err = classify(signal, runtime="node")
    assert err.error_type == "missing_env_var"
    assert err.extracted["var_name"] == "API_KEY"


def test_classify_build_failure() -> None:
    signal = _make_signal("Build failed with errors.", "build_failure")
    err = classify(signal, runtime="node")
    assert err.error_type == "build_failure"


def test_fingerprint_missing_dep() -> None:
    signal = _make_signal("Cannot find module 'axios'")
    err = classify(signal, runtime="node")
    fp = err.fingerprint()
    assert "missing_dependency" in fp
    assert "axios" in fp


def test_fingerprint_port_conflict() -> None:
    signal = _make_signal("EADDRINUSE :::8080", "port_conflict")
    err = classify(signal, runtime="node")
    fp = err.fingerprint()
    assert "port_conflict" in fp


def test_classify_scoped_npm_package() -> None:
    signal = _make_signal("Cannot find module '@nestjs/core'")
    err = classify(signal, runtime="node")
    assert err.extracted["package"] == "@nestjs/core"


def test_relative_import_not_classified_as_missing_dep() -> None:
    signal = _make_signal("Error: Cannot find module './utils/helpers'")
    err = classify(signal, runtime="node")
    # Relative paths should not produce an installable package name
    assert err.extracted.get("package") is None


def test_classify_node_openssl_legacy() -> None:
    signal = _make_signal("opensslErrorStack: ERR_OSSL_EVP_UNSUPPORTED", "node_openssl_legacy")
    err = classify(signal, runtime="node")
    assert err.error_type == "node_openssl_legacy"


def test_classify_git_remote_auth() -> None:
    signal = _make_signal("fatal: Could not read from remote repository.", "git_remote_auth")
    assert classify(signal).error_type == "git_remote_auth"


def test_classify_engines_strict_extracts_wanted_node() -> None:
    line = "ERR_PNPM_UNSUPPORTED_ENGINE  wanted: {'node': '>=20.1.0'}"
    signal = ErrorSignal(raw_line=line, source="stderr", error_type="engines_strict", context_lines=[line])
    err = classify(signal, runtime="node")
    assert err.error_type == "engines_strict"
    assert err.extracted.get("required") == "20.1.0"


def test_classify_glibc_extracts_tag() -> None:
    line = "version `GLIBC_2.34' not found"
    signal = ErrorSignal(raw_line=line, source="stderr", error_type="glibc_toolchain", context_lines=[line])
    err = classify(signal, runtime="python")
    assert err.error_type == "glibc_toolchain"
    assert err.extracted.get("glibc_need") == "2.34"
