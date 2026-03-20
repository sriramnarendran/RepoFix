"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Return an empty temporary directory representing a repo root."""
    return tmp_path


@pytest.fixture()
def node_repo(tmp_path: Path) -> Path:
    pkg = {
        "name": "my-app",
        "version": "1.0.0",
        "dependencies": {"next": "^14.0.0", "react": "^18.0.0"},
        "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    return tmp_path


@pytest.fixture()
def python_flask_repo(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\ngunicorn==21.0.0\n")
    (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    return tmp_path


@pytest.fixture()
def python_fastapi_repo(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text("fastapi==0.110.0\nuvicorn==0.29.0\n")
    return tmp_path


@pytest.fixture()
def docker_compose_repo(tmp_path: Path) -> Path:
    compose = """
version: "3"
services:
  web:
    build: .
    ports:
      - "8080:8080"
  db:
    image: postgres
"""
    (tmp_path / "docker-compose.yml").write_text(compose)
    return tmp_path


@pytest.fixture()
def dockerfile_repo(tmp_path: Path) -> Path:
    (tmp_path / "Dockerfile").write_text("FROM node:20\nCOPY . .\nRUN npm install\nCMD [\"node\", \"index.js\"]\n")
    return tmp_path


@pytest.fixture()
def go_repo(tmp_path: Path) -> Path:
    (tmp_path / "go.mod").write_text("module example.com/myapp\n\ngo 1.21\n")
    (tmp_path / "main.go").write_text('package main\nimport "fmt"\nfunc main() { fmt.Println("hello") }\n')
    return tmp_path
