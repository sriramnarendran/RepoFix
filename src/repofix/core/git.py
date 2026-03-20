"""Git operations — clone, checkout, validate."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import git
from git import GitCommandError, InvalidGitRepositoryError, Repo

from repofix import config as cfg
from repofix.output import display


class GitError(Exception):
    pass


_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)
_GIT_SSH_RE = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")


def is_remote_url(source: str) -> bool:
    return source.startswith(("http://", "https://", "git@"))


def parse_repo_name(url: str) -> str:
    for pattern in (_GITHUB_URL_RE, _GIT_SSH_RE):
        m = pattern.match(url)
        if m:
            return m.group("repo")
    parsed = urlparse(url)
    return Path(parsed.path).stem or "repo"


def resolve_repo(source: str, branch: str | None = None) -> Path:
    """
    Clone a remote URL or validate a local path.
    Returns the absolute path to the repo root.
    """
    if is_remote_url(source):
        return _clone(source, branch)
    return _validate_local(source)


def _clone(url: str, branch: str | None) -> Path:
    app_cfg = cfg.load()
    base = Path(app_cfg.clone_base_dir)
    base.mkdir(parents=True, exist_ok=True)

    repo_name = parse_repo_name(url)
    dest = base / repo_name

    if dest.exists():
        display.info(f"Repo already cloned at [bold]{dest}[/bold] — updating…")
        try:
            repo = Repo(dest)
            origin = repo.remotes.origin
            with display.spinner("Pulling latest changes"):
                origin.pull()
            if branch:
                _checkout_branch(repo, branch)
            display.success(f"Updated [bold]{repo_name}[/bold]")
            return dest
        except Exception as exc:
            display.warning(f"Could not update existing clone: {exc} — re-cloning")
            shutil.rmtree(dest, ignore_errors=True)

    display.step(f"Cloning [bold]{url}[/bold]")
    try:
        clone_kwargs: dict = {"depth": 1}
        if branch:
            clone_kwargs["branch"] = branch
        with display.spinner(f"Cloning {repo_name}…"):
            Repo.clone_from(url, dest, **clone_kwargs)
        display.success(f"Cloned to [bold]{dest}[/bold]")
        return dest
    except GitCommandError as exc:
        raise GitError(f"Clone failed: {exc}") from exc


def _validate_local(path_str: str) -> Path:
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        raise GitError(f"Local path does not exist: {path}")
    if not path.is_dir():
        raise GitError(f"Path is not a directory: {path}")
    try:
        Repo(path, search_parent_directories=True)
    except InvalidGitRepositoryError:
        display.warning(f"{path} is not a git repository — continuing anyway")
    display.success(f"Using local path [bold]{path}[/bold]")
    return path


def _checkout_branch(repo: Repo, branch: str) -> None:
    try:
        if branch in [h.name for h in repo.heads]:
            repo.heads[branch].checkout()
        else:
            repo.git.checkout("-b", branch, f"origin/{branch}")
        display.success(f"Checked out branch [bold]{branch}[/bold]")
    except GitCommandError as exc:
        raise GitError(f"Could not checkout branch '{branch}': {exc}") from exc


def get_default_branch(repo_path: Path) -> str | None:
    try:
        repo = Repo(repo_path)
        return repo.active_branch.name
    except Exception:
        return None
