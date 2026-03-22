"""Tests for normalizing ``.SHELLFLAGS`` in Makefiles."""

from pathlib import Path

from repofix.core.make_shellflags_fix import (
    env_with_term_for_make,
    fix_make_shellflags_in_repo,
    is_make_command,
)


def test_fix_line_removes_eu_keeps_trailing_c() -> None:
    from repofix.core import make_shellflags_fix as msf

    line = ".SHELLFLAGS = -eu -o pipefail -c\n"
    new, changed = msf._fix_shellflags_line(line)
    assert changed is True
    assert new == ".SHELLFLAGS = -o pipefail -c\n"


def test_fix_line_collapses_double_trailing_c() -> None:
    from repofix.core import make_shellflags_fix as msf

    line = ".SHELLFLAGS = -eu -o pipefail -c -c\n"
    new, changed = msf._fix_shellflags_line(line)
    assert changed is True
    assert new == ".SHELLFLAGS = -o pipefail -c\n"


def test_fix_line_noop_when_already_relaxed() -> None:
    from repofix.core import make_shellflags_fix as msf

    line = ".SHELLFLAGS = -o pipefail -c\n"
    new, changed = msf._fix_shellflags_line(line)
    assert changed is False
    assert new == line


def test_fix_line_relax_eu_without_c() -> None:
    from repofix.core import make_shellflags_fix as msf

    line = ".SHELLFLAGS = -eu -o pipefail\n"
    new, changed = msf._fix_shellflags_line(line)
    assert changed is True
    assert new == ".SHELLFLAGS = -o pipefail\n"


def test_is_make_command() -> None:
    assert is_make_command("make bootstrap") is True
    assert is_make_command("make") is True
    assert is_make_command("npm install") is False
    assert is_make_command(None) is False


def test_env_with_term_for_make_sets_xterm_when_dumb() -> None:
    e = env_with_term_for_make({"TERM": "dumb"})
    assert e["TERM"] == "xterm-256color"


def test_fix_repo_edits_mak_files(tmp_path: Path) -> None:
    cfg = tmp_path / ".config" / "make"
    cfg.mkdir(parents=True)
    mak = cfg / "00_config.mak"
    mak.write_text("SHELL:=/bin/bash\n.SHELLFLAGS = -eu -o pipefail -c\n", encoding="utf-8")
    assert fix_make_shellflags_in_repo(tmp_path) == 1
    text = mak.read_text()
    assert "-eu" not in text
    assert "-o pipefail -c" in text


def test_fix_shellflags_trailing_c_alias() -> None:
    from repofix.core import make_shellflags_fix as msf

    assert msf.fix_shellflags_trailing_c_in_repo is msf.fix_make_shellflags_in_repo
