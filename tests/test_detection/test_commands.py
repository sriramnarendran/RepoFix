"""Tests for command discovery."""

from __future__ import annotations

import json
from pathlib import Path

from repofix.detection.commands import (
    CommandSet,
    _extract_readme_commands,
    _from_makefile,
    _from_readme_heuristic,
    _split_readme_sections,
    discover,
)
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


def test_subpackage_bin_when_root_package_json_is_stub(tmp_path: Path) -> None:
    """Root package.json with no scripts/bin — CLI lives in a child folder (MegaLinter-style)."""
    (tmp_path / "package.json").write_text(
        json.dumps({"private": True, "name": "dummy", "dependencies": {}})
    )
    runner = tmp_path / "mega-linter-runner"
    runner.mkdir()
    (runner / "package.json").write_text(
        json.dumps({"name": "mega-linter-runner", "bin": {"mega-linter-runner": "lib/index.js"}})
    )
    lib = runner / "lib"
    lib.mkdir()
    (lib / "index.js").write_text("// cli\n")
    stack = StackInfo(language="Node.js", framework="Node.js", project_type="backend", runtime="node")
    cmds = discover(tmp_path, stack)
    assert cmds.source == "subpackage-bin"
    assert cmds.run == "node mega-linter-runner/lib/index.js"
    assert cmds.install == "npm install --prefix mega-linter-runner"


def test_makefile_bootstrap_skipped_for_subpackage_bin_install(tmp_path: Path) -> None:
    """Makefile bootstrap must not override npm install in the CLI subpackage."""
    (tmp_path / "Makefile").write_text("bootstrap:\n\tnpm ci\n")
    (tmp_path / "package.json").write_text(json.dumps({"private": True, "name": "dummy"}))
    runner = tmp_path / "cli-pkg"
    runner.mkdir()
    (runner / "package.json").write_text(
        json.dumps({"bin": {"my-cli": "lib/index.js"}})
    )
    (runner / "lib").mkdir()
    (runner / "lib" / "index.js").write_text("")
    stack = StackInfo(language="Node.js", framework="Node.js", project_type="backend", runtime="node")
    cmds = discover(tmp_path, stack)
    assert cmds.install == "npm install --prefix cli-pkg"
    assert cmds.run == "node cli-pkg/lib/index.js"


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


# ── README heuristic unit tests ───────────────────────────────────────────────

def test_split_readme_sections_basic() -> None:
    readme = "## Installation\n```bash\nnpm install\n```\n## Usage\n```bash\nnpm start\n```\n"
    sections = _split_readme_sections(readme)
    headings = [h for h, _ in sections]
    assert "Installation" in headings
    assert "Usage" in headings


def test_split_readme_sections_no_headings() -> None:
    readme = "Just plain text with ```bash\nnpm install\n``` code."
    sections = _split_readme_sections(readme)
    assert len(sections) == 1
    assert sections[0][0] == ""


def test_split_readme_sections_ignores_headings_inside_fences() -> None:
    readme = (
        "```text\n"
        "## This is not a real section\n"
        "npm install\n"
        "```\n\n"
        "## Usage\n\n"
        "```bash\nnpm start\n```\n"
    )
    sections = _split_readme_sections(readme)
    titles = [h for h, _ in sections]
    assert "## This is not a real section" not in "".join(titles)
    assert "Usage" in titles


def test_split_readme_sections_link_in_heading() -> None:
    readme = "## [Installation](https://example.com)\n\n```bash\npip install -e .\n```\n"
    sections = _split_readme_sections(readme)
    assert any(h == "Installation" for h, _ in sections)


def test_extract_readme_commands_npm() -> None:
    text = "```bash\nnpm install\nnpm start\n```"
    install, run = _extract_readme_commands(text)
    assert install == "npm install"
    assert run == "npm start"


def test_extract_readme_commands_plain_fence_no_language() -> None:
    text = "```\nnpm install\nnpm start\n```"
    install, run = _extract_readme_commands(text)
    assert install == "npm install"
    assert run == "npm start"


def test_extract_readme_commands_crlf_fence() -> None:
    text = "```bash\r\nnpm install\r\n```"
    install, run = _extract_readme_commands(text)
    assert install == "npm install"
    assert run is None


def test_extract_readme_commands_tilde_fence() -> None:
    text = "~~~bash\nnpm install\nnpm run dev\n~~~\n"
    install, run = _extract_readme_commands(text)
    assert install == "npm install"
    assert run == "npm run dev"


def test_extract_readme_commands_double_ampersand_line() -> None:
    text = "```bash\nnpm install && npm run build\n```"
    install, run = _extract_readme_commands(text)
    assert install == "npm install"
    assert run == "npm run build"


def test_extract_readme_commands_backslash_continuation() -> None:
    text = "```bash\nnpm install \\\n  --legacy-peer-deps\nnpm start\n```"
    install, run = _extract_readme_commands(text)
    assert install == "npm install --legacy-peer-deps"
    assert run == "npm start"


def test_extract_readme_commands_json_fence_skipped() -> None:
    text = '```json\n{"scripts": {"start": "node app.js"}}\n```\n```bash\nnpm start\n```\n'
    install, run = _extract_readme_commands(text)
    assert install is None
    assert run == "npm start"


def test_split_readme_sections_ignores_headings_in_tilde_fence() -> None:
    readme = "~~~\n## Not a section\nnpm ci\n~~~\n\n## Run\n\n```bash\nnpm start\n```\n"
    sections = _split_readme_sections(readme)
    titles = [h for h, _ in sections]
    assert "Not a section" not in titles
    assert "Run" in titles


def test_extract_readme_commands_dollar_prefix() -> None:
    text = "```sh\n$ pip install -r requirements.txt\n$ python app.py\n```"
    install, run = _extract_readme_commands(text)
    assert install == "pip install -r requirements.txt"
    assert run == "python app.py"


def test_extract_readme_commands_skips_ci_lines() -> None:
    text = "```yaml\n- uses: actions/checkout@v4\n  run: npm install\n```"
    install, run = _extract_readme_commands(text)
    # 'uses:' line is skipped; 'run:' line is also skipped
    assert install is None
    assert run is None


def test_extract_readme_commands_skips_placeholders() -> None:
    text = "```bash\nnpm install <YOUR_PACKAGE>\nyour_api_key=xxx node server.js\n```"
    install, run = _extract_readme_commands(text)
    assert install is None
    assert run is None


def test_from_readme_heuristic_installation_section(tmp_path: Path) -> None:
    readme = (
        "# My App\n\n"
        "## Installation\n\n"
        "```bash\npip install -r requirements.txt\n```\n\n"
        "## Usage\n\n"
        "```bash\npython app.py\n```\n"
    )
    (tmp_path / "README.md").write_text(readme)
    cmds = _from_readme_heuristic(tmp_path)
    assert cmds is not None
    assert cmds.source == "readme_heuristic"
    assert cmds.install == "pip install -r requirements.txt"
    assert cmds.run == "python app.py"


def test_from_readme_heuristic_quick_start_section(tmp_path: Path) -> None:
    readme = (
        "# MegaLinter-style\n\n"
        "## Quick Start\n\n"
        "```bash\nnpx mega-linter-runner --install\n```\n\n"
        "## Run Locally\n\n"
        "```bash\nnpx mega-linter-runner\n```\n"
    )
    (tmp_path / "README.md").write_text(readme)
    cmds = _from_readme_heuristic(tmp_path)
    assert cmds is not None
    assert "npx mega-linter-runner --install" == cmds.install
    assert "npx mega-linter-runner" == cmds.run


def test_from_readme_heuristic_no_code_blocks_returns_none(tmp_path: Path) -> None:
    readme = "# App\n\nInstall with pip. Run with python.\n"
    (tmp_path / "README.md").write_text(readme)
    cmds = _from_readme_heuristic(tmp_path)
    assert cmds is None


def test_from_readme_heuristic_no_readme_returns_none(tmp_path: Path) -> None:
    cmds = _from_readme_heuristic(tmp_path)
    assert cmds is None


def test_from_readme_heuristic_finds_lowercase_readme_md(tmp_path: Path) -> None:
    readme = "## Setup\n\n```\npnpm install\n```\n\n## Run\n\n```bash\npnpm start\n```\n"
    (tmp_path / "readme.md").write_text(readme, encoding="utf-8")
    cmds = _from_readme_heuristic(tmp_path)
    assert cmds is not None
    assert cmds.install == "pnpm install"
    assert cmds.run == "pnpm start"


def test_from_readme_heuristic_docker_commands(tmp_path: Path) -> None:
    readme = (
        "## Getting Started\n\n"
        "```bash\ndocker build -t myapp .\ndocker run -p 8080:8080 myapp\n```\n"
    )
    (tmp_path / "README.md").write_text(readme)
    cmds = _from_readme_heuristic(tmp_path)
    assert cmds is not None
    assert "docker build" in cmds.install
    assert "docker run" in cmds.run


# ── README-first integration tests ───────────────────────────────────────────

def test_readme_beats_makefile_for_install_and_run(tmp_path: Path) -> None:
    """README commands take priority over Makefile when both are present."""
    (tmp_path / "Makefile").write_text(
        "bootstrap:\n\tpython3.12 -m venv .venv && pip install -r requirements.txt\n\n"
        "start:\n\tnode index.js\n"
    )
    readme = (
        "## Installation\n\n```bash\nnpm install\n```\n\n"
        "## Usage\n\n```bash\nnpm start\n```\n"
    )
    (tmp_path / "README.md").write_text(readme)
    (tmp_path / "package.json").write_text(json.dumps({"name": "app"}))
    stack = _node_stack()
    cmds = discover(tmp_path, stack)
    assert cmds.source == "readme_heuristic"
    assert cmds.install == "npm install"
    assert cmds.run == "npm start"


def test_readme_run_wins_makefile_install_fills_gap(tmp_path: Path) -> None:
    """README has only a run command; install gap is filled from Makefile."""
    (tmp_path / "Makefile").write_text("install:\n\tpip install -r requirements.txt\n")
    readme = "## Running\n\n```bash\npython app.py\n```\n"
    (tmp_path / "README.md").write_text(readme)
    stack = _python_stack()
    cmds = discover(tmp_path, stack)
    assert cmds.run == "python app.py"
    assert cmds.install is not None
    assert "make install" in cmds.install


def test_readme_install_fills_gap_when_only_run_in_makefile(tmp_path: Path) -> None:
    """README provides install; Makefile provides run."""
    (tmp_path / "Makefile").write_text("run:\n\tnode server.js\n")
    readme = "## Setup\n\n```bash\nnpm install\n```\n"
    (tmp_path / "README.md").write_text(readme)
    stack = _node_stack()
    cmds = discover(tmp_path, stack)
    assert cmds.install == "npm install"
    assert "make run" in cmds.run


def test_subpackage_bin_install_beats_readme(tmp_path: Path) -> None:
    """Even when README says 'npm install', subpackage-bin's prefixed install wins."""
    readme = (
        "## Installation\n\n```bash\nnpm install\n```\n\n"
        "## Usage\n\n```bash\nnpx my-cli\n```\n"
    )
    (tmp_path / "README.md").write_text(readme)
    (tmp_path / "package.json").write_text(json.dumps({"private": True, "name": "root"}))
    cli = tmp_path / "my-cli"
    cli.mkdir()
    (cli / "package.json").write_text(json.dumps({"bin": {"my-cli": "lib/index.js"}}))
    (cli / "lib").mkdir()
    (cli / "lib" / "index.js").write_text("// cli\n")
    stack = _node_stack()
    cmds = discover(tmp_path, stack)
    # subpackage-bin install should override README's generic npm install
    assert cmds.install == "npm install --prefix my-cli"
    # README's npx run should be used
    assert cmds.run == "npx my-cli"


def test_readme_ignored_when_empty_of_commands(tmp_path: Path) -> None:
    """README with no code blocks should fall through to Makefile."""
    (tmp_path / "Makefile").write_text("install:\n\tnpm ci\n\nrun:\n\tnode server.js\n")
    (tmp_path / "README.md").write_text("# My App\n\nSee the docs for details.\n")
    stack = _node_stack()
    cmds = discover(tmp_path, stack)
    assert cmds.source == "Makefile"
    assert "make install" in cmds.install
    assert "make run" in cmds.run


def test_readme_npx_megalinter_style(tmp_path: Path) -> None:
    """Validates the real MegaLinter README pattern: npx install + npx run."""
    (tmp_path / "Makefile").write_text(
        "bootstrap:\n\tpython3.12 -m venv .venv\n\tpip install -r requirements.txt\n"
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"private": True, "workspaces": ["mega-linter-runner"]})
    )
    readme = (
        "# MegaLinter\n\n"
        "## Quick Start\n\n"
        "```console\nnpx mega-linter-runner --install\n```\n\n"
        "## Run MegaLinter Locally\n\n"
        "```bash\nnpx mega-linter-runner\n```\n"
    )
    (tmp_path / "README.md").write_text(readme)
    stack = _node_stack()
    cmds = discover(tmp_path, stack)
    # README should win over Makefile's python3.12-requiring bootstrap
    assert cmds.source == "readme_heuristic"
    assert "npx mega-linter-runner" in (cmds.install or "")
    assert "npx mega-linter-runner" in (cmds.run or "")
