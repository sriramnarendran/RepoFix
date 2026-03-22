"""Fix Makefiles that end ``.SHELLFLAGS`` with ``-c``.

GNU Make invokes the shell as ``$SHELL $SHELLFLAGS -c <recipe>``. A trailing
``-c`` in ``.SHELLFLAGS`` duplicates the flag and bash may run an empty script::

    /bin/bash: -c: line 1: syntax error: unexpected end of file

Some repos (e.g. MegaLinter) set ``.SHELLFLAGS = -eu -o pipefail -c``. We strip
only a final ``-c`` token from those lines in Makefiles and ``*.mak`` files.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_SHELLFLAGS = re.compile(r"^(\s*\.SHELLFLAGS\s*[:?]?=\s*)(.*)$")
_TRAILING_C = re.compile(r"\s+-c\s*$")

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
    new_rest = _TRAILING_C.sub("", stripped)
    if new_rest == stripped:
        return line, False
    return prefix + new_rest + ending, True


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


def fix_shellflags_trailing_c_in_repo(repo_path: Path) -> int:
    """
    Remove a trailing ``-c`` from ``.SHELLFLAGS`` assignments. Returns the
    number of files modified.
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


def is_make_command(cmd: str | None) -> bool:
    """True if *cmd* runs GNU Make (``make`` or ``make target``)."""
    if not cmd:
        return False
    s = cmd.strip()
    if s == "make":
        return True
    return s.startswith("make ")
