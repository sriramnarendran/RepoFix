"""Stack detection — language, framework, project type."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class StackInfo:
    language: str = "unknown"
    framework: str = "unknown"
    project_type: str = "unknown"  # frontend | backend | fullstack | service
    runtime: str = "unknown"       # e.g. "node", "python", "go", "docker"
    extras: dict[str, Any] = field(default_factory=dict)
    detection_source: str = "files"   # "files" | "readme_ai"

    def is_docker(self) -> bool:
        return self.runtime == "docker"

    def is_known(self) -> bool:
        return self.language != "unknown"

    def as_display_dict(self) -> dict[str, str]:
        d = {
            "Language": self.language,
            "Framework": self.framework,
            "Type": self.project_type,
            "Runtime": self.runtime,
        }
        if self.detection_source == "readme_ai":
            d["Detected via"] = "README (AI)"
        return d


def detect(repo_path: Path, readme_ai_fallback: "Callable[[str], StackInfo] | None" = None) -> StackInfo:
    """
    Detect the stack for a repository.

    Priority:
      1. Docker (Dockerfile / docker-compose.yml)
      2. Node.js (package.json)
      3. Python (requirements.txt / pyproject.toml / setup.py)
      4. Go (go.mod)
      5. Rust (Cargo.toml)
      6. Java/Kotlin (pom.xml / build.gradle)
      7. PHP (composer.json)
      8. Ruby (Gemfile)
      9. Dart/Flutter (pubspec.yaml)
      10. README AI fallback
    """
    info = (
        _detect_docker(repo_path)
        or _detect_nodejs(repo_path)
        or _detect_python(repo_path)
        or _detect_go(repo_path)
        or _detect_rust(repo_path)
        or _detect_java(repo_path)
        or _detect_php(repo_path)
        or _detect_ruby(repo_path)
        or _detect_dart(repo_path)
    )

    if info:
        return info

    if readme_ai_fallback:
        readme_content = _read_readme(repo_path)
        if readme_content:
            from repofix.output import display
            display.ai_action("Stack ambiguous — analysing README with AI…")
            try:
                ai_info = readme_ai_fallback(readme_content)
                ai_info.detection_source = "readme_ai"
                return ai_info
            except Exception as exc:
                from repofix.output import display as d
                d.warning(f"AI stack detection failed: {exc}")

    return StackInfo()


def detect_without_docker(
    repo_path: Path,
    readme_ai_fallback: "Callable[[str], StackInfo] | None" = None,
) -> StackInfo:
    """
    Like :func:`detect`, but never returns a Docker runtime.

    This is used for "prefer source" mode where Docker compose should be
    treated as an optional deployment wrapper, not the primary runtime.
    """
    info = (
        _detect_nodejs(repo_path)
        or _detect_python(repo_path)
        or _detect_go(repo_path)
        or _detect_rust(repo_path)
        or _detect_java(repo_path)
        or _detect_php(repo_path)
        or _detect_ruby(repo_path)
        or _detect_dart(repo_path)
    )

    if info:
        return info

    if readme_ai_fallback:
        readme_content = _read_readme(repo_path)
        if readme_content:
            from repofix.output import display

            display.ai_action("Stack ambiguous — analysing README with AI…")
            try:
                ai_info = readme_ai_fallback(readme_content)
                ai_info.detection_source = "readme_ai"
                return ai_info
            except Exception as exc:
                from repofix.output import display as d

                d.warning(f"AI stack detection failed: {exc}")

    return StackInfo()


# ── Docker ────────────────────────────────────────────────────────────────────

def _detect_docker(path: Path) -> StackInfo | None:
    has_compose = (path / "docker-compose.yml").exists() or (path / "docker-compose.yaml").exists()
    has_dockerfile = (path / "Dockerfile").exists()

    if not (has_compose or has_dockerfile):
        return None

    # A bare Dockerfile alongside strong language-specific signals (pyproject.toml,
    # requirements.txt, package.json …) usually means Docker is just the deployment
    # wrapper, not the primary runtime. Only claim the docker runtime when compose is
    # present (explicit multi-service intent) or when there are no other strong signals.
    if has_dockerfile and not has_compose:
        _LANGUAGE_SIGNALS = [
            "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg",
            "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
            "Gemfile", "composer.json", "pubspec.yaml",
        ]
        if any((path / s).exists() for s in _LANGUAGE_SIGNALS):
            return None  # defer to the language-specific detector

    mode = "compose" if has_compose else "dockerfile"
    services: list[str] = []

    if has_compose:
        compose_file = path / "docker-compose.yml"
        if not compose_file.exists():
            compose_file = path / "docker-compose.yaml"
        try:
            data = yaml.safe_load(compose_file.read_text())
            services = list((data or {}).get("services", {}).keys())
        except Exception:
            pass

    return StackInfo(
        language="Docker",
        framework="docker-compose" if has_compose else "Docker",
        project_type="service",
        runtime="docker",
        extras={"mode": mode, "services": services},
    )


# ── Node.js ───────────────────────────────────────────────────────────────────

_NODE_FRAMEWORK_DEPS: list[tuple[str, str, str]] = [
    ("next", "Next.js", "frontend"),
    ("react", "React", "frontend"),
    ("vue", "Vue", "frontend"),
    ("@angular/core", "Angular", "frontend"),
    ("svelte", "Svelte", "frontend"),
    ("express", "Express", "backend"),
    ("fastify", "Fastify", "backend"),
    ("@nestjs/core", "NestJS", "backend"),
    ("koa", "Koa", "backend"),
    ("hapi", "Hapi", "backend"),
    ("nuxt", "Nuxt", "fullstack"),
    ("remix", "Remix", "fullstack"),
    ("@redwoodjs/core", "RedwoodJS", "fullstack"),
    ("gatsby", "Gatsby", "frontend"),
    ("vite", "Vite", "frontend"),
]


def _detect_nodejs(path: Path) -> StackInfo | None:
    pkg_file = path / "package.json"
    if not pkg_file.exists():
        return None

    try:
        pkg: dict = json.loads(pkg_file.read_text())
    except Exception:
        return StackInfo(language="Node.js", framework="unknown", project_type="unknown", runtime="node")

    all_deps = {
        **pkg.get("dependencies", {}),
        **pkg.get("devDependencies", {}),
    }

    for dep_key, framework, ptype in _NODE_FRAMEWORK_DEPS:
        if dep_key in all_deps:
            return StackInfo(
                language="Node.js",
                framework=framework,
                project_type=ptype,
                runtime="node",
                extras={"pkg": pkg},
            )

    return StackInfo(language="Node.js", framework="Node.js", project_type="backend", runtime="node", extras={"pkg": pkg})


# ── Python ────────────────────────────────────────────────────────────────────

_PYTHON_FRAMEWORK_DEPS: list[tuple[str, str, str]] = [
    ("fastapi", "FastAPI", "backend"),
    ("flask", "Flask", "backend"),
    ("django", "Django", "fullstack"),
    ("tornado", "Tornado", "backend"),
    ("starlette", "Starlette", "backend"),
    ("streamlit", "Streamlit", "frontend"),
    ("gradio", "Gradio", "frontend"),
    ("aiohttp", "aiohttp", "backend"),
    ("sanic", "Sanic", "backend"),
]


def _detect_python(path: Path) -> StackInfo | None:
    has_req = (path / "requirements.txt").exists()
    has_pyproject = (path / "pyproject.toml").exists()
    has_setup = (path / "setup.py").exists() or (path / "setup.cfg").exists()

    if not (has_req or has_pyproject or has_setup):
        return None

    deps_text = ""
    if has_req:
        try:
            deps_text += (path / "requirements.txt").read_text().lower()
        except Exception:
            pass
    if has_pyproject:
        try:
            deps_text += (path / "pyproject.toml").read_text().lower()
        except Exception:
            pass

    for dep_key, framework, ptype in _PYTHON_FRAMEWORK_DEPS:
        if dep_key in deps_text:
            return StackInfo(
                language="Python",
                framework=framework,
                project_type=ptype,
                runtime="python",
            )

    return StackInfo(language="Python", framework="Python", project_type="backend", runtime="python")


# ── Go ────────────────────────────────────────────────────────────────────────

def _detect_go(path: Path) -> StackInfo | None:
    if not (path / "go.mod").exists():
        return None
    mod_text = ""
    try:
        mod_text = (path / "go.mod").read_text()
    except Exception:
        pass
    framework = "Gin" if "gin-gonic" in mod_text else ("Echo" if "labstack/echo" in mod_text else "Go")
    return StackInfo(language="Go", framework=framework, project_type="backend", runtime="go")


# ── Rust ──────────────────────────────────────────────────────────────────────

def _detect_rust(path: Path) -> StackInfo | None:
    if not (path / "Cargo.toml").exists():
        return None
    cargo_text = ""
    try:
        cargo_text = (path / "Cargo.toml").read_text()
    except Exception:
        pass
    framework = "Actix" if "actix" in cargo_text else ("Axum" if "axum" in cargo_text else "Rust")
    return StackInfo(language="Rust", framework=framework, project_type="backend", runtime="cargo")


# ── Java / Kotlin ─────────────────────────────────────────────────────────────

def _detect_java(path: Path) -> StackInfo | None:
    has_maven = (path / "pom.xml").exists()
    has_gradle = (path / "build.gradle").exists() or (path / "build.gradle.kts").exists()
    if not (has_maven or has_gradle):
        return None
    build_tool = "Maven" if has_maven else "Gradle"
    build_text = ""
    try:
        if has_maven:
            file = path / "pom.xml"
        elif (path / "build.gradle.kts").exists():
            file = path / "build.gradle.kts"
        else:
            file = path / "build.gradle"
        build_text = file.read_text().lower()
    except Exception:
        pass
    if "spring" in build_text:
        framework = "Spring Boot"
    elif "quarkus" in build_text:
        framework = "Quarkus"
    elif "micronaut" in build_text:
        framework = "Micronaut"
    else:
        framework = build_tool
    lang = "Kotlin" if (path / "build.gradle.kts").exists() else "Java"
    return StackInfo(language=lang, framework=framework, project_type="backend", runtime="java", extras={"build_tool": build_tool})


# ── PHP ───────────────────────────────────────────────────────────────────────

def _detect_php(path: Path) -> StackInfo | None:
    if not (path / "composer.json").exists():
        return None
    try:
        data = json.loads((path / "composer.json").read_text())
    except Exception:
        return StackInfo(language="PHP", framework="PHP", project_type="backend", runtime="php")
    deps = {**data.get("require", {}), **data.get("require-dev", {})}
    if "laravel/framework" in deps:
        return StackInfo(language="PHP", framework="Laravel", project_type="fullstack", runtime="php")
    if "symfony/symfony" in deps or any("symfony" in k for k in deps):
        return StackInfo(language="PHP", framework="Symfony", project_type="backend", runtime="php")
    return StackInfo(language="PHP", framework="PHP", project_type="backend", runtime="php")


# ── Ruby ──────────────────────────────────────────────────────────────────────

def _detect_ruby(path: Path) -> StackInfo | None:
    if not (path / "Gemfile").exists():
        return None
    try:
        gemfile = (path / "Gemfile").read_text().lower()
    except Exception:
        return StackInfo(language="Ruby", framework="Ruby", project_type="backend", runtime="ruby")
    if "rails" in gemfile:
        return StackInfo(language="Ruby", framework="Rails", project_type="fullstack", runtime="ruby")
    if "sinatra" in gemfile:
        return StackInfo(language="Ruby", framework="Sinatra", project_type="backend", runtime="ruby")
    return StackInfo(language="Ruby", framework="Ruby", project_type="backend", runtime="ruby")


# ── Dart / Flutter ────────────────────────────────────────────────────────────

def _detect_dart(path: Path) -> StackInfo | None:
    if not (path / "pubspec.yaml").exists():
        return None
    try:
        data = yaml.safe_load((path / "pubspec.yaml").read_text()) or {}
    except Exception:
        return StackInfo(language="Dart", framework="Flutter", project_type="frontend", runtime="flutter")
    deps = {**data.get("dependencies", {})}
    if "flutter" in deps:
        return StackInfo(language="Dart", framework="Flutter", project_type="frontend", runtime="flutter")
    return StackInfo(language="Dart", framework="Dart", project_type="backend", runtime="dart")


# ── README helper ─────────────────────────────────────────────────────────────

def _read_readme(path: Path) -> str | None:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        f = path / name
        if f.exists():
            try:
                return f.read_text(errors="replace")[:8000]
            except Exception:
                pass
    return None


# Type hint forward ref fix
from typing import Callable  # noqa: E402
