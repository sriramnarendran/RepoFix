"""Multi-service detection — identify frontend/backend in a single repo."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# ── Service role labels ───────────────────────────────────────────────────────

_FRONTEND_DIR_NAMES = {"frontend", "client", "web", "ui", "dashboard", "portal", "studio"}
_BACKEND_DIR_NAMES  = {"backend", "server", "api", "service", "services", "core", "engine", "worker"}
_APPS_DIR_NAMES     = {"apps", "packages"}   # monorepo roots (Turborepo / Nx)

# Files that indicate a runnable service lives in a directory
_SERVICE_INDICATORS = {
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "composer.json",
    "Gemfile",
    "pubspec.yaml",
    "Dockerfile",
    "docker-compose.yml",
}


@dataclass
class ServiceSpec:
    name: str           # display label: "frontend", "backend", "api", …
    path: Path          # absolute path to the service root directory
    role: str           # "frontend" | "backend" | "unknown"
    log_color: str = "cyan"   # Rich color for this service's log prefix


def detect_services(repo_path: Path) -> list[ServiceSpec] | None:
    """
    Return a list of ServiceSpecs if the repo contains multiple runnable
    services, or None if it appears to be a single-service repo.

    Detection strategies (in order):
      1. Named subdirectories (frontend/, backend/, client/, server/ …)
      2. Monorepo apps/ directory (Turborepo / Nx)
      3. Root-level mixed stack (e.g. Next.js root + Python backend/)
    """
    services: list[ServiceSpec] = []

    # ── Strategy 1: named frontend/backend subdirectories ─────────────────────
    services = _detect_named_dirs(repo_path)
    if services:
        return services

    # ── Strategy 2: monorepo apps/ or packages/ ───────────────────────────────
    services = _detect_monorepo(repo_path)
    if services:
        return services

    # ── Strategy 3: mixed root + backend subdir ───────────────────────────────
    services = _detect_mixed_root(repo_path)
    if services:
        return services

    return None


# ── Strategy 1: named subdirectories ─────────────────────────────────────────

_LABEL_COLORS = ["cyan", "magenta", "yellow", "blue", "green"]


def _detect_named_dirs(repo_path: Path) -> list[ServiceSpec]:
    found_frontend: list[ServiceSpec] = []
    found_backend:  list[ServiceSpec] = []

    for subdir in sorted(repo_path.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("."):
            continue
        if not _has_service_indicator(subdir):
            continue

        name = subdir.name.lower()
        if name in _FRONTEND_DIR_NAMES:
            found_frontend.append(ServiceSpec(
                name=subdir.name, path=subdir, role="frontend", log_color="cyan",
            ))
        elif name in _BACKEND_DIR_NAMES:
            found_backend.append(ServiceSpec(
                name=subdir.name, path=subdir, role="backend", log_color="magenta",
            ))

    if found_frontend and found_backend:
        return found_frontend + found_backend
    return []


# ── Strategy 2: monorepo (Turborepo / Nx / generic apps/) ────────────────────

def _detect_monorepo(repo_path: Path) -> list[ServiceSpec]:
    is_monorepo = (
        (repo_path / "turbo.json").exists()
        or (repo_path / "nx.json").exists()
        or (repo_path / "pnpm-workspace.yaml").exists()
        or (repo_path / "lerna.json").exists()
    )

    apps_dir = None
    for candidate in _APPS_DIR_NAMES:
        d = repo_path / candidate
        if d.is_dir():
            apps_dir = d
            break

    if not apps_dir:
        return []

    services: list[ServiceSpec] = []
    for idx, subdir in enumerate(sorted(apps_dir.iterdir())):
        if not subdir.is_dir() or subdir.name.startswith("."):
            continue
        if not _has_service_indicator(subdir):
            continue
        role = _infer_role(subdir)
        color = _LABEL_COLORS[idx % len(_LABEL_COLORS)]
        services.append(ServiceSpec(name=subdir.name, path=subdir, role=role, log_color=color))

    return services if len(services) >= 2 else []


# ── Strategy 3: mixed root + backend subdir ───────────────────────────────────

def _detect_mixed_root(repo_path: Path) -> list[ServiceSpec]:
    """
    Detect cases like:
      - Next.js at root  +  FastAPI or Express in backend/
      - Python at root   +  React in frontend/
    """
    root_role = _infer_role(repo_path)
    if root_role == "unknown":
        return []

    complement_names = (
        _BACKEND_DIR_NAMES if root_role == "frontend" else _FRONTEND_DIR_NAMES
    )
    complement_color = "magenta" if root_role == "frontend" else "cyan"
    root_color       = "cyan"    if root_role == "frontend" else "magenta"

    for subdir in sorted(repo_path.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("."):
            continue
        if subdir.name.lower() not in complement_names:
            continue
        if not _has_service_indicator(subdir):
            continue

        complement_role = "backend" if root_role == "frontend" else "frontend"
        return [
            ServiceSpec(name="root", path=repo_path, role=root_role, log_color=root_color),
            ServiceSpec(name=subdir.name, path=subdir, role=complement_role, log_color=complement_color),
        ]

    return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _has_service_indicator(path: Path) -> bool:
    return any((path / f).exists() for f in _SERVICE_INDICATORS)


def _infer_role(path: Path) -> str:
    """Guess frontend vs backend from the files present."""
    if (path / "package.json").exists():
        try:
            pkg = json.loads((path / "package.json").read_text())
        except Exception:
            return "unknown"
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        frontend_keys = {"next", "react", "vue", "@angular/core", "svelte", "nuxt", "gatsby", "vite"}
        backend_keys  = {"express", "fastify", "@nestjs/core", "koa", "hapi"}
        if any(k in deps for k in frontend_keys):
            return "frontend"
        if any(k in deps for k in backend_keys):
            return "backend"
        return "unknown"

    if any((path / f).exists() for f in ("requirements.txt", "pyproject.toml", "setup.py", "go.mod", "Cargo.toml")):
        return "backend"

    if (path / "docker-compose.yml").exists() or (path / "Dockerfile").exists():
        return "backend"

    return "unknown"
