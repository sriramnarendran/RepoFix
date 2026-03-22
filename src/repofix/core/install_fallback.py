"""Recover from broken Makefiles that set ``.SHELLFLAGS`` with a trailing ``-c``.

GNU Make already invokes ``$SHELL -c <recipe>``. If a Makefile's ``.SHELLFLAGS``
includes ``-c`` (as in MegaLinter's ``00_config.mak``), the shell receives two
``-c`` flags and may run an empty script, yielding::

    /bin/bash: -c: line 1: syntax error: unexpected end of file

When that pattern appears and the install command was ``make …``, fall back to
the project's Node installer if ``package.json`` exists.
"""

from __future__ import annotations

import re
from pathlib import Path

# e.g. "/bin/bash: -c: line 1: syntax error: unexpected end of file"
_BASH_C_EOF = re.compile(
    r"bash:\s*-c:\s*line\s+1:\s*syntax error:\s*unexpected end of file",
    re.IGNORECASE,
)


def is_make_duplicate_shellflags_c_bug(output: str) -> bool:
    """True if *output* matches the GNU Make + duplicate ``-c`` failure pattern."""
    return bool(_BASH_C_EOF.search(output))


def pick_node_install_command(repo_path: Path) -> str | None:
    """Return npm/pnpm/yarn install for *repo_path* if ``package.json`` exists."""
    if not (repo_path / "package.json").is_file():
        return None
    if (repo_path / "pnpm-lock.yaml").is_file():
        return "pnpm install"
    if (repo_path / "yarn.lock").is_file():
        return "yarn install"
    return "npm install"


def suggest_node_install_after_make_shell_bug(
    repo_path: Path,
    install_cmd: str,
    full_output: str,
) -> str | None:
    """
    If ``make`` failed with the duplicate-``-c`` pattern and the repo has
    Node metadata, return an alternate install command; otherwise ``None``.
    """
    if not is_make_duplicate_shellflags_c_bug(full_output):
        return None
    if not install_cmd.strip().startswith("make"):
        return None
    return pick_node_install_command(repo_path)
