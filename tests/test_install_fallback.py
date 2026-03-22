"""Tests for install fallback when Make invokes bash with a broken -c chain."""

from pathlib import Path

from repofix.core import install_fallback as fb


def test_detects_bash_c_eof(tmp_path: Path) -> None:
    out = "make[2]: *** [bootstrap] Error 2\n/bin/bash: -c: line 1: syntax error: unexpected end of file\n"
    assert fb.is_make_duplicate_shellflags_c_bug(out) is True
    assert fb.suggest_node_install_after_make_shell_bug(tmp_path, "make bootstrap", out) is None


def test_suggests_npm_when_package_json_exists(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}\n")
    out = "/bin/bash: -c: line 1: syntax error: unexpected end of file\n"
    assert fb.suggest_node_install_after_make_shell_bug(tmp_path, "make bootstrap", out) == "npm install"


def test_non_make_no_suggest(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}\n")
    out = "/bin/bash: -c: line 1: syntax error: unexpected end of file\n"
    assert fb.suggest_node_install_after_make_shell_bug(tmp_path, "npm install", out) is None


def test_pnpm_lock_priority(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("lockfile: stub\n")
    out = "bash: -c: line 1: syntax error: unexpected end of file\n"
    assert fb.suggest_node_install_after_make_shell_bug(tmp_path, "make x", out) == "pnpm install"
