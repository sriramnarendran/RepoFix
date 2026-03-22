"""Locate and read README files at a repository root."""

from __future__ import annotations

import re
from pathlib import Path

# Prefer common spellings before scanning the directory.
_README_CANDIDATES: tuple[str, ...] = (
    "README.md",
    "readme.md",
    "Readme.md",
    "README.markdown",
    "readme.markdown",
    "README.rst",
    "readme.rst",
    "README.txt",
    "README",
)

_README_ROOT_NAME_RE = re.compile(r"^readme(?:\.(md|markdown|rst|txt))?$", re.IGNORECASE)


def find_readme_path(repo_path: Path) -> Path | None:
    """Return the path to a root-level README file, or None if not found."""
    for name in _README_CANDIDATES:
        p = repo_path / name
        if p.is_file():
            return p
    try:
        for entry in sorted(repo_path.iterdir()):
            if entry.is_file() and _README_ROOT_NAME_RE.match(entry.name):
                return entry
    except OSError:
        pass
    return None


def read_readme_text(repo_path: Path, *, max_chars: int = 8000) -> str | None:
    """Read README text with UTF-8 (BOM allowed), truncated to *max_chars* when > 0."""
    path = find_readme_path(repo_path)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars]
    return text
