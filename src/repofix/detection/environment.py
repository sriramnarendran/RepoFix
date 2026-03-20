"""Detect required environment variables from .env.example and source code."""

from __future__ import annotations

import re
from pathlib import Path


def parse_env_example(repo_path: Path) -> dict[str, str]:
    """
    Parse .env.example (or .env.sample / .env.template) and return
    a dict of {VAR_NAME: default_value_or_empty}.
    """
    for candidate in (".env.example", ".env.sample", ".env.template", ".env.defaults"):
        env_file = repo_path / candidate
        if env_file.exists():
            return _parse_dotenv_file(env_file)
    return {}


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for raw_line in path.read_text(errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    result[key] = value
    except Exception:
        pass
    return result


def scan_code_for_env_vars(repo_path: Path) -> set[str]:
    """
    Scan source files for environment variable references.
    Returns a set of variable names referenced in code.
    """
    patterns = [
        re.compile(r'process\.env\.([A-Z_][A-Z0-9_]*)'),                    # JS/TS
        re.compile(r'os\.environ(?:\.get)?\(["\']([A-Z_][A-Z0-9_]*)'),      # Python
        re.compile(r'os\.Getenv\(["\']([A-Z_][A-Z0-9_]*)'),                 # Go
        re.compile(r'std::env::var\(["\']([A-Z_][A-Z0-9_]*)'),              # Rust
        re.compile(r'ENV\[["\']([A-Z_][A-Z0-9_]*)'),                        # Ruby
        re.compile(r'\$_ENV\[["\']([A-Z_][A-Z0-9_]*)'),                     # PHP
        re.compile(r'System\.getenv\(["\']([A-Z_][A-Z0-9_]*)'),             # Java
    ]

    found: set[str] = set()
    _SCAN_EXTENSIONS = {".js", ".ts", ".jsx", ".tsx", ".py", ".go", ".rs", ".rb", ".php", ".java", ".kt"}
    _SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next"}

    for file_path in repo_path.rglob("*"):
        if file_path.is_file() and file_path.suffix in _SCAN_EXTENSIONS:
            if any(skip in file_path.parts for skip in _SKIP_DIRS):
                continue
            try:
                text = file_path.read_text(errors="replace")
                for pattern in patterns:
                    found.update(pattern.findall(text))
            except Exception:
                pass

    return found
