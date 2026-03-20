"""Tests for command discovery."""

from __future__ import annotations

import json
from pathlib import Path

from repofix.detection.commands import CommandSet, _from_makefile, discover
from repofix.detection.stack import StackInfo


def _node_stack() -> StackInfo:
    return StackInfo(language="Node.js", framework="Next.js", project_type="frontend", runtime="node")


def _python_stack() -> StackInfo:
    return StackInfo(language="Python", framework="FastAPI", project_type="backend", runtime="python")


def _docker_stack(mode: str = "compose") -> StackInfo:
    return StackInfo(
        language="Docker",
        framework="docker-compose",
        project_type="service",
        runtime="docker",
        extras={"mode": mode, "services": []},
    )


# ── package.json ──────────────────────────────────────────────────────────────

def test_discovers_from_package_json(node_repo: Path) -> None:
    stack = _node_stack()
    cmds = discover(node_repo, stack)
    assert cmds.source == "package.json"
    assert cmds.run is not None
    assert "dev" in cmds.run
    assert cmds.install is not None
    assert "npm install" in cmds.install
    assert cmds.build is not None


def test_prefers_dev_over_start(tmp_path: Path) -> None:
    pkg = {
        "scripts": {"start": "node index.js", "dev": "nodemon index.js", "build": "webpack"}
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    stack = StackInfo(language="Node.js", framework="Express", project_type="backend", runtime="node")
    cmds = discover(tmp_path, stack)
    assert "dev" in cmds.run


def test_yarn_detection(tmp_path: Path) -> None:
    pkg = {"scripts": {"dev": "vite"}, "dependencies": {"vite": "^5.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    (tmp_path / "yarn.lock").write_text("")
    stack = StackInfo(language="Node.js", framework="Vite", project_type="frontend", runtime="node")
    cmds = discover(tmp_path, stack)
    assert "yarn install" in cmds.install
    assert "yarn run dev" in cmds.run


# ── CLI overrides ─────────────────────────────────────────────────────────────

def test_cli_override_wins(node_repo: Path) -> None:
    stack = _node_stack()
    cmds = discover(node_repo, stack, override_run="my-custom-run", override_install="my-custom-install")
    assert cmds.run == "my-custom-run"
    assert cmds.install == "my-custom-install"


def test_partial_override_install_only(node_repo: Path) -> None:
    stack = _node_stack()
    cmds = discover(node_repo, stack, override_install="make install")
    assert cmds.install == "make install"
    assert cmds.run is not None  # filled from package.json


def test_partial_override_run_only(node_repo: Path) -> None:
    stack = _node_stack()
    cmds = discover(node_repo, stack, override_run="make run")
    assert cmds.run == "make run"
    assert cmds.install is not None  # filled from package.json


# ── Makefile ──────────────────────────────────────────────────────────────────

def test_makefile_beats_package_json(tmp_path: Path) -> None:
    """Makefile run target should win over package.json scripts."""
    (tmp_path / "Makefile").write_text("install:\n\tpip install -r requirements.txt\n\nrun:\n\tpython app.py\n")
    pkg = {"scripts": {"start": "node index.js"}, "dependencies": {}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    stack = _python_stack()
    cmds = discover(tmp_path, stack)
    assert cmds.source == "Makefile"
    assert "make run" in cmds.run
    assert "make install" in cmds.install


def test_makefile_discovery_basic(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("install:\n\tpip install -r requirements.txt\n\nrun:\n\tpython app.py\n")
    stack = _python_stack()
    cmds = discover(tmp_path, stack)
    assert cmds.source == "Makefile"
    assert "make run" in cmds.run
    assert "make install" in cmds.install


def test_makefile_dev_target(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("dev:\n\tnpm run dev\n\ninstall:\n\tnpm ci\n")
    stack = _node_stack()
    cmds = discover(tmp_path, stack)
    assert "make dev" in cmds.run


def test_makefile_up_target(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("up:\n\tdocker compose up\n\nsetup:\n\tdocker compose build\n")
    stack = _docker_stack()
    cmds = discover(tmp_path, stack)
    assert "make up" in cmds.run
    assert "make setup" in cmds.install


def test_makefile_bootstrap_target(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("bootstrap:\n\tnpm install\n\nstart:\n\tnode server.js\n")
    stack = _node_stack()
    cmds = discover(tmp_path, stack)
    assert "make bootstrap" in cmds.install
    assert "make start" in cmds.run


def test_gnumakefile_detected(tmp_path: Path) -> None:
    (tmp_path / "GNUmakefile").write_text("run:\n\tgo run .\n\ninstall:\n\tgo mod download\n")
    stack = StackInfo(language="Go", framework="Go", project_type="backend", runtime="go")
    cmds = discover(tmp_path, stack)
    assert cmds.source == "Makefile"
    assert "make run" in cmds.run


def test_makefile_no_useful_targets_falls_through(tmp_path: Path) -> None:
    """A Makefile with only test/lint targets should fall through to defaults."""
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n\nlint:\n\truff check .\n")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    stack = _python_stack()
    cmds = discover(tmp_path, stack)
    # No run/install in Makefile → falls back to stack defaults
    assert "uvicorn" in cmds.run


def test_makefile_gaps_filled_from_stack(tmp_path: Path) -> None:
    """Makefile has run but no install → install comes from stack defaults."""
    (tmp_path / "Makefile").write_text("run:\n\tpython main.py\n")
    (tmp_path / "requirements.txt").write_text("flask\n")
    stack = _python_stack()
    cmds = discover(tmp_path, stack)
    assert cmds.source == "Makefile"
    assert "make run" in cmds.run
    assert "pip install" in cmds.install  # gap filled from defaults


def test_makefile_with_build_target(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("install:\n\tnpm ci\n\nbuild:\n\tnpm run build\n\nstart:\n\tnode dist/index.js\n")
    stack = _node_stack()
    cmds = discover(tmp_path, stack)
    assert "make build" in cmds.build
    assert "make install" in cmds.install


# ── Docker stack ──────────────────────────────────────────────────────────────

def test_docker_stack_ignores_package_json(tmp_path: Path) -> None:
    """Docker repo with package.json should NOT pick npm install."""
    (tmp_path / "docker-compose.yml").write_text(
        "version: '3'\nservices:\n  web:\n    build: .\n"
    )
    pkg = {"scripts": {"start": "node index.js"}, "dependencies": {"express": "^4"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    from repofix.detection.stack import detect
    stack = detect(tmp_path)
    cmds = discover(tmp_path, stack)
    assert "docker compose up" in cmds.run
    assert cmds.install is None or "npm" not in (cmds.install or "")


def test_docker_compose_commands(docker_compose_repo: Path) -> None:
    from repofix.detection.stack import detect
    stack = detect(docker_compose_repo)
    cmds = discover(docker_compose_repo, stack)
    assert "docker compose up" in cmds.run
    assert cmds.source == "docker-compose.yml"


def test_docker_with_makefile_uses_makefile(tmp_path: Path) -> None:
    """Docker repo with Makefile run target should prefer Makefile."""
    (tmp_path / "docker-compose.yml").write_text(
        "version: '3'\nservices:\n  app:\n    build: .\n"
    )
    (tmp_path / "Makefile").write_text("up:\n\tdocker compose up --build\n\ndown:\n\tdocker compose down\n")
    from repofix.detection.stack import detect
    stack = detect(tmp_path)
    cmds = discover(tmp_path, stack)
    assert "make up" in cmds.run
    assert cmds.source == "Makefile"


# ── Procfile ──────────────────────────────────────────────────────────────────

def test_procfile_discovery(tmp_path: Path) -> None:
    (tmp_path / "Procfile").write_text("web: gunicorn app:app\nworker: celery worker\n")
    stack = _python_stack()
    cmds = discover(tmp_path, stack)
    assert cmds.source == "Procfile"
    assert "gunicorn" in cmds.run


# ── Stack defaults ────────────────────────────────────────────────────────────

def test_python_defaults(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    stack = _python_stack()
    cmds = discover(tmp_path, stack)
    assert "uvicorn" in cmds.run
    assert "pip install" in cmds.install
