"""Tests for stripping duplicate ``-c`` from ``.SHELLFLAGS`` in Makefiles."""

from pathlib import Path

from repofix.core.make_shellflags_fix import (
    fix_shellflags_trailing_c_in_repo,
    is_make_command,
)


def test_fix_line_strips_trailing_c() -> None:
    from repofix.core import make_shellflags_fix as msf

    line = ".SHELLFLAGS = -eu -o pipefail -c\n"
    new, changed = msf._fix_shellflags_line(line)
    assert changed is True
    assert new == ".SHELLFLAGS = -eu -o pipefail\n"

    unchanged, changed2 = msf._fix_shellflags_line(".SHELLFLAGS = -eu -o pipefail\n")
    assert changed2 is False
    assert unchanged == ".SHELLFLAGS = -eu -o pipefail\n"


def test_is_make_command() -> None:
    assert is_make_command("make bootstrap") is True
    assert is_make_command("make") is True
    assert is_make_command("npm install") is False
    assert is_make_command(None) is False


def test_fix_repo_edits_mak_files(tmp_path: Path) -> None:
    cfg = tmp_path / ".config" / "make"
    cfg.mkdir(parents=True)
    mak = cfg / "00_config.mak"
    mak.write_text("SHELL:=/bin/bash\n.SHELLFLAGS = -eu -o pipefail -c\n", encoding="utf-8")
    assert fix_shellflags_trailing_c_in_repo(tmp_path) == 1
    assert "-c" not in mak.read_text()
    assert "pipefail" in mak.read_text()
