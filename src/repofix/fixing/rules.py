"""Rule-based fix strategies for classified errors."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repofix.detection.commands import _node_subpackage_bin_info
from repofix.detection.stack import StackInfo
from repofix.fixing.classifier import ClassifiedError


@dataclass
class FixAction:
    """Describes a concrete action to take to resolve an error."""

    description: str
    commands: list[str] = field(default_factory=list)   # shell commands to run
    env_updates: dict[str, str] = field(default_factory=dict)  # env vars to set
    port_override: int | None = None                    # switch to this port
    next_step: str | None = None                        # one of: "reinstall" | "rebuild" | "rerun"
    source: str = "rule"                                # "rule" | "memory" | "ai"
    run_fn: Callable[[], bool] | None = None            # in-process fix (e.g. docker bind file)

    def is_empty(self) -> bool:
        return (
            not self.commands
            and not self.env_updates
            and self.port_override is None
            and self.run_fn is None
        )


# ── Public entry point ────────────────────────────────────────────────────────

def apply_rule(
    error: ClassifiedError,
    stack: StackInfo,
    repo_path: Path,
) -> FixAction | None:
    """
    Return a FixAction for the error, or None if no rule matches.
    """
    handler = _HANDLERS.get(error.error_type)
    if handler:
        return handler(error, stack, repo_path)
    return None


# ── Handlers ──────────────────────────────────────────────────────────────────

def _fix_missing_dependency(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction | None:
    package = error.extracted.get("package")
    if not package:
        return None

    runtime = stack.runtime.lower()

    if runtime in ("node", "npm", "yarn", "pnpm", "bun"):
        pm = _detect_pkg_manager(repo_path)
        sub_info = _node_subpackage_bin_info(repo_path)
        subdir = sub_info[1] if sub_info else None
        if subdir:
            install_map = {
                "yarn": f"yarn --cwd {subdir} add {package}",
                "pnpm": f"pnpm add {package} --dir {subdir}",
                "bun": f"bun add {package} --cwd {subdir}",
                "npm": f"npm install {package} --prefix {subdir}",
            }
            cmd = install_map.get(pm, f"npm install {package} --prefix {subdir}")
        else:
            install_map = {
                "yarn": f"yarn add {package}",
                "pnpm": f"pnpm add {package}",
                "bun": f"bun add {package}",
                "npm": f"npm install {package}",
            }
            cmd = install_map.get(pm, f"npm install {package}")
        return FixAction(
            description=f"Install missing Node.js package: {package}",
            commands=[cmd],
            next_step="rerun",
            source="rule",
        )

    if runtime in ("python", "pip"):
        # Map common import names to PyPI package names
        pkg_map = {
            "cv2": "opencv-python",
            "PIL": "Pillow",
            "sklearn": "scikit-learn",
            "bs4": "beautifulsoup4",
            "dotenv": "python-dotenv",
            "yaml": "pyyaml",
            "jwt": "pyjwt",
            "git": "gitpython",
        }
        pypi_name = pkg_map.get(package, package)
        return FixAction(
            description=f"Install missing Python package: {pypi_name}",
            commands=[f"pip install {pypi_name}"],
            next_step="rerun",
            source="rule",
        )

    if runtime == "go":
        return FixAction(
            description=f"Fetch missing Go module: {package}",
            commands=[f"go get {package}"],
            next_step="rerun",
            source="rule",
        )

    if runtime == "cargo":
        return FixAction(
            description=f"Add missing Rust crate: {package}",
            commands=[f"cargo add {package}"],
            next_step="rebuild",
            source="rule",
        )

    return None


def _fix_port_conflict(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    port = error.extracted.get("port", 3000)
    return FixAction(
        description=f"Resolve port {port} conflict",
        port_override=port,
        next_step="rerun",
        source="rule",
    )


def _fix_missing_env_var(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    var_name = error.extracted.get("var_name", "")
    return FixAction(
        description=f"Resolve missing environment variable: {var_name}",
        env_updates={var_name: ""} if var_name else {},
        next_step="rerun",
        source="rule",
    )


_go_mod_directive_line_re = re.compile(r"^(\s*go\s+)(\S+)(.*)$")


def _normalize_go_mod_toolchain_version(ver: str) -> str:
    """Strip illegal patch segment (1.22.0 → 1.22) from a go directive value."""
    parts = ver.split(".")
    if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
        return f"{parts[0]}.{parts[1]}"
    return ver


def _fix_go_mod_bad_version(
    error: ClassifiedError,
    stack: StackInfo,
    repo_path: Path,
) -> FixAction | None:
    """
    Fix invalid `go` lines in go.mod (e.g. `go 1.22.0`).

    Newer toolchains reject patch forms; installing golang via apt does not change go.mod.
    """
    raw = (error.extracted.get("go_mod_path") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (repo_path / path).resolve()
    want = (error.extracted.get("wanted_version") or "").strip()

    def _run() -> bool:
        if not path.is_file():
            return False
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        lines = text.splitlines(True)
        out: list[str] = []
        changed = False
        for line in lines:
            core = line.rstrip("\r\n")
            eol = line[len(core) :]
            m = _go_mod_directive_line_re.match(core)
            if m:
                current = m.group(2)
                new_ver = want if want else _normalize_go_mod_toolchain_version(current)
                if new_ver != current:
                    core = f"{m.group(1)}{new_ver}{m.group(3)}"
                    changed = True
            out.append(core + eol)
        if not changed:
            return False
        try:
            path.write_text("".join(out), encoding="utf-8")
        except OSError:
            return False
        return True

    try:
        rel = path.relative_to(repo_path)
    except ValueError:
        rel = path
    return FixAction(
        description=f"Correct invalid `go` version line in {rel} (toolchain rejects patch-style semver in go.mod)",
        commands=[],
        next_step="rerun",
        source="rule",
        run_fn=_run,
    )


def _fix_build_failure(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction | None:
    runtime = stack.runtime.lower()

    if runtime in ("node", "npm"):
        pm = _detect_pkg_manager(repo_path)
        rebuild = f"{pm} run build" if pm != "npm" else "npm run build"
        return FixAction(
            description="Re-run build with verbose output",
            commands=[rebuild + " --verbose 2>&1 | head -100"],
            next_step="rerun",
            source="rule",
        )

    if runtime == "python":
        return FixAction(
            description="Re-install all Python dependencies",
            commands=["pip install -r requirements.txt --force-reinstall"],
            next_step="rerun",
            source="rule",
        )

    return None


def _fix_permission_error(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    return FixAction(
        description="Fix file permissions in repo directory",
        commands=[f"chmod -R u+rwx {repo_path}"],
        next_step="rerun",
        source="rule",
    )


def _fix_bind_mount_is_directory(
    error: ClassifiedError, stack: StackInfo, repo_path: Path
) -> FixAction | None:
    from repofix.core.docker_compose_bind_fix import fix_host_for_container_path

    cpath = (error.extracted.get("container_path") or "").strip()
    if not cpath:
        return None

    def _run() -> bool:
        return fix_host_for_container_path(repo_path, cpath)

    return FixAction(
        description=f"Replace host bind-mount for {cpath} (was a directory) with a config file",
        commands=[],
        next_step="rerun",
        source="rule",
        run_fn=_run,
    )


def _fix_missing_config(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction | None:
    # Try to copy .example or .sample config files
    cmds: list[str] = []
    for ext in (".example", ".sample", ".defaults"):
        for pattern in ("*.env*", "*.config*", "*.json*", "*.yaml*", "*.toml*"):
            for candidate in repo_path.glob(f"**/*{ext}"):
                target = candidate.with_suffix("") if candidate.suffix == ext else Path(str(candidate).replace(ext, ""))
                if not target.exists():
                    cmds.append(f"cp {candidate} {target}")

    if cmds:
        return FixAction(
            description="Copy example config files to their real locations",
            commands=cmds[:5],  # limit to 5 copies
            next_step="rerun",
            source="rule",
        )
    return None


def _fix_version_mismatch(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction | None:
    required = error.extracted.get("required")
    runtime = stack.runtime.lower()
    raw_line = error.signal.raw_line.lower()

    # Prefer Node when the log line is clearly about Node (Vite, engines, etc.) —
    # avoids falling through to the Python venv branch when runtime is mis-detected
    # or when multiple version lines appear in captured output.
    is_node_line = (
        runtime in ("node", "npm")
        or "node.js" in raw_line
        or "nodejs" in raw_line
        or "vite requires" in raw_line
        or "requires node" in raw_line
        or "engine \"node\"" in raw_line
    )

    if is_node_line:
        required_node = (required or "20.19").rstrip("+")
        return FixAction(
            description=f"Switch Node.js version to {required_node}",
            commands=[
                (
                    "bash -lc '"
                    f"REQ={required_node}; "
                    "if command -v fnm >/dev/null 2>&1; then fnm install \"$REQ\" && fnm use \"$REQ\"; "
                    "elif [ -s \"$HOME/.nvm/nvm.sh\" ]; then . \"$HOME/.nvm/nvm.sh\" && nvm install \"$REQ\" && nvm use \"$REQ\"; "
                    "elif command -v asdf >/dev/null 2>&1; then asdf install nodejs \"$REQ\" >/dev/null 2>&1 || true; asdf local nodejs \"$REQ\" && asdf reshim nodejs; "
                    "elif command -v volta >/dev/null 2>&1; then volta install node@\"$REQ\" && volta pin node@\"$REQ\"; "
                    "else echo \"No Node version manager found (fnm/nvm/asdf/volta).\"; exit 1; fi'"
                ),
            ],
            next_step="rerun",
            source="rule",
        )

    # Fire for Python version mismatches regardless of detected runtime: a repo whose
    # primary language is Python may be detected as "docker" (Dockerfile present) yet
    # still fail with a Python version constraint error from uv/pip.
    is_python_version_error = runtime in ("python", "pip") or (
        re.search(r"\bpython\b", raw_line)
        and any(kw in raw_line for kw in ("satisfy", "requires", "python_requires", "does not satisfy"))
    )

    if is_python_version_error:
        # Normalise to major.minor (e.g. "3.11.2" → "3.11")
        version = required or "3.11"
        major_minor = ".".join(version.split(".")[:2])
        return FixAction(
            description=f"Switch Python to {major_minor} and recreate venv",
            commands=[
                # Install the required Python version
                f"uv python install {major_minor} 2>/dev/null || pyenv install -s {major_minor} 2>/dev/null || true",
                # Recreate .venv with the correct Python version so uv pip install uses the right interpreter.
                # --clear lets python -m venv overwrite an existing directory without needing rm -rf.
                f"uv venv --python {major_minor} .venv 2>/dev/null || python{major_minor} -m venv --clear .venv 2>/dev/null || true",
            ],
            next_step="reinstall",
            source="rule",
        )

    return None


def _fix_node_openssl_legacy(
    error: ClassifiedError, stack: StackInfo, repo_path: Path
) -> FixAction:
    """Webpack / react-scripts on Node 17+ hit OpenSSL 3 legacy algorithm limits (common on Stack Overflow)."""
    _ = stack, repo_path, error
    return FixAction(
        description=(
            "Enable OpenSSL legacy provider for Node (webpack 4 / old react-scripts). "
            "Prefer upgrading react-scripts/webpack long-term."
        ),
        env_updates={"NODE_OPTIONS": "--openssl-legacy-provider"},
        next_step="rerun",
        source="rule",
    )


def _fix_git_remote_auth(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    _ = stack
    return FixAction(
        description=(
            "Git cannot access the remote: verify the repo URL, your permissions, and auth "
            "(SSH key on GitHub, or HTTPS with a personal access token instead of a password)."
        ),
        commands=[
            f"git -C {repo_path} remote -v",
            "ssh -T git@github.com 2>&1 || true",
        ],
        next_step="rerun",
        source="rule",
    )


def _fix_pip_resolution(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    _ = error, stack
    cmds = [
        "python3 -m pip install --upgrade pip setuptools wheel",
    ]
    if (repo_path / "requirements.txt").exists():
        cmds.append("python3 -m pip install -r requirements.txt")
    elif (repo_path / "pyproject.toml").exists():
        cmds.append(
            "python3 -m pip install -e . 2>/dev/null || python3 -m pip install . 2>/dev/null || true"
        )
    cmds.append(
        "echo \"If still failing: loosen version pins in pyproject/requirements or use a fresh venv.\""
    )
    return FixAction(
        description="Upgrade pip tooling and retry install (conflicting dependency pins)",
        commands=cmds,
        next_step="reinstall",
        source="rule",
    )


def _fix_corepack_required(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    _ = error, stack, repo_path
    return FixAction(
        description="Enable Corepack so the package.json packageManager field pins Yarn/pnpm correctly (Node 16.13+).",
        commands=["corepack enable"],
        next_step="reinstall",
        source="rule",
    )


def _fix_package_manager_wrong(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    _ = stack
    pm = _detect_pkg_manager(repo_path)
    cmd = {
        "yarn": "yarn install",
        "pnpm": "pnpm install",
        "bun": "bun install",
        "npm": "npm ci 2>/dev/null || npm install",
    }.get(pm, "npm install")
    return FixAction(
        description=f"Install dependencies with the tool that matches the lockfile ({pm}), not a mismatched package manager.",
        commands=[cmd],
        next_step="rerun",
        source="rule",
    )


def _fix_engines_strict(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction | None:
    _ = repo_path
    runtime = stack.runtime.lower()
    if runtime not in ("node", "npm", "yarn", "pnpm", "bun") and "node" not in runtime:
        return None
    required = (error.extracted.get("required") or "").strip()
    hint = f" (package wants Node {required})" if required else ""
    return FixAction(
        description=(
            f"Turn off engine-strict / ignore engines so installs can proceed{hint}. "
            "Prefer switching to the requested Node version when you can."
        ),
        commands=[
            "npm config set engine-strict false 2>/dev/null || true",
            "pnpm config set engine-strict false 2>/dev/null || true",
        ],
        env_updates={"YARN_IGNORE_ENGINES": "1"},
        next_step="reinstall",
        source="rule",
    )


def _fix_glibc_toolchain(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    _ = stack, repo_path
    need = (error.extracted.get("glibc_need") or "").strip()
    suffix = f" (seen {need})" if need else ""
    return FixAction(
        description=(
            f"Binary wheels need newer glibc/libstdc++ than this host provides{suffix}. "
            "Use a newer OS image or container (e.g. Debian bookworm / Ubuntu 22.04+), conda/micromamba, "
            "or install/build from source — upgrading apt glibc in-place is not recommended."
        ),
        commands=[
            'echo "Check: ldd --version  and  pip/uv verbose install for manylinux tag."',
        ],
        next_step="rerun",
        source="rule",
    )


def _fix_gpu_cuda_runtime(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    """Trending ML repos (PyTorch, Warp, etc.) often fail without NVIDIA drivers."""
    _ = repo_path
    line_l = (error.signal.raw_line or "").lower()
    rt = stack.runtime.lower()
    cmds: list[str] = [
        'echo "GPU/CUDA: install a proprietary NVIDIA driver + CUDA toolkit on Linux, or use CPU-only builds."',
    ]
    if (
        "python" in rt
        or rt in ("pip", "unknown")
        or any(k in line_l for k in ("torch", "pytorch", "tensorflow", "jax", "warp", "triton", "cupy"))
    ):
        cmds.append(
            "python3 -m pip install --upgrade torch torchvision torchaudio "
            "--index-url https://download.pytorch.org/whl/cpu 2>/dev/null || true"
        )
    return FixAction(
        description=(
            "No working GPU/CUDA stack — use CPU wheels where possible, or install NVIDIA drivers for CUDA builds."
        ),
        commands=cmds,
        next_step="rerun",
        source="rule",
    )


def _fix_git_lfs_error(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    _ = stack, error
    return FixAction(
        description="Install Git LFS and fetch large files (models, assets)",
        commands=[
            "sudo apt-get install -y git-lfs 2>/dev/null || true",
            "git lfs install 2>/dev/null || true",
            f"git -C {repo_path} lfs pull 2>/dev/null || true",
        ],
        next_step="rerun",
        source="rule",
    )


def _fix_playwright_browsers(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    _ = stack, error
    return FixAction(
        description="Download Playwright browser binaries (or Puppeteer Chromium)",
        commands=[
            "npx playwright install chromium 2>/dev/null || npx playwright install 2>/dev/null || true",
        ],
        next_step="rerun",
        source="rule",
    )


def _fix_ssl_error(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    runtime = error.extracted.get("runtime", stack.runtime).lower()
    if "python" in runtime or runtime in ("pip",):
        return FixAction(
            description="Fix SSL certificates for pip",
            commands=[
                "pip install --upgrade pip certifi",
                "pip install --upgrade pip --trusted-host pypi.org --trusted-host files.pythonhosted.org",
            ],
            next_step="reinstall",
            source="rule",
        )
    if "node" in runtime or runtime in ("npm", "yarn", "pnpm", "bun"):
        return FixAction(
            description="Fix SSL certificate validation for Node.js",
            env_updates={"NODE_TLS_REJECT_UNAUTHORIZED": "0"},
            next_step="rerun",
            source="rule",
        )
    return FixAction(
        description="Update system SSL certificates",
        commands=[
            "sudo apt-get install --reinstall ca-certificates 2>/dev/null || true",
            "sudo update-ca-certificates 2>/dev/null || true",
            "pip install --upgrade certifi 2>/dev/null || true",
        ],
        next_step="reinstall",
        source="rule",
    )


def _fix_memory_limit(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    runtime = error.extracted.get("runtime", stack.runtime).lower()
    if "node" in runtime or runtime in ("npm", "yarn", "pnpm", "bun"):
        return FixAction(
            description="Increase Node.js heap memory limit to 4 GB",
            env_updates={"NODE_OPTIONS": "--max-old-space-size=4096"},
            next_step="rerun",
            source="rule",
        )
    if runtime == "java":
        return FixAction(
            description="Increase JVM heap memory to 2 GB",
            env_updates={"JAVA_TOOL_OPTIONS": "-Xmx2g -Xms512m"},
            next_step="rerun",
            source="rule",
        )
    if runtime == "python":
        return FixAction(
            description="Set Python memory-related environment hints",
            env_updates={"PYTHONUNBUFFERED": "1", "MALLOC_ARENA_MAX": "2"},
            next_step="rerun",
            source="rule",
        )
    return FixAction(
        description="Increase Node.js heap memory limit",
        env_updates={"NODE_OPTIONS": "--max-old-space-size=4096"},
        next_step="rerun",
        source="rule",
    )


def _fix_disk_space(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    runtime = stack.runtime.lower()
    cmds = [
        "sudo sysctl fs.inotify.max_user_watches=524288 2>/dev/null || true",
        "sudo sysctl fs.inotify.max_user_instances=512 2>/dev/null || true",
    ]
    if "node" in runtime or runtime in ("npm", "yarn", "pnpm", "bun"):
        cmds += ["npm cache clean --force 2>/dev/null || true"]
    elif runtime == "python":
        cmds += ["pip cache purge 2>/dev/null || true"]
    return FixAction(
        description="Increase inotify file watcher limits and clean package caches",
        commands=cmds,
        next_step="rerun",
        source="rule",
    )


def _fix_network_error(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    conn_port = error.extracted.get("conn_port")
    _DB_PORTS: dict[int, str] = {
        5432: "postgresql",
        3306: "mysql",
        27017: "mongodb",
        6379: "redis",
        5984: "couchdb",
    }
    if conn_port and conn_port in _DB_PORTS:
        svc = _DB_PORTS[conn_port]
        return FixAction(
            description=f"Start {svc} service (port {conn_port} unreachable)",
            commands=[
                f"sudo systemctl start {svc} 2>/dev/null || sudo service {svc} start 2>/dev/null || true",
            ],
            next_step="rerun",
            source="rule",
        )
    return FixAction(
        description="Network connection failed — check service availability",
        commands=[],
        next_step="rerun",
        source="rule",
    )


def _fix_database_error(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    db_type = error.extracted.get("db_type", "")
    _START_CMDS: dict[str, str] = {
        "postgresql": "sudo systemctl start postgresql 2>/dev/null || pg_ctlcluster start 2>/dev/null || true",
        "mysql": "sudo systemctl start mysql 2>/dev/null || sudo service mysql start 2>/dev/null || true",
        "mariadb": "sudo systemctl start mariadb 2>/dev/null || sudo service mariadb start 2>/dev/null || true",
        "mongodb": "sudo systemctl start mongod 2>/dev/null || sudo service mongod start 2>/dev/null || true",
        "redis": "sudo systemctl start redis 2>/dev/null || redis-server --daemonize yes 2>/dev/null || true",
        "sqlite": "",
    }
    cmd = _START_CMDS.get(db_type or "", "")
    return FixAction(
        description=f"Start {db_type or 'database'} service",
        commands=[cmd] if cmd else [],
        next_step="rerun",
        source="rule",
    )


def _fix_peer_dependency(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    pm = _detect_pkg_manager(repo_path)
    if pm == "npm":
        return FixAction(
            description="Resolve npm peer dependency conflicts with --legacy-peer-deps",
            commands=["npm install --legacy-peer-deps"],
            next_step="rerun",
            source="rule",
        )
    if pm == "yarn":
        return FixAction(
            description="Resolve yarn peer dependency conflicts",
            commands=["yarn install --ignore-optional"],
            next_step="rerun",
            source="rule",
        )
    if pm == "pnpm":
        return FixAction(
            description="Resolve pnpm peer dependency conflicts with shamefully-hoist",
            commands=["pnpm install --shamefully-hoist"],
            next_step="rerun",
            source="rule",
        )
    return FixAction(
        description="Force install to override peer dependency conflicts",
        commands=["npm install --force"],
        next_step="rerun",
        source="rule",
    )


def _fix_bundler_version(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    return FixAction(
        description="Update Ruby Bundler to match Gemfile.lock required version",
        commands=[
            "gem install bundler",
            "bundle update --bundler",
            "bundle install",
        ],
        next_step="rerun",
        source="rule",
    )


def _fix_system_dependency(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    lib = (error.extracted.get("lib") or "").lower()
    _LIB_APT: dict[str, str] = {
        "ssl": "libssl-dev",
        "openssl": "libssl-dev",
        "crypto": "libssl-dev",
        "ffi": "libffi-dev",
        "pq": "libpq-dev",
        "pg": "libpq-dev",
        "mysqlclient": "libmysqlclient-dev",
        "mysql": "libmysqlclient-dev",
        "sqlite3": "libsqlite3-dev",
        "z": "zlib1g-dev",
        "bz2": "libbz2-dev",
        "readline": "libreadline-dev",
        "ncurses": "libncurses5-dev",
        "sasl2": "libsasl2-dev",
        "ldap": "libldap2-dev",
        "xml2": "libxml2-dev",
        "xslt": "libxslt1-dev",
        "jpeg": "libjpeg-dev",
        "png": "libpng-dev",
        "freetype": "libfreetype6-dev",
        "cairo": "libcairo2-dev",
        "glib": "libglib2.0-dev",
        "sodium": "libsodium-dev",
        "lzma": "liblzma-dev",
        "uuid": "uuid-dev",
        "pcre": "libpcre3-dev",
        "gmp": "libgmp-dev",
    }
    base_cmds = ["sudo apt-get install -y build-essential pkg-config 2>/dev/null || true"]
    if lib and lib in _LIB_APT:
        apt_pkg = _LIB_APT[lib]
        cmds = [
            f"sudo apt-get install -y {apt_pkg} 2>/dev/null || "
            f"sudo yum install -y {apt_pkg} 2>/dev/null || true",
        ] + base_cmds
    else:
        cmds = base_cmds + [
            "sudo apt-get install -y libssl-dev libffi-dev libpq-dev libmysqlclient-dev 2>/dev/null || true",
        ]
    return FixAction(
        description=f"Install missing system library: {lib or 'build dependencies'}",
        commands=cmds,
        next_step="reinstall",
        source="rule",
    )


def _fix_compiler_error(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    return FixAction(
        description="Install C/C++ compiler and build tools",
        commands=[
            "sudo apt-get install -y build-essential gcc g++ make 2>/dev/null || "
            "sudo yum groupinstall -y 'Development Tools' 2>/dev/null || true",
        ],
        next_step="reinstall",
        source="rule",
    )


def _fix_lock_file_conflict(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    runtime = stack.runtime.lower()
    raw_l = (error.signal.raw_line or "").lower()
    if (repo_path / "uv.lock").exists() or (
        (repo_path / "pyproject.toml").exists() and ("uv.lock" in raw_l or "uv lock" in raw_l)
    ):
        return FixAction(
            description="Regenerate uv.lock from pyproject.toml",
            commands=["uv lock"],
            next_step="reinstall",
            source="rule",
        )
    if (repo_path / "poetry.lock").exists() or (
        (repo_path / "pyproject.toml").exists()
        and (
            "poetry.lock" in raw_l
            or "poetry lock" in raw_l
            or "pyproject.toml changed significantly" in raw_l
        )
    ):
        return FixAction(
            description="Regenerate poetry.lock to match pyproject.toml",
            commands=["poetry lock --no-update || poetry lock"],
            next_step="reinstall",
            source="rule",
        )
    if "node" in runtime or runtime in ("npm", "yarn", "pnpm", "bun"):
        pm = _detect_pkg_manager(repo_path)
        if pm == "yarn":
            return FixAction(
                description="Regenerate yarn.lock to resolve conflicts",
                commands=["rm -f yarn.lock", "yarn install"],
                next_step="rerun",
                source="rule",
            )
        if pm == "pnpm":
            return FixAction(
                description="Regenerate pnpm-lock.yaml to resolve conflicts",
                commands=["rm -f pnpm-lock.yaml", "pnpm install"],
                next_step="rerun",
                source="rule",
            )
        return FixAction(
            description="Regenerate package-lock.json to resolve conflicts",
            commands=["rm -f package-lock.json", "npm install"],
            next_step="rerun",
            source="rule",
        )
    if runtime == "ruby":
        return FixAction(
            description="Regenerate Gemfile.lock to resolve conflicts",
            commands=["rm -f Gemfile.lock", "bundle install"],
            next_step="rerun",
            source="rule",
        )
    return FixAction(
        description="Remove stale lock files and reinstall dependencies",
        commands=["rm -f package-lock.json yarn.lock pnpm-lock.yaml"],
        next_step="reinstall",
        source="rule",
    )


def _fix_metadata_generation(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    return FixAction(
        description="Fix pip metadata generation failure by upgrading build tools",
        commands=[
            "pip install --upgrade pip setuptools wheel",
            "pip install --upgrade build",
        ],
        next_step="reinstall",
        source="rule",
    )


def _fix_npm_lifecycle_failure(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    """Fix npm install failing because a lifecycle script (prepare/postinstall) tries to
    run a devDependency (e.g. husky) before it is installed — the classic chicken-and-egg.
    Solution: re-run install with --ignore-scripts to get packages on disk, then the
    next normal install (or the user) can set up the hooks manually."""
    pm = _detect_pkg_manager(repo_path)
    install_no_scripts = {
        "yarn": "yarn install --ignore-scripts",
        "pnpm": "pnpm install --ignore-scripts",
        "bun": "bun install --ignore-scripts",
        "npm": "npm install --ignore-scripts",
    }
    cmd = install_no_scripts.get(pm, "npm install --ignore-scripts")
    tool = error.extracted.get("tool_name", "lifecycle script tool")
    return FixAction(
        description=f"Re-run install skipping lifecycle scripts ('{tool}' not yet available)",
        commands=[cmd],
        next_step="run",
        source="rule",
    )


def _fix_node_gyp(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    pm = _detect_pkg_manager(repo_path)
    install_no_scripts = {
        "yarn": "yarn install --ignore-scripts",
        "pnpm": "pnpm install --ignore-scripts",
        "bun": "bun install --ignore-scripts",
        "npm": "npm install --ignore-scripts",
    }
    fallback = install_no_scripts.get(pm, "npm install --ignore-scripts")
    return FixAction(
        description="Install node-gyp system build dependencies and retry",
        commands=[
            "sudo apt-get install -y python3 make g++ 2>/dev/null || "
            "sudo yum install -y python3 make gcc-c++ 2>/dev/null || true",
            fallback,
        ],
        next_step="reinstall",
        source="rule",
    )


def _fix_java_version(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    return FixAction(
        description="Install/configure JDK and set JAVA_HOME",
        commands=[
            "sudo apt-get install -y default-jdk 2>/dev/null || "
            "sudo yum install -y java-17-openjdk-devel 2>/dev/null || true",
        ],
        env_updates={"JAVA_TOOL_OPTIONS": "-Xmx2g"},
        next_step="rebuild",
        source="rule",
    )


def _fix_gradle_error(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    cmds: list[str] = []
    gradle_props = repo_path / "gradle.properties"
    jvm_arg_line = "org.gradle.jvmargs=-Xmx2g -XX:MaxMetaspaceSize=512m"
    if gradle_props.exists():
        cmds.append(f"echo '{jvm_arg_line}' >> {gradle_props}")
    else:
        cmds.append(f"echo '{jvm_arg_line}' > {gradle_props}")
    if (repo_path / "gradlew").exists():
        cmds += [
            f"chmod +x {repo_path / 'gradlew'}",
            f"cd {repo_path} && ./gradlew dependencies --refresh-dependencies",
        ]
    return FixAction(
        description="Increase Gradle JVM heap and refresh dependencies",
        commands=cmds,
        next_step="rebuild",
        source="rule",
    )


def _fix_docker_error(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    line = error.signal.raw_line
    if "Cannot connect to the Docker daemon" in line or "docker daemon running" in line.lower():
        return FixAction(
            description="Start Docker daemon",
            commands=[
                "sudo systemctl start docker 2>/dev/null || true",
            ],
            next_step="rerun",
            source="rule",
        )
    if "Pool overlaps" in line or "failed to create network" in line.lower():
        return FixAction(
            description="Clean up conflicting Docker networks",
            commands=[
                "docker network prune -f 2>/dev/null || true",
                "docker-compose down --remove-orphans 2>/dev/null || true",
            ],
            next_step="rerun",
            source="rule",
        )
    if "pull access denied" in line.lower():
        return FixAction(
            description="Docker image pull failed — login or check image name",
            commands=["docker login 2>/dev/null || true"],
            next_step="rerun",
            source="rule",
        )
    if "failed to solve" in line.lower() or "executor failed running" in line.lower():
        return FixAction(
            description="Dockerfile / BuildKit step failed — prune build cache and retry compose build",
            commands=[
                "docker builder prune -f 2>/dev/null || true",
            ],
            next_step="rerun",
            source="rule",
        )
    return FixAction(
        description="Reset Docker containers and networks",
        commands=[
            "docker-compose down --remove-orphans 2>/dev/null || true",
            "docker network prune -f 2>/dev/null || true",
        ],
        next_step="rerun",
        source="rule",
    )


def _fix_git_submodule(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    return FixAction(
        description="Initialize and update all git submodules",
        commands=[
            f"git -C {repo_path} submodule update --init --recursive",
        ],
        next_step="reinstall",
        source="rule",
    )


def _fix_rust_linker(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    lib = (error.extracted.get("lib") or "").lower()
    raw_line = error.signal.raw_line.lower()
    if "ssl" in lib or "openssl" in raw_line or "ERR_get_error" in error.signal.raw_line:
        return FixAction(
            description="Install OpenSSL development libraries for Rust build",
            commands=[
                "sudo apt-get install -y pkg-config libssl-dev 2>/dev/null || "
                "sudo dnf install -y pkgconf perl-FindBin perl-IPC-Cmd openssl-devel 2>/dev/null || true",
            ],
            env_updates={"PKG_CONFIG_PATH": "/usr/lib/x86_64-linux-gnu/pkgconfig"},
            next_step="rebuild",
            source="rule",
        )
    return FixAction(
        description="Install Rust linker system dependencies",
        commands=[
            "sudo apt-get install -y pkg-config build-essential libssl-dev 2>/dev/null || "
            "sudo yum install -y pkgconfig openssl-devel gcc 2>/dev/null || true",
        ],
        next_step="rebuild",
        source="rule",
    )


_TOOL_INSTALL_CMDS: dict[str, str] = {
    # Python build/package tools — all installable via pip
    "uv": "pip install uv",
    "poetry": "pip install poetry",
    "pipenv": "pip install pipenv",
    "pdm": "pip install pdm",
    "hatch": "pip install hatch",
    "pre-commit": "pip install pre-commit",
    "black": "pip install black",
    "ruff": "pip install ruff",
    "mypy": "pip install mypy",
    "pytest": "pip install pytest",
    "tox": "pip install tox",
    "nox": "pip install nox",
    "flit": "pip install flit",
    "twine": "pip install twine",
    "poe": "pip install poethepoet",
    "poe the poet": "pip install poethepoet",
    # System tools via apt
    "node": "sudo apt-get install -y nodejs npm 2>/dev/null || true",
    "npm": "sudo apt-get install -y npm 2>/dev/null || true",
    # JS package managers — Corepack (bundled with Node 16.13+) then npm global fallback
    "pnpm": (
        "command -v corepack >/dev/null 2>&1 && corepack enable && "
        "corepack prepare pnpm@latest --activate || npm install -g pnpm"
    ),
    "yarn": (
        "command -v corepack >/dev/null 2>&1 && corepack enable && "
        "corepack prepare yarn@stable --activate || npm install -g yarn"
    ),
    "go": "sudo apt-get install -y golang-go 2>/dev/null || true",
    "cargo": "sudo apt-get install -y cargo 2>/dev/null || true",
    "rustc": "sudo apt-get install -y rustc 2>/dev/null || true",
    "java": "sudo apt-get install -y default-jdk 2>/dev/null || true",
    "javac": "sudo apt-get install -y default-jdk 2>/dev/null || true",
    "ruby": "sudo apt-get install -y ruby 2>/dev/null || true",
    "php": "sudo apt-get install -y php 2>/dev/null || true",
    "composer": "sudo apt-get install -y composer 2>/dev/null || true",
    "cmake": "sudo apt-get install -y cmake 2>/dev/null || true",
    # Java build tools
    "mvn": "sudo apt-get install -y maven 2>/dev/null || brew install maven 2>/dev/null || true",
    "maven": "sudo apt-get install -y maven 2>/dev/null || brew install maven 2>/dev/null || true",
    "gradle": "sudo apt-get install -y gradle 2>/dev/null || brew install gradle 2>/dev/null || true",
    "ant": "sudo apt-get install -y ant 2>/dev/null || brew install ant 2>/dev/null || true",
    # Common system utilities
    "make": "sudo apt-get install -y make 2>/dev/null || true",
    "gcc": "sudo apt-get install -y gcc 2>/dev/null || true",
    "g++": "sudo apt-get install -y g++ 2>/dev/null || true",
    "curl": "sudo apt-get install -y curl 2>/dev/null || true",
    "wget": "sudo apt-get install -y wget 2>/dev/null || true",
    "unzip": "sudo apt-get install -y unzip 2>/dev/null || true",
    "zip": "sudo apt-get install -y zip 2>/dev/null || true",
    "jq": "sudo apt-get install -y jq 2>/dev/null || true",
    "docker": "sudo apt-get install -y docker.io 2>/dev/null || true",
    "kubectl": "sudo apt-get install -y kubectl 2>/dev/null || true",
    "terraform": "sudo apt-get install -y terraform 2>/dev/null || true",
    "helm": "sudo apt-get install -y helm 2>/dev/null || true",
}


def _python_cli_install_command(tool: str) -> str | None:
    """Install a CPython interpreter when ``python`` / ``python3.x`` is missing from PATH.

    Uses distro packages first (Debian/Ubuntu/RHEL-ish), then ``uv python install`` or
    ``pyenv`` if present. Exact ``3.x`` builds may require a PPA (Ubuntu) or ``uv`` —
    we cannot guarantee every OS ships ``python3.12`` via apt alone.
    """
    tl = tool.strip()
    if tl in ("python", "python2", "python3"):
        return (
            "sudo apt-get update -qq && sudo apt-get install -y python3 python3-venv python3-pip "
            "2>/dev/null || sudo yum install -y python3 2>/dev/null || true"
        )
    m = re.fullmatch(r"python3\.(\d+)", tl)
    if m:
        ver = f"3.{m.group(1)}"
        return (
            f"sudo apt-get update -qq && sudo apt-get install -y {tl} {tl}-venv 2>/dev/null || "
            f"sudo yum install -y {tl} 2>/dev/null || "
            f"(command -v uv >/dev/null 2>&1 && uv python install {ver}) || "
            f"(command -v pyenv >/dev/null 2>&1 && pyenv install -s {ver}) || true"
        )
    return None


def _fix_missing_tool(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction | None:
    tool = (error.extracted.get("tool_name") or "").strip()
    if not tool:
        return None
    cmd = _TOOL_INSTALL_CMDS.get(tool) or _python_cli_install_command(tool)
    if not cmd:
        return None
    return FixAction(
        description=f"Install missing CLI tool: {tool}",
        commands=[cmd],
        next_step="reinstall",
        source="rule",
    )


def _fix_ruby_gem_error(error: ClassifiedError, stack: StackInfo, repo_path: Path) -> FixAction:
    gem = error.extracted.get("gem", "")
    _GEM_DEPS: dict[str, str] = {
        "pg": "sudo apt-get install -y libpq-dev 2>/dev/null || true",
        "mysql2": "sudo apt-get install -y libmysqlclient-dev 2>/dev/null || true",
        "sqlite3": "sudo apt-get install -y libsqlite3-dev 2>/dev/null || true",
        "nokogiri": "sudo apt-get install -y libxml2-dev libxslt1-dev 2>/dev/null || true",
        "eventmachine": "sudo apt-get install -y libssl-dev 2>/dev/null || true",
        "ffi": "sudo apt-get install -y libffi-dev 2>/dev/null || true",
        "bcrypt": "sudo apt-get install -y build-essential 2>/dev/null || true",
        "rmagick": "sudo apt-get install -y libmagickwand-dev 2>/dev/null || true",
        "curb": "sudo apt-get install -y libcurl4-openssl-dev 2>/dev/null || true",
        "capybara-webkit": "sudo apt-get install -y qt5-default libqt5webkit5-dev 2>/dev/null || true",
    }
    cmds = ["sudo apt-get install -y build-essential ruby-dev 2>/dev/null || true"]
    if gem and gem in _GEM_DEPS:
        cmds.insert(0, _GEM_DEPS[gem])
    cmds.append("bundle install")
    return FixAction(
        description=f"Install system dependencies for {gem or 'Ruby'} native gem",
        commands=cmds,
        next_step="reinstall",
        source="rule",
    )


_HANDLERS = {
    "missing_dependency": _fix_missing_dependency,
    "bind_mount_is_directory": _fix_bind_mount_is_directory,
    "go_mod_bad_version": _fix_go_mod_bad_version,
    "port_conflict": _fix_port_conflict,
    "missing_env_var": _fix_missing_env_var,
    "node_openssl_legacy": _fix_node_openssl_legacy,
    "git_remote_auth": _fix_git_remote_auth,
    "pip_resolution": _fix_pip_resolution,
    "corepack_required": _fix_corepack_required,
    "package_manager_wrong": _fix_package_manager_wrong,
    "engines_strict": _fix_engines_strict,
    "glibc_toolchain": _fix_glibc_toolchain,
    "gpu_cuda_runtime": _fix_gpu_cuda_runtime,
    "git_lfs_error": _fix_git_lfs_error,
    "playwright_browsers": _fix_playwright_browsers,
    "build_failure": _fix_build_failure,
    "permission_error": _fix_permission_error,
    "missing_config": _fix_missing_config,
    "version_mismatch": _fix_version_mismatch,
    "ssl_error": _fix_ssl_error,
    "memory_limit": _fix_memory_limit,
    "disk_space": _fix_disk_space,
    "network_error": _fix_network_error,
    "database_error": _fix_database_error,
    "peer_dependency": _fix_peer_dependency,
    "bundler_version": _fix_bundler_version,
    "system_dependency": _fix_system_dependency,
    "compiler_error": _fix_compiler_error,
    "lock_file_conflict": _fix_lock_file_conflict,
    "metadata_generation": _fix_metadata_generation,
    "npm_lifecycle_failure": _fix_npm_lifecycle_failure,
    "node_gyp": _fix_node_gyp,
    "java_version": _fix_java_version,
    "gradle_error": _fix_gradle_error,
    "docker_error": _fix_docker_error,
    "git_submodule": _fix_git_submodule,
    "rust_linker": _fix_rust_linker,
    "ruby_gem_error": _fix_ruby_gem_error,
    "missing_tool": _fix_missing_tool,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_pkg_manager(path: Path) -> str:
    if (path / "bun.lockb").exists():
        return "bun"
    if (path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (path / "yarn.lock").exists():
        return "yarn"
    return "npm"
