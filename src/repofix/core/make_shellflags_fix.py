"""Normalize ``.SHELLFLAGS`` for GNU Make + bash.

GNU Make runs ``$(shell …)`` as ``$SHELL`` + ``$(.SHELLFLAGS)`` + *command* — it
does **not** insert ``-c`` itself. The ``-c`` flag must appear **inside**
``.SHELLFLAGS`` (e.g. ``-o pipefail -c``) or ``$(shell command -v …)`` is
invoked as a *script path* and bash reports::

    bash: command -v uv 2> /dev/null: No such file or directory

Repos such as MegaLinter set ``.SHELLFLAGS = -eu -o pipefail -c``. We remove
``-eu`` / ``-e`` / ``-u`` (errexit/nounset) so ``$(shell …)`` probes and rc files
behave, and **keep the trailing ``-c``** — stripping it breaks ``$(shell)``.

A duplicate trailing ``-c`` (``… -c -c``) is extremely rare; if present, we strip
one trailing ``-c`` so the value ends with a single ``-c``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_SHELLFLAGS = re.compile(r"^(\s*\.SHELLFLAGS\s*[:?]?=\s*)(.*)$")
# Collapse accidental "-c -c" at end (typos); keep a single "-c" for $(shell).
_DOUBLE_TRAILING_C = re.compile(r"\s+-c\s+-c\s*$")

_EXCLUDE_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        ".venv",
        ".venv-binary-build",
        "__pycache__",
        ".tox",
        "dist",
        "build",
        ".eggs",
    }
)


def _relax_errexit(shellflags_value: str) -> str:
    """Drop ``-e`` / ``-eu`` / ``-u`` shell options that break ``$(shell …)`` or bashrc under ``-u``.

    We remove ``-eu`` entirely (not ``-eu``→``-u``): nounset (``-u``) can make bash
    source system rc files and fail on ``PS1`` when Make runs non-interactive shells.
    """
    s = shellflags_value.strip()
    s = re.sub(r"(^|\s)-eu(?=\s|$)", r"\1", s)
    s = re.sub(r"(^|\s)-e(?=\s|$)", r"\1", s)
    s = re.sub(r"(^|\s)-u(?=\s|$)", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fix_shellflags_line(line: str) -> tuple[str, bool]:
    """Return (possibly updated line, True if changed). *line* may include newline(s)."""
    ending = ""
    core = line
    if line.endswith("\r\n"):
        ending = "\r\n"
        core = line[:-2]
    elif line.endswith("\n"):
        ending = "\n"
        core = line[:-1]
    elif line.endswith("\r"):
        ending = "\r"
        core = line[:-1]

    m = _SHELLFLAGS.match(core)
    if not m:
        return line, False
    prefix, rest = m.group(1), m.group(2)
    stripped = rest.rstrip()
    no_double_c = _DOUBLE_TRAILING_C.sub(" -c", stripped)
    relaxed = _relax_errexit(no_double_c)
    if relaxed == stripped:
        return line, False
    return prefix + relaxed + ending, True


def _iter_makefile_paths(repo_path: Path):
    for name in ("Makefile", "GNUmakefile", "makefile"):
        p = repo_path / name
        if p.is_file():
            yield p
    for root, dirs, files in os.walk(repo_path, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIR_NAMES]
        for f in files:
            if f.endswith(".mak"):
                yield Path(root) / f


def fix_make_shellflags_in_repo(repo_path: Path) -> int:
    """
    Patch ``.SHELLFLAGS`` lines (relax ``-e``/``-eu``; optional ``-c -c`` → ``-c``).
    Returns the number of files modified.
    """
    changed_files = 0
    for path in _iter_makefile_paths(repo_path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines(keepends=True)
        new_parts: list[str] = []
        modified = False
        for line in lines:
            nl, ch = _fix_shellflags_line(line)
            if ch:
                modified = True
            new_parts.append(nl)
        if modified:
            try:
                path.write_text("".join(new_parts), encoding="utf-8")
                changed_files += 1
            except OSError:
                continue
    return changed_files


# Backward-compatible name
fix_shellflags_trailing_c_in_repo = fix_make_shellflags_in_repo


def is_make_command(cmd: str | None) -> bool:
    """True if *cmd* runs GNU Make (``make`` or ``make target``)."""
    if not cmd:
        return False
    s = cmd.strip()
    if s == "make":
        return True
    return s.startswith("make ")


def env_with_term_for_make(env: dict[str, str] | None) -> dict[str, str]:
    """Ensure ``TERM`` is usable for ``tput`` in ``$(shell …)`` when running ``make``."""
    merged: dict[str, str] = {**(env or {})}
    t = merged.get("TERM") or os.environ.get("TERM", "")
    if not t or t == "dumb":
        merged["TERM"] = "xterm-256color"
    return merged
