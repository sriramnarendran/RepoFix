"""Tests for multi-service detection."""

from __future__ import annotations

import json
from pathlib import Path

from repofix.detection.multi import ServiceSpec, detect_services


def _pkg(deps: dict) -> str:
    return json.dumps({"dependencies": deps, "scripts": {"dev": "vite", "start": "node ."}})


def _make_frontend(path: Path, name: str = "frontend") -> Path:
    d = path / name
    d.mkdir()
    (d / "package.json").write_text(_pkg({"next": "^14", "react": "^18"}))
    return d


def _make_backend(path: Path, name: str = "backend") -> Path:
    d = path / name
    d.mkdir()
    (d / "requirements.txt").write_text("fastapi\nuvicorn\n")
    return d


# ── Named subdirectories ──────────────────────────────────────────────────────

def test_detects_frontend_backend_dirs(tmp_path: Path) -> None:
    _make_frontend(tmp_path)
    _make_backend(tmp_path)
    services = detect_services(tmp_path)
    assert services is not None
    assert len(services) == 2
    roles = {s.role for s in services}
    assert "frontend" in roles
    assert "backend" in roles


def test_detects_client_server_dirs(tmp_path: Path) -> None:
    _make_frontend(tmp_path, name="client")
    _make_backend(tmp_path, name="server")
    services = detect_services(tmp_path)
    assert services is not None
    assert len(services) == 2


def test_detects_web_api_dirs(tmp_path: Path) -> None:
    _make_frontend(tmp_path, name="web")
    _make_backend(tmp_path, name="api")
    services = detect_services(tmp_path)
    assert services is not None
    assert len(services) == 2


def test_single_service_returns_none(tmp_path: Path) -> None:
    _make_frontend(tmp_path)
    result = detect_services(tmp_path)
    assert result is None


def test_no_indicator_files_returns_none(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend").mkdir()
    # No package.json or requirements.txt — not real services
    result = detect_services(tmp_path)
    assert result is None


def test_service_names_preserved(tmp_path: Path) -> None:
    _make_frontend(tmp_path, name="client")
    _make_backend(tmp_path, name="server")
    services = detect_services(tmp_path)
    assert services is not None
    names = {s.name for s in services}
    assert "client" in names
    assert "server" in names


def test_service_paths_are_subdirs(tmp_path: Path) -> None:
    _make_frontend(tmp_path)
    _make_backend(tmp_path)
    services = detect_services(tmp_path)
    assert services is not None
    for svc in services:
        assert svc.path != tmp_path
        assert svc.path.is_dir()


def test_services_have_log_colors(tmp_path: Path) -> None:
    _make_frontend(tmp_path)
    _make_backend(tmp_path)
    services = detect_services(tmp_path)
    assert services is not None
    for svc in services:
        assert svc.log_color


# ── Monorepo detection ────────────────────────────────────────────────────────

def test_detects_turborepo_apps(tmp_path: Path) -> None:
    (tmp_path / "turbo.json").write_text("{}")
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "web").mkdir()
    (apps / "web" / "package.json").write_text(_pkg({"next": "^14"}))
    (apps / "api").mkdir()
    (apps / "api" / "package.json").write_text(_pkg({"express": "^4"}))
    services = detect_services(tmp_path)
    assert services is not None
    assert len(services) == 2


def test_monorepo_without_turbo_json(tmp_path: Path) -> None:
    """Generic apps/ dir without turbo.json should still be detected."""
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "frontend").mkdir()
    (apps / "frontend" / "package.json").write_text(_pkg({"react": "^18"}))
    (apps / "backend").mkdir()
    (apps / "backend" / "requirements.txt").write_text("django\n")
    services = detect_services(tmp_path)
    assert services is not None
    assert len(services) == 2


def test_monorepo_single_app_returns_none(tmp_path: Path) -> None:
    (tmp_path / "turbo.json").write_text("{}")
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "web").mkdir()
    (apps / "web" / "package.json").write_text(_pkg({"next": "^14"}))
    services = detect_services(tmp_path)
    assert services is None


# ── Mixed root detection ──────────────────────────────────────────────────────

def test_detects_nextjs_root_with_backend_subdir(tmp_path: Path) -> None:
    """Next.js at root + FastAPI in backend/ subdir."""
    (tmp_path / "package.json").write_text(_pkg({"next": "^14", "react": "^18"}))
    _make_backend(tmp_path)
    services = detect_services(tmp_path)
    assert services is not None
    assert len(services) == 2
    roles = {s.role for s in services}
    assert "frontend" in roles
    assert "backend" in roles


def test_detects_python_root_with_frontend_subdir(tmp_path: Path) -> None:
    """FastAPI at root + React in frontend/ subdir."""
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    _make_frontend(tmp_path)
    services = detect_services(tmp_path)
    assert services is not None
    assert len(services) == 2


def test_mixed_root_service_named_root(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(_pkg({"next": "^14"}))
    _make_backend(tmp_path)
    services = detect_services(tmp_path)
    assert services is not None
    root_svc = next((s for s in services if s.path == tmp_path), None)
    assert root_svc is not None
    assert root_svc.name == "root"
