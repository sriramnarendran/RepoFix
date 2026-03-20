"""Tests for docker compose bind-mount file repair."""

from __future__ import annotations

import json
from pathlib import Path

from repofix.core.docker_compose_bind_fix import (
    ensure_docker_compose_bind_files,
    iter_bind_file_mounts,
    repair_bind_host,
)


def test_iter_bind_file_mounts_short_syntax(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        """
services:
  app:
    image: test
    volumes:
      - ./host.conf:/app/config.conf
      - namedvol:/data
""",
        encoding="utf-8",
    )
    mounts = iter_bind_file_mounts(tmp_path)
    assert len(mounts) == 1
    host, container = mounts[0]
    assert host == (tmp_path / "host.conf").resolve()
    assert container == "/app/config.conf"


def test_repair_replaces_empty_directory_with_file(tmp_path: Path) -> None:
    ex_dir = tmp_path / "examples"
    ex_dir.mkdir(parents=True)
    example = {
        "storage": {"workspace": "./data"},
        "server": {"port": 1933},
    }
    (ex_dir / "ov.conf.example").write_text(json.dumps(example), encoding="utf-8")

    bad = tmp_path / "state" / "ov.conf"
    bad.parent.mkdir(parents=True)
    bad.mkdir()

    assert repair_bind_host(tmp_path, bad, "/app/ov.conf") is True
    assert bad.is_file()
    data = json.loads(bad.read_text(encoding="utf-8"))
    assert data["storage"]["workspace"] == "/app/data"


def test_ensure_skips_when_not_docker_run(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  x:\n    image: t\n    volumes:\n      - ./c.json:/app/c.json\n",
        encoding="utf-8",
    )
    d = tmp_path / "c.json"
    d.mkdir()
    ensure_docker_compose_bind_files(tmp_path, run_command="npm start", stack_is_docker=False)
    assert d.is_dir()


def test_ensure_fixes_when_docker_compose_run(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  x:\n    image: t\n    volumes:\n      - ./c.json:/app/c.json\n",
        encoding="utf-8",
    )
    d = tmp_path / "c.json"
    d.mkdir()
    ensure_docker_compose_bind_files(
        tmp_path, run_command="docker compose up", stack_is_docker=False
    )
    assert d.is_file()
