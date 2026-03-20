"""Tests for stack detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repofix.detection.stack import StackInfo, detect, detect_without_docker


def test_detects_nextjs(node_repo: Path) -> None:
    info = detect(node_repo)
    assert info.language == "Node.js"
    assert info.framework == "Next.js"
    assert info.runtime == "node"
    assert info.project_type == "frontend"


def test_detects_express(tmp_path: Path) -> None:
    pkg = {"dependencies": {"express": "^4.18.0"}, "scripts": {"start": "node index.js"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    info = detect(tmp_path)
    assert info.framework == "Express"
    assert info.project_type == "backend"


def test_detects_flask(python_flask_repo: Path) -> None:
    info = detect(python_flask_repo)
    assert info.language == "Python"
    assert info.framework == "Flask"
    assert info.runtime == "python"


def test_detects_fastapi(python_fastapi_repo: Path) -> None:
    info = detect(python_fastapi_repo)
    assert info.framework == "FastAPI"


def test_detects_docker_compose(docker_compose_repo: Path) -> None:
    info = detect(docker_compose_repo)
    assert info.runtime == "docker"
    assert info.extras["mode"] == "compose"
    assert "web" in info.extras["services"]
    assert "db" in info.extras["services"]


def test_detects_dockerfile(dockerfile_repo: Path) -> None:
    info = detect(dockerfile_repo)
    assert info.runtime == "docker"
    assert info.extras["mode"] == "dockerfile"


def test_docker_takes_priority_over_nodejs(tmp_path: Path) -> None:
    """Docker should be detected even when package.json also exists."""
    (tmp_path / "docker-compose.yml").write_text("version: '3'\nservices:\n  web:\n    build: .\n")
    pkg = {"dependencies": {"react": "^18.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    info = detect(tmp_path)
    assert info.runtime == "docker"


def test_detect_without_docker_uses_python_when_compose_present(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "version: '3'\nservices:\n  web:\n    image: test\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")

    info = detect_without_docker(tmp_path)
    assert info.runtime == "python"
    assert info.language == "Python"


def test_detects_go(go_repo: Path) -> None:
    info = detect(go_repo)
    assert info.language == "Go"
    assert info.runtime == "go"


def test_unknown_stack_returns_unknown(tmp_path: Path) -> None:
    info = detect(tmp_path)
    assert not info.is_known()
    assert info.language == "unknown"


def test_gin_framework_detection(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.com/app\n\ngo 1.21\n\nrequire github.com/gin-gonic/gin v1.9.1\n"
    )
    info = detect(tmp_path)
    assert info.framework == "Gin"


def test_django_detection(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("django==5.0\ngunicorn\n")
    info = detect(tmp_path)
    assert info.framework == "Django"
    assert info.project_type == "fullstack"
