"""Command discovery — find install, build, and run commands for a repo."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from repofix.detection.readme_util import read_readme_text
from repofix.detection.stack import StackInfo


@dataclass
class CommandSet:
    install: str | None = None
    build: str | None = None
    run: str | None = None
    source: str = "defaults"  # "package.json" | "makefile" | "procfile" | "docker" | "defaults" | "readme_ai"

    def has_all(self) -> bool:
        return bool(self.install and self.run)

    def as_display_dict(self) -> dict[str, str]:
        d: dict[str, str] = {}
        if self.install:
            d["Install"] = self.install
        if self.build:
            d["Build"] = self.build
        if self.run:
            d["Run"] = self.run
        d["Discovered via"] = self.source
        return d


def discover(
    repo_path: Path,
    stack: StackInfo,
    override_install: str | None = None,
    override_run: str | None = None,
    readme_ai_fallback: Callable[[str], CommandSet] | None = None,
) -> CommandSet:
    """
    Discover commands using a priority order that puts explicit documentation first:

      1. CLI overrides      — always wins, applied last as a final replacement
      2. README heuristic   — rule-based scan of Install/Setup/QuickStart/Run sections
      3. Makefile           — explicit developer scripts; checked for ALL stacks
      4. Docker             — if stack IS docker, use compose/dockerfile commands next
      5. Procfile / uv.lock / package.json / subpackage-bin / stack-specific / defaults
      6. README AI fallback — last resort when no heuristic produced a run command

    Merge semantics: each source fills in fields that higher-priority sources left
    as None.  Two exceptions override this for install commands (since they know the
    precise install location):

    * ``subpackage-bin`` install (e.g. ``npm install --prefix app/``) beats both the
      README's generic ``npm install`` and the Makefile's ``make bootstrap``.
    * ``uv.lock`` install (``uv sync``) beats the Makefile's install target and any
      generic ``pip install`` the README might mention.
    """
    # 1. Hard CLI overrides — short-circuit only when BOTH are specified
    if override_install and override_run:
        return CommandSet(install=override_install, run=override_run, source="cli-override")

    # 2. README heuristic — highest priority: explicit user-facing documentation
    readme_cmds = _from_readme_heuristic(repo_path)

    # 3. Makefile — explicit developer scripts
    mk = _from_makefile(repo_path)

    # 4. Stack-specific source for install/build/run
    if stack.is_docker():
        stack_cmds = _from_docker(repo_path, stack)
    else:
        stack_cmds = (
            _from_procfile(repo_path)
            or _from_uv_project(repo_path, stack)
            or _from_node_workspaces(repo_path, stack)
            or _from_package_json(repo_path, stack)
            or _from_node_subpackage_bin(repo_path, stack)
            or _from_python_packaging(repo_path, stack)
            or _from_java_build_tool(repo_path, stack)
            or _from_go_project(repo_path, stack)
            or _from_rust_workspace(repo_path, stack)
            or _from_stack_defaults(stack)
        )

    # subpackage-bin install knows the exact --prefix; beats README + Makefile
    if stack_cmds and stack_cmds.source == "subpackage-bin":
        if mk:
            mk.install = None
        if readme_cmds:
            readme_cmds.install = None

    # uv.lock install (uv sync) is more reliable than any generic pip/make target
    if stack_cmds and stack_cmds.source == "uv.lock":
        if mk:
            mk.install = None
        if readme_cmds:
            readme_cmds.install = None

    # Merge chain: README > Makefile > stack_cmds (each fills gaps left by higher sources)
    cmds = _merge(readme_cmds, _merge(mk, stack_cmds))

    if cmds:
        if override_install:
            cmds.install = override_install
        if override_run:
            cmds.run = override_run
        return cmds

    # 6. README AI fallback — only reached when no heuristic produced ANY commands
    if readme_ai_fallback:
        readme = _read_readme(repo_path)
        if readme:
            from repofix.output import display
            display.ai_action("Commands not found — extracting from README with AI…")
            try:
                ai_cmds = readme_ai_fallback(readme)
                ai_cmds.source = "readme_ai"
                if override_install:
                    ai_cmds.install = override_install
                if override_run:
                    ai_cmds.run = override_run
                return ai_cmds
            except Exception as exc:
                from repofix.output import display as d
                d.warning(f"AI command extraction failed: {exc}")

    return CommandSet(source="unknown")


def _merge(primary: CommandSet | None, fallback: CommandSet | None) -> CommandSet | None:
    """
    Combine two CommandSets: fields from `primary` win; missing fields are
    filled from `fallback`. Returns None only if both are None.
    """
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return CommandSet(
        install=primary.install or fallback.install,
        build=primary.build or fallback.build,
        run=primary.run or fallback.run,
        source=primary.source,  # credit the higher-priority source
    )


# ── package.json ──────────────────────────────────────────────────────────────

_PREFERRED_RUN_SCRIPTS = ["dev", "start", "serve", "preview"]
_PREFERRED_BUILD_SCRIPTS = ["build", "compile", "bundle"]

# Matches npm script names that are wrappers around a Java build tool
_JAVA_BUILD_SCRIPT_RE = re.compile(
    r"(?:build|compile|package|assemble)[\w-]*java"
    r"|java[\w-]*(?:build|compile|package|assemble)"
    r"|mvn|gradle|build-java|java-build",
    re.I,
)

# Matches npm script names that start a Java server
_JAVA_RUN_SCRIPT_RE = re.compile(
    r"(?:start|run|serve|launch)[\w-]*java|java[\w-]*(?:start|run|serve)",
    re.I,
)

# ── Agent plugin / skills-framework detection ─────────────────────────────────

# Hidden directories that signal an AI agent plugin repo (not a runnable service)
_AGENT_PLUGIN_DIRS: frozenset[str] = frozenset({
    ".claude-plugin", ".cursor-plugin", ".opencode", ".codex",
    ".gemini", ".aider",
})

# Scripts that indicate a real runnable web/CLI app
_RUNNABLE_SCRIPTS: frozenset[str] = frozenset({
    "start", "dev", "serve", "preview", "server",
})


def _is_plugin_path(path_str: str) -> bool:
    """Return True if the path lives inside a known agent plugin directory."""
    parts = Path(path_str).parts
    return bool(parts and parts[0] in _AGENT_PLUGIN_DIRS)


def detect_non_runnable(path: Path) -> dict | None:
    """
    Detect repos that are not meant to be run as services.

    Currently identifies:
    - **agent_plugin**: repos whose primary purpose is to be installed into an
      AI coding agent (e.g. superpowers, claude-plugin repos).  Indicated by the
      presence of agent plugin directories *and* the absence of any genuine run
      script in ``package.json``.

    Returns a dict with at least ``{"type": str, ...}`` or ``None`` when the
    repo appears to be a normal runnable project.
    """
    plugin_dirs = sorted(d for d in _AGENT_PLUGIN_DIRS if (path / d).is_dir())
    if not plugin_dirs:
        return None

    # If package.json defines a real run script, this is a runnable app that
    # *also* happens to ship a plugin — don't block it.
    pkg_file = path / "package.json"
    if pkg_file.exists():
        try:
            data: dict = json.loads(pkg_file.read_text())
            scripts: dict = data.get("scripts", {})
            if any(k in scripts for k in _RUNNABLE_SCRIPTS):
                return None
        except Exception:
            pass

    platforms = [d.lstrip(".").replace("-plugin", "") for d in plugin_dirs]
    return {"type": "agent_plugin", "platforms": sorted(platforms)}


_FAT_JAR_SUFFIXES = (
    "-jar-with-dependencies.jar",
    "-fat.jar", "-uber.jar", "-shaded.jar",
    "-all.jar", "-standalone.jar", "-runnable.jar",
)
_SKIP_JAR_SUFFIXES = (
    "-sources.jar", "-javadoc.jar",
    "-tests.jar", "-test.jar", "-original.jar",
)


# Directories to exclude from recursive JAR / pom.xml searches
_SEARCH_EXCLUDE_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", ".gradle", ".mvn", ".idea",
    "__pycache__", ".venv", "venv",
})


def _is_excluded(jar: Path, repo_root: Path) -> bool:
    """True if any path component is in the exclusion list."""
    try:
        parts = jar.relative_to(repo_root).parts
    except ValueError:
        return False
    return any(p in _SEARCH_EXCLUDE_DIRS for p in parts)


def find_best_jar(path: Path) -> Path | None:
    """Return the best runnable JAR produced by a Maven or Gradle build, or None.

    Searches recursively so multi-module projects (e.g. ``java/module/target/``)
    are found even when the ``pom.xml`` is not at the repository root.
    """
    candidates: list[Path] = []
    for pattern in ("**/target/*.jar", "**/build/libs/*.jar"):
        for jar in path.glob(pattern):
            if _is_excluded(jar, path):
                continue
            if any(jar.name.lower().endswith(s) for s in _SKIP_JAR_SUFFIXES):
                continue
            candidates.append(jar)

    if not candidates:
        return None

    # Prefer explicitly-named fat/uber JARs (contain all dependencies)
    for jar in candidates:
        if any(jar.name.lower().endswith(s) for s in _FAT_JAR_SUFFIXES):
            return jar

    # Fall back to the largest JAR (most likely to be the bundled one)
    return max(candidates, key=lambda j: j.stat().st_size)


def has_java_build_files(path: Path) -> bool:
    """Return True if the repo contains Maven or Gradle build files (any depth)."""
    if (path / "pom.xml").exists() or (path / "build.gradle").exists() or (path / "build.gradle.kts").exists():
        return True
    # Check one level of subdirectories (covers java/, backend/, server/, etc.)
    for child in path.iterdir():
        if not child.is_dir() or child.name in _SEARCH_EXCLUDE_DIRS:
            continue
        if (child / "pom.xml").exists() or (child / "build.gradle").exists():
            return True
    return False


_NODE_SUBPKG_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", ".gradle", ".mvn", ".idea",
    "__pycache__", ".venv", "venv", "dist", "build", ".next",
})


def _node_subpackage_bin_info(path: Path) -> tuple[str, str] | None:
    """Return ``(node_cmd, subdir)`` for a direct child package with a ``bin`` entry.

    *subdir* is the relative path to that package (one segment). Install commands
    should run in that directory so ``node_modules`` matches the CLI (MegaLinter,
    etc.).
    """
    for child in sorted(path.iterdir()):
        if not child.is_dir() or child.name in _NODE_SUBPKG_SKIP_DIRS:
            continue
        if child.name.startswith("."):
            continue
        pkg_file = child / "package.json"
        if not pkg_file.exists():
            continue
        try:
            data: dict = json.loads(pkg_file.read_text())
        except Exception:
            continue
        bin_field = data.get("bin")
        if not bin_field:
            continue
        if isinstance(bin_field, str):
            entry = bin_field
        else:
            entry = next(iter(bin_field.values()))
        rel_script = child / entry
        if rel_script.is_file():
            try:
                rel = rel_script.relative_to(path)
            except ValueError:
                continue
            return (f"node {rel}", child.name)
    return None


def _node_subpackage_bin_run(path: Path) -> str | None:
    """Return ``node <rel/path>`` for a direct child package with a ``bin`` entry."""
    info = _node_subpackage_bin_info(path)
    return info[0] if info else None


def _from_node_subpackage_bin(path: Path, stack: StackInfo) -> CommandSet | None:
    """When the root ``package.json`` has no runnable scripts/bin, use a subpackage CLI."""
    if stack.runtime.lower() != "node":
        return None
    info = _node_subpackage_bin_info(path)
    if not info:
        return None
    _run_cmd, subdir = info
    return CommandSet(
        install=_install_into_subpackage(path, subdir),
        run=_run_cmd,
        source="subpackage-bin",
    )


def find_node_entry(path: Path) -> str | None:
    """Return the best ``node <file>`` command for a Node.js repo whose default
    entry file (index.js) was not found.

    Search order:
      1. ``package.json`` → ``main`` field (if the file exists on disk)
      2. Direct child ``package.json`` → ``bin`` field (monorepo CLI packages)
      3. Common entry-file candidates, checked in priority order
      4. TypeScript variants (``ts-node`` / ``npx ts-node``)
    """
    # 1. package.json main field — skip plugin paths (e.g. .opencode/plugins/...)
    pkg_file = path / "package.json"
    if pkg_file.exists():
        try:
            data: dict = json.loads(pkg_file.read_text())
            main = data.get("main")
            if main and not _is_plugin_path(main) and (path / main).exists():
                return f"node {main}"
        except Exception:
            pass

    sub_bin = _node_subpackage_bin_run(path)
    if sub_bin:
        return sub_bin

    # 2. Common JS entry-file candidates (src/index.js before root index.js for bin-style packages)
    _JS_CANDIDATES = [
        "src/index.js", "src/app.js", "src/server.js", "src/main.js",
        "app.js", "server.js", "index.js",
        "dist/index.js", "dist/app.js", "dist/server.js",
        "build/index.js", "build/app.js",
        "lib/index.js",
    ]
    for candidate in _JS_CANDIDATES:
        if (path / candidate).exists():
            return f"node {candidate}"

    # 3. TypeScript entry candidates — prefer tsx / ts-node from local node_modules
    _TS_CANDIDATES = [
        "src/index.ts", "src/app.ts", "src/server.ts", "src/main.ts",
        "index.ts", "app.ts", "server.ts",
    ]
    ts_runner = None
    if (path / "node_modules" / ".bin" / "tsx").exists():
        ts_runner = "npx tsx"
    elif (path / "node_modules" / ".bin" / "ts-node").exists():
        ts_runner = "npx ts-node"

    if ts_runner:
        for candidate in _TS_CANDIDATES:
            if (path / candidate).exists():
                return f"{ts_runner} {candidate}"

    return None


def jar_run_cmd(path: Path) -> str:
    """Return the best ``java -jar`` command for this repository.

    Uses the actual JAR path when already built; falls back to a glob pattern
    when called before the build (so the command still works post-build).
    """
    best = find_best_jar(path)
    if best:
        try:
            return f"java -jar {best.relative_to(path)}"
        except ValueError:
            return f"java -jar {best}"
    return "java -jar target/*.jar" if (path / "pom.xml").exists() else "java -jar build/libs/*.jar"


def _from_package_json(path: Path, stack: StackInfo) -> CommandSet | None:
    pkg_file = path / "package.json"
    if not pkg_file.exists():
        return None

    try:
        pkg: dict = json.loads(pkg_file.read_text())
    except Exception:
        return None

    scripts: dict[str, str] = dict(pkg.get("scripts") or {})
    bin_field = pkg.get("bin")
    if not scripts and not bin_field:
        return None

    pkg_manager = _detect_package_manager(path)
    run_prefix = f"{pkg_manager} run"
    _has_java_build = has_java_build_files(path)

    run_cmd: str | None = None
    build_cmd: str | None = None

    # Preferred run scripts
    for preferred in _PREFERRED_RUN_SCRIPTS:
        if preferred in scripts:
            run_cmd = f"{run_prefix} {preferred}"
            break

    # Explicit Java server start script (e.g. "start-java", "run-java")
    if not run_cmd and _has_java_build:
        for name in scripts:
            if _JAVA_RUN_SCRIPT_RE.search(name):
                run_cmd = f"{run_prefix} {name}"
                break

    # Preferred build scripts
    for preferred in _PREFERRED_BUILD_SCRIPTS:
        if preferred in scripts:
            build_cmd = f"{run_prefix} {preferred}"
            break

    if not run_cmd and scripts:
        first = next(iter(scripts))
        # If the first script is a Java build wrapper (e.g. "build-java") and this
        # repo has a pom.xml / build.gradle, treat it as the BUILD step and derive
        # the java -jar run command automatically.
        if _has_java_build and _JAVA_BUILD_SCRIPT_RE.search(first):
            build_cmd = build_cmd or f"{run_prefix} {first}"
            run_cmd = jar_run_cmd(path)
        else:
            run_cmd = f"{run_prefix} {first}"

    # Tooling packages: only a "bin" entry (e.g. MCP servers), no scripts
    if not run_cmd and bin_field:
        if isinstance(bin_field, str):
            entry = bin_field
        else:
            entry = next(iter(bin_field.values()))
        run_cmd = f"node {entry}"

    install_map = {"npm": "npm install", "yarn": "yarn install", "pnpm": "pnpm install", "bun": "bun install"}
    install_cmd = install_map.get(pkg_manager, "npm install")

    return CommandSet(install=install_cmd, build=build_cmd, run=run_cmd, source="package.json")


def _detect_package_manager(path: Path) -> str:
    if (path / "bun.lockb").exists():
        return "bun"
    if (path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (path / "yarn.lock").exists():
        return "yarn"
    return "npm"


def is_npm_workspace_root(path: Path) -> bool:
    """True if *path* is an npm/yarn workspace root or a pnpm workspace root."""
    pkg_file = path / "package.json"
    if pkg_file.exists():
        try:
            data: dict = json.loads(pkg_file.read_text())
        except Exception:
            data = {}
        if data.get("workspaces"):
            return True
    return (path / "pnpm-workspace.yaml").exists()


def node_install_command(path: Path) -> str:
    """Standard install line for the package manager used at *path*."""
    pm = _detect_package_manager(path)
    return {
        "npm": "npm install",
        "yarn": "yarn install",
        "pnpm": "pnpm install",
        "bun": "bun install",
    }.get(pm, "npm install")


def _install_into_subpackage(repo_path: Path, subdir: str) -> str:
    """Install dependencies into a direct child package's ``node_modules`` (CLI monorepos)."""
    pm = _detect_package_manager(repo_path)
    if pm == "npm":
        return f"npm install --prefix {subdir}"
    if pm == "yarn":
        return f"yarn --cwd {subdir} install"
    if pm == "pnpm":
        return f"pnpm install --dir {subdir}"
    if pm == "bun":
        return f"bun install --cwd {subdir}"
    return f"npm install --prefix {subdir}"


# ── Node.js workspace / monorepo detection ────────────────────────────────────

# Ordered preference of subpackage directory names to look for
_WS_PREFERRED_DIRS: tuple[str, ...] = (
    "apps/api", "apps/server", "apps/backend",
    "apps/web", "apps/app", "apps/frontend",
    "packages/server", "packages/api", "packages/app",
    "server", "api", "backend", "app", "web",
)

_WS_RUN_SCRIPTS: tuple[str, ...] = ("dev", "start", "serve", "preview")


def _from_node_workspaces(path: Path, stack: StackInfo) -> CommandSet | None:
    """Detect Node.js monorepos and find the best subpackage to run.

    Only fires when the root ``package.json`` declares ``workspaces`` (npm/yarn)
    or a ``pnpm-workspace.yaml`` is present **and** the root itself has no
    usable run script.  Drills into well-known sub-directory patterns to find
    a package that does have a ``dev``/``start`` script.
    """
    if stack.runtime.lower() != "node":
        return None

    pkg_file = path / "package.json"
    if not pkg_file.exists():
        return None

    try:
        pkg: dict = json.loads(pkg_file.read_text())
    except Exception:
        return None

    has_workspaces = bool(pkg.get("workspaces"))
    has_pnpm_ws = (path / "pnpm-workspace.yaml").exists()
    if not (has_workspaces or has_pnpm_ws):
        return None

    # If root already has a usable run script, let _from_package_json handle it
    if any(s in pkg.get("scripts", {}) for s in _PREFERRED_RUN_SCRIPTS):
        return None

    pkg_manager = _detect_package_manager(path)
    install_map = {
        "npm": "npm install", "yarn": "yarn install",
        "pnpm": "pnpm install", "bun": "bun install",
    }
    install_cmd = install_map.get(pkg_manager, "npm install")
    run_prefix = f"{pkg_manager} run"

    for rel_dir in _WS_PREFERRED_DIRS:
        ws_pkg_file = path / rel_dir / "package.json"
        if not ws_pkg_file.exists():
            continue
        try:
            ws_pkg: dict = json.loads(ws_pkg_file.read_text())
        except Exception:
            continue
        scripts: dict = ws_pkg.get("scripts", {})
        for script in _WS_RUN_SCRIPTS:
            if script in scripts:
                return CommandSet(
                    install=install_cmd,
                    run=f"cd {rel_dir} && {run_prefix} {script}",
                    source="workspaces",
                )

    return None


# ── Makefile ──────────────────────────────────────────────────────────────────

# Ordered by preference — first match wins within each category
_MAKE_RUN_TARGETS = [
    "dev", "run", "start", "serve", "up",
    "dev-server", "start-dev", "run-dev", "watch",
    "local", "launch",
]
_MAKE_INSTALL_TARGETS = [
    "install", "setup", "deps", "bootstrap",
    "install-deps", "setup-dev", "init",
]
_MAKE_BUILD_TARGETS = [
    "build", "compile", "bundle", "dist",
    "build-prod", "build-all",
]


def _from_makefile(path: Path) -> CommandSet | None:
    # Support Makefile, GNUmakefile, makefile
    mk_file = None
    for name in ("Makefile", "GNUmakefile", "makefile"):
        candidate = path / name
        if candidate.exists():
            mk_file = candidate
            break
    if mk_file is None:
        return None

    try:
        content = mk_file.read_text()
    except Exception:
        return None

    targets = set(re.findall(r"^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:", content, re.MULTILINE))

    run_cmd = next((f"make {t}" for t in _MAKE_RUN_TARGETS if t in targets), None)
    install_cmd = next((f"make {t}" for t in _MAKE_INSTALL_TARGETS if t in targets), None)
    build_cmd = next((f"make {t}" for t in _MAKE_BUILD_TARGETS if t in targets), None)

    # Only return if the Makefile actually has something useful
    if run_cmd or install_cmd:
        return CommandSet(install=install_cmd, build=build_cmd, run=run_cmd, source="Makefile")
    return None


# ── 4. Procfile ───────────────────────────────────────────────────────────────

def _from_procfile(path: Path) -> CommandSet | None:
    procfile = path / "Procfile"
    if not procfile.exists():
        return None

    try:
        for line in procfile.read_text().splitlines():
            if line.startswith("web:"):
                run_cmd = line.split(":", 1)[1].strip()
                return CommandSet(run=run_cmd, source="Procfile")
    except Exception:
        pass
    return None


# ── 5. Docker ─────────────────────────────────────────────────────────────────

def _from_docker(path: Path, stack: StackInfo) -> CommandSet | None:
    if not stack.is_docker():
        return None

    if stack.extras.get("mode") == "compose":
        return CommandSet(
            install=None,
            build="docker compose build",
            run="docker compose up",
            source="docker-compose.yml",
        )
    # Standalone Dockerfile
    image_name = path.name.lower().replace(" ", "-")
    return CommandSet(
        install=None,
        build=f"docker build -t {image_name} .",
        run=f"docker run -p 8080:8080 {image_name}",
        source="Dockerfile",
    )


# ── 5b. uv-managed Python projects (uv.lock) ─────────────────────────────────

def _from_uv_project(path: Path, stack: StackInfo) -> CommandSet | None:
    """
    Detect projects managed by uv (uv.lock present).

    ``uv sync`` is more reliable than ``uv pip install -e .`` or ``make install``
    for these repos: it installs from the lockfile, respects extras, and honours
    the ``requires-python`` constraint without creating an incompatible venv.

    Special sub-detectors:
    - ``langgraph.json``  →  ``uv run langgraph dev --no-browser``
    """
    if not (path / "uv.lock").exists():
        return None
    if stack.language.lower() not in ("python", "unknown"):
        return None

    # Determine whether the project declares optional-dependency groups / extras
    install_cmd = "uv sync"
    if (path / "pyproject.toml").exists():
        try:
            content = (path / "pyproject.toml").read_text()
            if "[project.optional-dependencies]" in content or "[tool.uv.sources]" in content:
                install_cmd = "uv sync --all-extras"
        except Exception:
            pass

    # LangGraph projects expose a standard dev-server entry point
    if (path / "langgraph.json").exists():
        run_cmd: str | None = "uv run langgraph dev --no-browser"
    else:
        entry = _run_from_pyproject(path)
        run_cmd = f"uv run {entry}" if entry else None

    # Library repos have no entry point — fall back to a meaningful command
    if not run_cmd:
        lib_cmd = _run_cmd_for_library(path)
        run_cmd = f"uv run {lib_cmd}" if not lib_cmd.startswith("python") else lib_cmd

    return CommandSet(install=install_cmd, run=run_cmd, source="uv.lock")


# ── 5c. Python packaging files ────────────────────────────────────────────────

def _from_python_packaging(path: Path, stack: StackInfo) -> CommandSet | None:
    """
    Detect pyproject.toml / setup.py / setup.cfg and use ``pip install -e .``
    instead of the generic ``pip install -r requirements.txt`` default.
    """
    if stack.language.lower() != "python":
        return None

    has_pyproject = (path / "pyproject.toml").exists()
    has_setup_py  = (path / "setup.py").exists()
    has_setup_cfg = (path / "setup.cfg").exists()

    if not (has_pyproject or has_setup_py or has_setup_cfg):
        return None

    source  = "pyproject.toml" if has_pyproject else ("setup.py" if has_setup_py else "setup.cfg")
    run_cmd = _run_from_pyproject(path) if has_pyproject else None

    # For library repos with no declared entry point, fall back to a meaningful command
    # rather than leaving run=None and aborting at the runner level.
    if not run_cmd:
        run_cmd = _run_cmd_for_library(path)

    return CommandSet(install="pip install -e .", run=run_cmd, source=source)


def _run_from_pyproject(path: Path) -> str | None:
    """Extract the first CLI entry-point name from ``[project.scripts]`` or
    ``[tool.poetry.scripts]`` in pyproject.toml, if present."""
    try:
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        data: dict = tomllib.loads((path / "pyproject.toml").read_text())
    except Exception:
        return None

    scripts = data.get("project", {}).get("scripts", {})
    if not scripts:
        scripts = data.get("tool", {}).get("poetry", {}).get("scripts", {})
    if scripts:
        return next(iter(scripts))
    return None


def _run_cmd_for_library(path: Path) -> str:
    """
    Pick a meaningful run command for a Python library that has no declared
    entry point.

    Priority:
      1. ``pytest`` / ``py.test`` — if a tests/ directory or test_*.py files exist.
         This exercises the library and validates it works, which is the most
         useful thing you can do with an installed library.
      2. ``python -m <package>`` — if a package directory with ``__main__.py`` exists.
      3. ``python -c "import <package>"`` — quick import check using the first
         top-level Python package (directory containing ``__init__.py``).
      4. ``python`` — interactive REPL as a last resort.
    """
    # 1. Test runner
    if (path / "tests").is_dir() or (path / "test").is_dir() or any(path.glob("test_*.py")):
        return "pytest"

    # 2. Package with __main__.py
    for pkg_dir in sorted(path.iterdir()):
        if (
            pkg_dir.is_dir()
            and not pkg_dir.name.startswith((".", "_"))
            and (pkg_dir / "__main__.py").exists()
        ):
            return f"python -m {pkg_dir.name}"

    # 3. Quick import check for the first top-level package
    for pkg_dir in sorted(path.iterdir()):
        if (
            pkg_dir.is_dir()
            and not pkg_dir.name.startswith((".", "_"))
            and (pkg_dir / "__init__.py").exists()
        ):
            return f'python -c "import {pkg_dir.name}; print({pkg_dir.name}.__name__, \'installed OK\')"'

    # 4. REPL fallback
    return "python"


# ── 6a. Java / Kotlin build-tool specific defaults ────────────────────────────

# Regex for signals that a Gradle subproject is the runnable application
_GRADLE_APP_SIGNAL_RE = re.compile(
    r'\bapplication\b'           # application plugin
    r'|mainClass\s*[.=]'         # mainClass property assignment
    r'|mainClass\.set\s*\('      # mainClass.set(...)
    r'|shadowJar\b'              # Shadow fat-jar plugin task
    r'|\binstallDist\b'          # distribution task
    r'|\bstartScripts\b',        # start script generation
    re.I,
)

# Ordered names that suggest a subproject is the main CLI/server entry point
_JAVA_PREFERRED_MODULE_NAMES: tuple[str, ...] = (
    "cli", "app", "server", "api", "service", "main", "web", "backend",
)


def _find_gradle_app_module(path: Path) -> str | None:
    """Return the Gradle subproject name that defines the runnable entry point.

    Reads ``settings.gradle(.kts)`` to enumerate included subprojects, then
    checks each subproject's build file for the ``application`` or ``shadow``
    plugin, a ``mainClass`` declaration, or distribution-related tasks.

    Returns the bare subproject name (e.g. ``"maestro-cli"``) or ``None`` if
    this is a single-project build or no application subproject is found.
    """
    settings_file = next(
        (path / n for n in ("settings.gradle.kts", "settings.gradle") if (path / n).exists()),
        None,
    )
    if settings_file is None:
        return None

    try:
        settings_text = settings_file.read_text()
    except Exception:
        return None

    raw_modules = re.findall(r'\binclude\s*\(\s*["\']([^"\']+)["\']\s*\)', settings_text)
    if not raw_modules:
        return None  # single-project build

    # De-duplicate, strip leading ":", preserve order
    seen: set[str] = set()
    modules: list[str] = []
    for m in raw_modules:
        key = m.lstrip(":")
        if key not in seen:
            seen.add(key)
            modules.append(key)

    app_modules: list[str] = []
    for module in modules:
        # "maestro-studio:server" → maestro-studio/server on disk
        mod_dir = path / module.replace(":", "/")
        if not mod_dir.is_dir():
            continue
        for build_name in ("build.gradle.kts", "build.gradle"):
            build_file = mod_dir / build_name
            if not build_file.exists():
                continue
            try:
                if _GRADLE_APP_SIGNAL_RE.search(build_file.read_text()):
                    app_modules.append(module)
            except Exception:
                pass
            break  # only read one build file per subproject

    if not app_modules:
        return None

    for pref in _JAVA_PREFERRED_MODULE_NAMES:
        for m in app_modules:
            if pref in m.lower():
                return m
    return app_modules[0]


def _find_maven_app_module(path: Path) -> str | None:
    """Return the Maven submodule that contains the main application.

    Parses the root ``pom.xml`` for a ``<modules>`` section, then checks
    each submodule's ``pom.xml`` for Spring Boot / Quarkus / Micronaut
    Maven plugins or an explicit ``Main-Class`` manifest entry.
    """
    pom = path / "pom.xml"
    if not pom.exists():
        return None

    try:
        import xml.etree.ElementTree as ET
        xml_root = ET.parse(pom).getroot()
        # Handle optional XML namespace (xmlns="http://maven.apache.org/POM/4.0.0")
        ns_match = re.match(r'\{([^}]+)\}', xml_root.tag)
        ns_prefix = f"{{{ns_match.group(1)}}}" if ns_match else ""
        modules_el = xml_root.find(f"{ns_prefix}modules")
        if modules_el is None:
            return None
        modules = [el.text for el in modules_el if el.text]
    except Exception:
        return None

    if not modules:
        return None

    _APP_SIGNALS = (
        "spring-boot-maven-plugin", "quarkus-maven-plugin",
        "micronaut-maven-plugin", "exec-maven-plugin",
        "main-class", "mainclass",
    )
    app_modules: list[str] = []
    for module in modules:
        sub_pom = path / module / "pom.xml"
        if not sub_pom.exists():
            continue
        try:
            if any(sig in sub_pom.read_text().lower() for sig in _APP_SIGNALS):
                app_modules.append(module)
        except Exception:
            continue

    if not app_modules:
        return None

    for pref in _JAVA_PREFERRED_MODULE_NAMES:
        for m in app_modules:
            if pref in m.lower():
                return m
    return app_modules[0]


def _gradle_app_name(mod_dir: Path) -> str | None:
    """Return the ``applicationName`` declared in a Gradle build file, if any.

    Both ``applicationName = "foo"`` (Groovy/Kotlin DSL) and
    ``applicationName.set("foo")`` forms are handled.
    """
    for build_name in ("build.gradle.kts", "build.gradle"):
        bf = mod_dir / build_name
        if not bf.exists():
            continue
        try:
            m = re.search(r'applicationName\s*(?:=|\.set\s*\()\s*["\']([^"\']+)["\']',
                          bf.read_text())
            if m:
                return m.group(1)
        except Exception:
            pass
    return None


def _from_java_build_tool(path: Path, stack: StackInfo) -> CommandSet | None:
    """Return build/run commands for Maven or Gradle projects.

    For multi-module builds the application subproject is auto-detected so
    the build targets only that module (avoiding platform-specific modules
    like iOS/Android that would fail outside their native environment).

    Prefers the project wrapper scripts (``./gradlew``, ``./mvnw``) when
    present — they pin the exact tool version and work without a global install.
    """
    if stack.runtime.lower() != "java":
        return None

    build_tool = stack.extras.get("build_tool", "").lower()

    if build_tool == "gradle":
        gradle_cmd = "./gradlew" if (path / "gradlew").exists() else "gradle"
        app_module = _find_gradle_app_module(path)
        if app_module:
            mod_dir = path / app_module.replace(":", "/")
            has_shadow = False
            has_app_plugin = False
            for build_name in ("build.gradle.kts", "build.gradle"):
                bf = mod_dir / build_name
                if bf.exists():
                    try:
                        bt = bf.read_text()
                        has_shadow = "shadow" in bt.lower()
                        # Match `application` as a bare plugin keyword, not just
                        # any occurrence of the word (e.g. inside a string value)
                        has_app_plugin = bool(re.search(r'^\s*application\b', bt, re.M))
                    except Exception:
                        pass
                    break

            if has_app_plugin:
                # installDist produces a self-contained launch script and avoids
                # fat-JAR glob ambiguity entirely.  Preferred over shadowJar even
                # when the shadow plugin is also present.
                gradle_task = f":{app_module}:installDist"
                # Respect an explicit `applicationName = "..."` override; fall
                # back to the subproject directory name.
                app_name = _gradle_app_name(mod_dir) or app_module.split(":")[-1]
                run_cmd: str = f"{app_module}/build/install/{app_name}/bin/{app_name}"
            elif has_shadow:
                # Shadow plugin without application plugin: fat JAR with -all classifier
                gradle_task = f":{app_module}:shadowJar"
                run_cmd = f"java -jar {app_module}/build/libs/*-all.jar"
            else:
                gradle_task = f":{app_module}:assemble"
                run_cmd = f"java -jar {app_module}/build/libs/*.jar"

            install_cmd: str = f"{gradle_cmd} {gradle_task} -x test"
        else:
            install_cmd = f"{gradle_cmd} build -x test"
            run_cmd = jar_run_cmd(path)

        return CommandSet(
            install=install_cmd,
            build=f"{gradle_cmd} build",
            run=run_cmd,
            source="gradle",
        )

    if build_tool == "maven":
        mvn_cmd = "./mvnw" if (path / "mvnw").exists() else "mvn"
        app_module = _find_maven_app_module(path)
        if app_module:
            install_cmd = f"{mvn_cmd} -pl {app_module} -am package -DskipTests"
            run_cmd = f"java -jar {app_module}/target/*.jar"
        else:
            install_cmd = f"{mvn_cmd} install -DskipTests"
            run_cmd = jar_run_cmd(path)

        return CommandSet(
            install=install_cmd,
            build=f"{mvn_cmd} package -DskipTests",
            run=run_cmd,
            source="maven",
        )

    return None


# ── 6b. Go project entry-point detection ─────────────────────────────────────

_GO_ENTRY_PREFERRED: tuple[str, ...] = (
    "server", "api", "app", "main", "service", "web", "daemon", "cmd",
)


def _from_go_project(path: Path, stack: StackInfo) -> CommandSet | None:
    """Detect Go projects using the ``cmd/`` directory convention.

    Many Go projects follow the ``cmd/<name>/main.go`` layout where there is
    no ``main.go`` at the repo root.  When that pattern is found this function
    returns the correct ``go run ./cmd/<name>`` entry point instead of the
    fallback ``go run .`` from stack defaults (which would fail).

    Also handles projects where the sole main package lives inside an
    ``internal/`` subdirectory.
    """
    if stack.runtime.lower() != "go":
        return None

    # Root-level main.go → "go run ." from _from_stack_defaults is correct
    if (path / "main.go").exists():
        return None

    cmd_dir = path / "cmd"
    if cmd_dir.is_dir():
        subdirs = sorted(
            d for d in cmd_dir.iterdir() if d.is_dir() and (d / "main.go").exists()
        )
        if subdirs:
            chosen = next(
                (d for pref in _GO_ENTRY_PREFERRED for d in subdirs
                 if d.name.lower().startswith(pref)),
                subdirs[0],
            )
            return CommandSet(
                install="go mod download",
                build=f"go build -o ./bin/{chosen.name} ./cmd/{chosen.name}",
                run=f"go run ./cmd/{chosen.name}",
                source="cmd-dir",
            )

    return None


# ── 6c. Rust workspace detection ─────────────────────────────────────────────

_RUST_PREFERRED_NAMES: tuple[str, ...] = (
    "server", "api", "app", "main", "service", "web", "cli", "daemon", "bin",
)


def _from_rust_workspace(path: Path, stack: StackInfo) -> CommandSet | None:
    """Detect Cargo workspace projects and identify the binary member to run.

    Single-crate projects (no ``[workspace]`` section) are handled by the
    ``cargo`` stack defaults (``cargo run``).  This function only activates
    when there is a true multi-crate workspace so it can target the right
    ``-p <package>`` flag.
    """
    if stack.runtime.lower() != "cargo":
        return None

    cargo_toml = path / "Cargo.toml"
    if not cargo_toml.exists():
        return None

    try:
        content = cargo_toml.read_text()
    except Exception:
        return None

    if "[workspace]" not in content:
        return None  # single-crate — let stack defaults handle it

    m = re.search(r'members\s*=\s*\[(.*?)\]', content, re.DOTALL)
    if not m:
        return None
    members = re.findall(r'"([^"]+)"', m.group(1))
    if not members:
        return None

    # Collect members that produce a binary
    bin_members: list[str] = []
    for member in members:
        member_path = path / member
        if (member_path / "src" / "main.rs").exists():
            bin_members.append(member)
            continue
        sub_cargo = member_path / "Cargo.toml"
        if sub_cargo.exists():
            try:
                if "[[bin]]" in sub_cargo.read_text():
                    bin_members.append(member)
            except Exception:
                pass

    if not bin_members:
        return None

    chosen = next(
        (m for pref in _RUST_PREFERRED_NAMES
         for m in bin_members if pref in Path(m).name.lower()),
        bin_members[0],
    )
    pkg_name = Path(chosen).name
    return CommandSet(
        install=None,
        build=f"cargo build --release -p {pkg_name}",
        run=f"cargo run -p {pkg_name}",
        source="cargo-workspace",
    )


# ── 6. Stack defaults ─────────────────────────────────────────────────────────

_STACK_DEFAULTS: dict[str, CommandSet] = {
    "python": CommandSet(
        install="pip install -r requirements.txt",
        run="python main.py",
        source="defaults",
    ),
    "flask": CommandSet(
        install="pip install -r requirements.txt",
        run="flask run",
        source="defaults",
    ),
    "fastapi": CommandSet(
        install="pip install -r requirements.txt",
        run="uvicorn main:app --reload",
        source="defaults",
    ),
    "django": CommandSet(
        install="pip install -r requirements.txt",
        build="python manage.py migrate",
        run="python manage.py runserver",
        source="defaults",
    ),
    "go": CommandSet(
        install="go mod download",
        run="go run .",
        source="defaults",
    ),
    "cargo": CommandSet(
        install=None,
        build="cargo build --release",
        run="cargo run",
        source="defaults",
    ),
    "java": CommandSet(
        install="mvn install -DskipTests",
        build="mvn package -DskipTests",
        run="java -jar target/*.jar",
        source="defaults",
    ),
    "php": CommandSet(
        install="composer install",
        run="php -S localhost:8000 -t public",
        source="defaults",
    ),
    "ruby": CommandSet(
        install="bundle install",
        run="ruby app.rb",
        source="defaults",
    ),
    "rails": CommandSet(
        install="bundle install",
        build="rails db:migrate",
        run="rails server",
        source="defaults",
    ),
    "flutter": CommandSet(
        install="flutter pub get",
        run="flutter run",
        source="defaults",
    ),
    "node": CommandSet(
        install="npm install",
        run="node index.js",
        source="defaults",
    ),
}


def _from_stack_defaults(stack: StackInfo) -> CommandSet | None:
    # Try framework first, then runtime
    for key in (stack.framework.lower(), stack.runtime.lower()):
        if key in _STACK_DEFAULTS:
            return _STACK_DEFAULTS[key]
    return None


# ── README helper ─────────────────────────────────────────────────────────────

def _read_readme(path: Path) -> str | None:
    # Larger window than stack AI so install/run sections are less often truncated.
    return read_readme_text(path, max_chars=24_000)


# ── README heuristic (rule-based, no AI) ──────────────────────────────────────
# Markdown fenced blocks (``` and ~~~), CommonMark-style open/close lengths,
# optional 0–3 space indent, and language allowlists so JSON/YAML blocks are ignored.

_README_FENCE_OPEN_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# First word of the info string after an opening fence (e.g. ```bash  linenums)
_README_SHELL_FENCE_LANGS: frozenset[str] = frozenset({
    "bash", "sh", "shell", "zsh", "console", "cmd", "command", "powershell", "pwsh", "ps1",
    "bat", "batch", "fish", "nu", "terminal", "tty", "prompt", "shell-session",
    "text", "txt", "plain", "plaintext", "output", "nocode",
    "dockerfile", "containerfile",
})

_SHELL_PREFIX_RE = re.compile(r"^[\$>]\s+")

# Lines that look like CI/CD config or template placeholders — skip them
_README_SKIP_LINE_RE = re.compile(
    r"(?:uses:|run:|with:|env:|image:|jobs?:|steps?:|name:|on:|if:|needs?:)"
    r"|<[A-Z_][A-Z0-9_]*>"
    r"|\$\{[^}]+\}"
    r"|your[_\-]?(?:token|key|secret|api)"
    r"|example\.com|placeholder"
    r"|\bcurl\s+.+\|\s*(?:ba)?sh\b",
    re.I,
)

_README_INSTALL_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bnpm\s+(?:i|install|ci)\b", re.I),
    re.compile(r"\bpnpm\s+(?:i|install|add)\b", re.I),
    re.compile(r"\byarn\s+(?:install|add)\b", re.I),
    re.compile(r"\bbun\s+install\b", re.I),
    re.compile(r"\buv\s+(?:sync|pip\s+install|install|tool\s+install)\b", re.I),
    re.compile(r"\bpip\s+install\b", re.I),
    re.compile(r"\bpipx\s+install\b", re.I),
    re.compile(r"\bpoetry\s+install\b", re.I),
    re.compile(r"\bpipenv\s+install\b", re.I),
    re.compile(r"\bconda\s+(?:install|create)\b", re.I),
    re.compile(r"\bgo\s+mod\s+(?:download|tidy)\b", re.I),
    re.compile(r"\bcargo\s+build(?:\s+--release)?\b", re.I),
    re.compile(r"\bbundle\s+install\b", re.I),
    re.compile(r"\bcomposer\s+install\b", re.I),
    re.compile(r"\bdocker\s+(?:pull|build)\b", re.I),
    re.compile(r"\bdocker\s+compose\s+(?:build|pull)\b", re.I),
    re.compile(r"\bdocker-compose\s+(?:build|pull)\b", re.I),
    re.compile(r"\bmake\s+(?:install|setup|bootstrap|deps|prepare)\b", re.I),
    re.compile(r"\bjust\s+(?:deps|install|bootstrap|setup)\b", re.I),
    re.compile(r"\bflutter\s+pub\s+get\b", re.I),
    re.compile(r"\bmvn\s+(?:install|package|compile)\b", re.I),
    re.compile(r"\bgradle\s+(?:build|assemble)\b", re.I),
    re.compile(r"\b\./gradlew(?:\s+\S+)*\s+(?:build|assemble|install)\b", re.I),
    re.compile(r"\bnpx\s+\S+.*\s--install\b", re.I),
    re.compile(r"\bdotnet\s+(?:restore|build)\b", re.I),
]

_README_RUN_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bnpm\s+(?:start|run\s+[\w:@/-]+)\b", re.I),
    re.compile(r"\bpnpm\s+(?:start|run\s+[\w:@/-]+|dev|dlx)\b", re.I),
    re.compile(r"\byarn\s+(?:start|run\s+[\w:@/-]+|dev)\b", re.I),
    re.compile(r"\bbun\s+(?:start|run\s+[\w:@/-]+|dev)\b", re.I),
    re.compile(r"\bnpx\s+\S+", re.I),
    re.compile(r"\bnode\s+\S+", re.I),
    re.compile(r"\bpython(?:\d[\d.]*)?\s+(?:-m\s+\S+|\S+\.py)\b", re.I),
    re.compile(r"\buv\s+run\b", re.I),
    re.compile(r"\buvicorn\s+\S+", re.I),
    re.compile(r"\bflask\s+run\b", re.I),
    re.compile(r"\bgunicorn\s+\S+", re.I),
    re.compile(r"\bgo\s+run\b", re.I),
    re.compile(r"\bcargo\s+run\b", re.I),
    re.compile(r"\bjava\s+-jar\b", re.I),
    re.compile(r"\bdocker\s+run\b", re.I),
    re.compile(r"\bdocker\s+compose(?:\s+--profile\s+[\w-]+)?\s+up\b", re.I),
    re.compile(r"\bdocker-compose(?:\s+--profile\s+[\w-]+)?\s+up\b", re.I),
    re.compile(r"\bmake\s+(?:run|start|dev|serve|up|watch)\b", re.I),
    re.compile(r"\bjust\s+(?:run|start|dev|serve|up|watch)\b", re.I),
    re.compile(r"\bruby\s+\S+", re.I),
    re.compile(r"\brails\s+(?:server|s)\b", re.I),
    re.compile(r"\bphp\s+-S\b", re.I),
    re.compile(r"\bflutter\s+run\b", re.I),
    re.compile(r"\bnext\s+dev\b", re.I),
    re.compile(r"\bvite(?:\s+[\w./:%-]+)*\s*$", re.I),
    re.compile(r"\bastro\s+dev\b", re.I),
    re.compile(r"\bdotnet\s+run\b", re.I),
    re.compile(r"\bturbo\s+(?:dev|run)\b", re.I),
    re.compile(r"\bmix\s+(?:phx\.server|run)\b", re.I),
]

_README_INSTALL_SECTION_RE = re.compile(
    r"install(?:ation|ing)?|set[\s\-]?up|getting[\s\-]started|"
    r"quick[\s\-]start|prerequisites?|requirements?|dependencies|"
    r"how\s+to\s+(?:run|use|install|build)|"
    r"bootstrap|dev\s+environment|local\s+environment|"
    r"build(?:ing)?\s+from\s+source",
    re.I,
)

_README_RUN_SECTION_RE = re.compile(
    r"usage|running|run[\s\-]?(?:locally|app|server|the\s+app)?|"
    r"start(?:ing)?|development|local[\s\-]?dev(?:elopment)?|"
    r"try\s+it|demo|execut(?:e|ion)",
    re.I,
)

_MD_HEADING_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_HEADING_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_README_URL_LINE_RE = re.compile(r"^https?://", re.I)


def _readme_try_open_fence(line: str) -> tuple[str, int, str] | None:
    m = _README_FENCE_OPEN_RE.match(line.replace("\r", ""))
    if not m:
        return None
    fence = m.group("fence")
    info = (m.group("info") or "").strip()
    return fence[0], len(fence), info


def _readme_fence_line_closes(line: str, ch: str, min_len: int) -> bool:
    s = line.replace("\r", "").strip()
    if not s or s[0] != ch:
        return False
    i = 0
    while i < len(s) and s[i] == ch:
        i += 1
    if i < min_len:
        return False
    return i == len(s) or not s[i:].strip()


def _readme_fence_info_is_for_commands(info: str) -> bool:
    """True for unlabeled fences and shell-ish / plain-text fences; false for json, yaml, etc."""
    if not info:
        return True
    first = info.split()[0].lower()
    return first in _README_SHELL_FENCE_LANGS


def _readme_fence_outside_mask(lines: list[str]) -> list[bool]:
    """One bool per line: True when the line is outside fenced code blocks (``` and ~~~)."""
    n = len(lines)
    out = [True] * n
    i = 0
    inside = False
    ch = "`"
    olen = 3
    while i < n:
        line = lines[i]
        if not inside:
            op = _readme_try_open_fence(line)
            if op:
                inside = True
                ch, olen, _ = op
                out[i] = False
                i += 1
                continue
            i += 1
        else:
            if _readme_fence_line_closes(line, ch, olen):
                inside = False
                out[i] = False
                i += 1
            else:
                out[i] = False
                i += 1
    return out


def _readme_extract_shell_fenced_bodies(text: str) -> list[str]:
    """Bodies of fenced blocks whose language tag warrants command extraction."""
    lines = text.splitlines()
    bodies: list[str] = []
    i = 0
    while i < len(lines):
        op = _readme_try_open_fence(lines[i])
        if not op:
            i += 1
            continue
        ch, olen, info = op
        if not _readme_fence_info_is_for_commands(info):
            i += 1
            while i < len(lines) and not _readme_fence_line_closes(lines[i], ch, olen):
                i += 1
            if i < len(lines):
                i += 1
            continue
        i += 1
        chunk: list[str] = []
        while i < len(lines) and not _readme_fence_line_closes(lines[i], ch, olen):
            chunk.append(lines[i])
            i += 1
        if i < len(lines):
            i += 1
        bodies.append("\n".join(chunk))
    return bodies


def _readme_join_continuations(lines: Iterable[str]) -> list[str]:
    """Join shell lines ending with a backslash with the following line."""
    out: list[str] = []
    buf = ""
    for line in lines:
        s = line.rstrip()
        if len(s) > 1 and s.endswith("\\") and not s.endswith("\\\\"):
            buf += s[:-1].rstrip() + " "
            continue
        out.append(buf + s)
        buf = ""
    if buf:
        out.append(buf.rstrip())
    return out


def _readme_clean_command_line(raw: str) -> str:
    line = raw.strip()
    if line.startswith("\ufeff"):
        line = line.lstrip("\ufeff")
    line = _SHELL_PREFIX_RE.sub("", line).strip()
    line = re.sub(r"\s+", " ", line)
    return line


def _normalize_readme_heading(raw: str) -> str:
    """Strip common Markdown from heading text for keyword matching."""
    s = raw.strip()
    s = _MD_HEADING_LINK_RE.sub(r"\1", s)
    s = _MD_HEADING_INLINE_CODE_RE.sub(r"\1", s)
    return s.strip()


def _split_readme_sections(readme: str) -> list[tuple[str, str]]:
    """Return (heading_text, section_body) pairs from a Markdown README.

    ATX headings (# .. ####) are ignored when inside fenced code blocks (``` and ~~~).
    Heading text is normalized (links / inline code stripped) for matching.
    """
    heading_line_re = re.compile(r"^#{1,4}\s+(.+?)\s*$")
    raw_lines = readme.splitlines(keepends=True)
    content_lines = [ln.rstrip("\r\n") for ln in raw_lines]
    outside = _readme_fence_outside_mask(content_lines)
    headings: list[tuple[str, int]] = []

    offset = 0
    for idx, raw in enumerate(raw_lines):
        line_start = offset
        offset += len(raw)
        if not outside[idx]:
            continue
        stripped = content_lines[idx]
        m = heading_line_re.match(stripped)
        if m:
            h = _normalize_readme_heading(m.group(1))
            headings.append((h, line_start))

    if not headings:
        return [("", readme)]

    result: list[tuple[str, str]] = []
    for i, (heading, start) in enumerate(headings):
        end = headings[i + 1][1] if i + 1 < len(headings) else len(readme)
        result.append((heading, readme[start:end]))
    return result


def _extract_readme_commands(text: str) -> tuple[str | None, str | None]:
    """Return (install_cmd, run_cmd) by scanning shell-like fenced code blocks in *text*."""
    install_cmd: str | None = None
    run_cmd: str | None = None
    for body in _readme_extract_shell_fenced_bodies(text):
        for raw in _readme_join_continuations(body.splitlines()):
            for fragment in re.split(r"\s+&&\s+", raw):
                line = _readme_clean_command_line(fragment)
                if not line or line.startswith("#") or _README_SKIP_LINE_RE.search(line):
                    continue
                if _README_URL_LINE_RE.match(line):
                    continue
                if not install_cmd and any(p.search(line) for p in _README_INSTALL_PATTERNS):
                    install_cmd = line
                elif not run_cmd and any(p.search(line) for p in _README_RUN_PATTERNS):
                    run_cmd = line
        if install_cmd and run_cmd:
            break
    return install_cmd, run_cmd


def _from_readme_heuristic(path: Path) -> CommandSet | None:
    """Extract install/run commands from README sections without AI.

    Scans installation/setup/quickstart sections first, then run/usage sections,
    then falls back to the whole README if no section headings matched.
    Returns None when no recognisable commands are found.
    """
    readme = _read_readme(path)
    if not readme:
        return None

    sections = _split_readme_sections(readme)

    # Install sections are matched first; run sections exclude any already matched as install
    # (e.g. "Quick Start" matches both quick-start AND "start" — avoid scanning it twice)
    install_headings: set[str] = {
        h for h, _ in sections if _README_INSTALL_SECTION_RE.search(h)
    }
    install_text = "\n".join(b for h, b in sections if h in install_headings)
    run_text = "\n".join(
        b for h, b in sections
        if _README_RUN_SECTION_RE.search(h) and h not in install_headings
    )
    relevant = (install_text + "\n" + run_text).strip() or readme

    install_cmd, run_cmd = _extract_readme_commands(relevant)

    # If we found a run command but no install, do a full-README scan for install
    if run_cmd and not install_cmd:
        install_cmd, _ = _extract_readme_commands(readme)

    if not install_cmd and not run_cmd:
        return None

    return CommandSet(install=install_cmd, run=run_cmd, source="readme_heuristic")
