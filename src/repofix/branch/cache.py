"""Branch-aware dependency cache utilities.

Each git branch tracks its own dependency fingerprint so repofix can skip
reinstalling packages when switching back to a previously-set-up branch.

Isolation strategy by runtime:
  Python  → branch-specific venv:  .venv-<branch-slug>  (full isolation)
  Node    → shared node_modules; dep hash decides whether npm/yarn/pnpm re-runs
  Go/Rust → module caches are global; dep hash decides whether to re-run tidy/fetch
  Ruby    → shared vendor/bundle; dep hash controls bundle install
  Others  → dep hash tracked; no extra isolation needed
"""

from __future__ import annotations

import hashlib
from pathlib import Path


# Dependency manifest files — any change here invalidates the branch cache.
DEP_FILE_NAMES: list[str] = [
    # Python
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-prod.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "uv.lock",
    # Node
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    # Go
    "go.mod",
    "go.sum",
    # Rust
    "Cargo.toml",
    "Cargo.lock",
    # Ruby
    "Gemfile",
    "Gemfile.lock",
    # PHP
    "composer.json",
    "composer.lock",
    # Dart / Flutter
    "pubspec.yaml",
    "pubspec.lock",
    # Java / Kotlin
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradle.lockfile",
]


def get_current_branch(repo_path: Path) -> str:
    """Return the active git branch name, or 'HEAD' for detached/non-git repos."""
    try:
        from git import InvalidGitRepositoryError, Repo  # type: ignore[import]
        repo = Repo(repo_path, search_parent_directories=True)
        return repo.active_branch.name
    except Exception:
        return "HEAD"


def compute_dep_hash(repo_path: Path) -> tuple[str, list[str]]:
    """
    SHA-256 hash of the contents of every present dependency manifest.

    Returns:
        (hex_digest, list_of_found_filenames)

    An empty repo (no dep files at all) returns a stable sentinel hash so it
    can still be cached and compared correctly.
    """
    hasher = hashlib.sha256()
    found: list[str] = []
    for name in DEP_FILE_NAMES:
        dep_file = repo_path / name
        if dep_file.exists() and dep_file.is_file():
            found.append(name)
            hasher.update(name.encode())
            hasher.update(dep_file.read_bytes())

    if not found:
        # Stable sentinel so a no-dep-file repo still gets a consistent hash
        hasher.update(b"__no_dep_files__")

    return hasher.hexdigest(), found


def normalize_repo_key(source: str, repo_path: Path) -> str:
    """Stable, lowercase string key identifying a repo (URL or absolute path)."""
    if source.startswith(("http://", "https://", "git@")):
        key = source.rstrip("/")
        if key.endswith(".git"):
            key = key[:-4]
        return key.lower()
    return str(repo_path.resolve())


def branch_slug(branch: str) -> str:
    """Convert a branch name to a safe, short filesystem slug.

    Examples:
      "main"            → "main"
      "feature/my-work" → "feature-my-work"
      "HEAD"            → "HEAD"
    """
    safe = (
        branch
        .replace("/", "-")
        .replace("\\", "-")
        .replace(" ", "-")
        .replace(":", "-")
    )
    # Keep only alphanumeric, dash, underscore, dot
    safe = "".join(c for c in safe if c.isalnum() or c in "-_.")
    return safe[:48] or "branch"


def branch_venv_name(branch: str) -> str:
    """Return the .venv directory name to use for a given branch."""
    slug = branch_slug(branch)
    # main / master keep the canonical name for backwards compatibility
    if slug in ("main", "master"):
        return ".venv"
    return f".venv-{slug}"


def is_env_valid(repo_path: Path, runtime: str, env_dir: str) -> bool:
    """
    Sanity-check that the cached isolated environment is still usable.

    For Python: the branch-specific venv python binary must exist.
    For Node:   node_modules directory must exist.
    For others: assume valid (global caches are outside the repo).
    """
    rt = runtime.lower()
    if rt in ("python", "pip"):
        venv = Path(env_dir) if env_dir else repo_path / ".venv"
        return (venv / "bin" / "python").exists()
    if rt in ("node", "npm"):
        return (repo_path / "node_modules").exists()
    if rt == "ruby":
        return (repo_path / "vendor" / "bundle").exists()
    # Go, Rust, Java, PHP, Docker — their caches live in global dirs; trust them
    return True
