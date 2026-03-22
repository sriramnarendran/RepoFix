"""Tests for automatic shell completion installation."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_maybe_install_skips_when_ci(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CI", "true")
    import shellingham

    monkeypatch.setattr(shellingham, "detect_shell", lambda: ("bash", "/bin/bash"))

    from repofix.shell_completion_auto import maybe_install_shell_completion

    maybe_install_shell_completion()
    assert not (tmp_path / ".local/share/bash-completion/completions/repofix").exists()


def test_maybe_install_skips_when_opt_out(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("REPOFIX_NO_AUTO_COMPLETION", "1")
    import shellingham

    monkeypatch.setattr(shellingham, "detect_shell", lambda: ("bash", "/bin/bash"))

    from repofix.shell_completion_auto import maybe_install_shell_completion

    maybe_install_shell_completion()
    assert not (tmp_path / ".local/share/bash-completion/completions/repofix").exists()


def test_maybe_install_writes_bash_completion(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("REPOFIX_NO_AUTO_COMPLETION", raising=False)
    import shellingham

    monkeypatch.setattr(shellingham, "detect_shell", lambda: ("bash", "/bin/bash"))

    from repofix.shell_completion_auto import maybe_install_shell_completion

    maybe_install_shell_completion()
    path = tmp_path / ".local/share/bash-completion/completions/repofix"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "_repofix_completion" in text
    assert "repofix" in text


def test_maybe_install_writes_fish_completion(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CI", raising=False)
    import shellingham

    monkeypatch.setattr(shellingham, "detect_shell", lambda: ("fish", "/usr/bin/fish"))

    from repofix.shell_completion_auto import maybe_install_shell_completion

    maybe_install_shell_completion()
    path = tmp_path / ".config/fish/completions/repofix.fish"
    assert path.is_file()
    assert "fish" in path.read_text(encoding="utf-8").lower() or "complete" in path.read_text(encoding="utf-8")
