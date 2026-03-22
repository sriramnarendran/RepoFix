"""Classify error signals into typed, structured error objects."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from repofix.fixing.detector import ErrorSignal


@dataclass
class ClassifiedError:
    error_type: str
    description: str
    signal: ErrorSignal
    extracted: dict[str, Any] = field(default_factory=dict)
    # e.g. {"package": "express"} for missing_dependency
    #      {"port": 3000} for port_conflict
    #      {"var_name": "DATABASE_URL"} for missing_env_var

    def fingerprint(self) -> str:
        """Stable key for memory store lookups."""
        parts = [self.error_type]
        if "package" in self.extracted:
            parts.append(self.extracted["package"] or "")
        elif "port" in self.extracted:
            parts.append(str(self.extracted["port"]))
        elif "var_name" in self.extracted:
            parts.append(self.extracted["var_name"] or "")
        elif "tool_name" in self.extracted:
            parts.append(self.extracted["tool_name"] or "")
        elif self.extracted.get("container_path"):
            parts.append(str(self.extracted["container_path"]))
        elif self.extracted.get("go_mod_path"):
            parts.append(str(self.extracted["go_mod_path"]))
        elif self.extracted.get("required"):
            parts.append(str(self.extracted["required"]))
        elif self.extracted.get("glibc_need"):
            parts.append(str(self.extracted["glibc_need"]))
        else:
            # No specific extracted field — derive a stable slug from the raw
            # error line so that different messages never share a cache entry.
            slug = re.sub(r"[^\w]", "", self.signal.raw_line[:80]).lower()
            parts.append(slug)
        return ":".join(parts)


_PORT_RE = re.compile(r":(\d{2,5})")
_JS_MODULE_RE = re.compile(r"Cannot find module ['\"](.+?)['\"]")
_WEBPACK_MODULE_RE = re.compile(r"Can't resolve ['\"](.+?)['\"]")
_ROLLUP_RESOLVE_RE = re.compile(r"Could not resolve ['\"](.+?)['\"]", re.I)
_DENO_MODULE_RE = re.compile(r"error: Module not found ['\"](.+?)['\"]", re.I)
_PY_MODULE_RE = re.compile(r"No module named ['\"]?([^\s'\"]+)")
_PY_MODULE2_RE = re.compile(r"ModuleNotFoundError.*['\"]([^'\"]+)['\"]")
_ENV_KEY_RE = re.compile(r"KeyError:\s*['\"]([A-Z_][A-Z0-9_]*)['\"]")
_ENV_PROC_RE = re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)")
_ENV_MSG_RE = re.compile(r"(?:env|environment).*['\"]([A-Z_][A-Z0-9_]*)['\"]", re.I)
_ENGINES_NODE_RE = re.compile(r"requires node(?:\.js)?[. ]?(?:version)?\s*([\d.x+]+)", re.I)
_CANNOT_FIND_PACKAGE_RE = re.compile(r"Cannot find package ['\"](.+?)['\"]", re.I)
_PYTHON_CONSTRAINT_RE = re.compile(r"Python\s*[><=!]+\s*([\d]+(?:\.[\d]+)*)", re.I)
_HOST_PORT_RE = re.compile(r"([\w][\w.\-]*):(\d{2,5})")
_DB_TYPE_RE = re.compile(r"\b(postgres(?:ql)?|mysql|mongodb|mongo|redis|sqlite|mssql|mariadb|cockroachdb)\b", re.I)
_SYSTEM_LIB_RE = re.compile(r"cannot find -l(\w[\w\-]*)|Package ([\w\-\.]+) was not found|fatal error: ([\w/\.]+\.h)")
_GEM_NAME_RE = re.compile(r"Installing (\S+) \d[\d\.]+|error.*installing (\S+) and Bundler")
_JAVA_VERSION_RE = re.compile(r"major version (\d+)|target release: (\d+)")
_GO_MOD_BAD_VER_RE = re.compile(
    r"(?P<path>[^:]+):\d+:\s*invalid go version\s+['\"](?P<bad>[^'\"]+)['\"]"
    r"(?:\s*:\s*must match format\s+(?P<want>[\d.]+))?",
    re.I,
)
_MISSING_TOOL_RE = re.compile(
    r"make:\s+(\S+): No such file or directory"
    r"|bash: (\S+): command not found"
    r"|zsh: command not found: (\S+)"
    r"|sh: \d*:? ?(\S+): (?:not found|No such file or directory)"
    r"|(?:/bin/)?sh:\s*\d+:\s*(\S+):\s*(?:not found|No such file or directory)"
    r"|^(\S+): command not found"
    r"|Error:\s+(\w[\w.-]*)\s+not found"
    r"|(\w[\w.-]+) (?:is )?not found(?: in PATH)?",
    re.I,
)
# Maps prose "X compiler/toolchain not found" phrases to the canonical tool name
_COMPILER_TOOL_RE = re.compile(
    r"\b(go(?:lang)?)\s+(?:compiler|toolchain|binary)\s+not\s+found"
    r"|\bGo compiler not found"
    r"|\b(rust(?:c)?)\s+(?:compiler|toolchain)\s+not\s+found"
    r"|\b(C(?:\+\+)?)\s+compiler\s+not\s+found",
    re.I,
)
_COMPILER_TOOL_CANONICAL: dict[str, str] = {
    "go": "go",
    "golang": "go",
    "rust": "rustc",
    "rustc": "rustc",
    "c": "gcc",
    "c++": "g++",
}
_NPM_LIFECYCLE_TOOL_RE = re.compile(r"^sh:\s*\d*:?\s*(\S+):\s+not found$", re.I)
_ENGINES_WANTED_JSON_RE = re.compile(
    r'wanted:\s*\{\s*["\']node["\']\s*:\s*["\']([^"\']+)["\']',
    re.I,
)
_GLIBC_TAG_RE = re.compile(r"GLIBC_(\d+\.\d+(?:\.\d+)?)|GLIBCXX_(\d+\.\d+(?:\.\d+)?)", re.I)
_SHARED_LIB_LOAD_RE = re.compile(
    r"cannot open shared object file:\s*([^\s]+)|"
    r"error while loading shared libraries:\s*([^\s:]+)|"
    r"(lib[\w\-\.]+\.so(?:\.\d+)?)",
    re.I,
)


def classify(signal: ErrorSignal, runtime: str = "unknown") -> ClassifiedError:
    """Convert a raw ErrorSignal into a ClassifiedError with extracted metadata."""
    line = signal.raw_line
    context = "\n".join(signal.context_lines)

    if signal.error_type == "missing_dependency":
        package = _extract_missing_package(line, context, runtime)
        return ClassifiedError(
            error_type="missing_dependency",
            description=f"Missing package: {package or 'unknown'}",
            signal=signal,
            extracted={"package": package, "runtime": runtime},
        )

    if signal.error_type == "port_conflict":
        port = _extract_port(line, context)
        return ClassifiedError(
            error_type="port_conflict",
            description=f"Port {port} already in use",
            signal=signal,
            extracted={"port": port},
        )

    if signal.error_type == "missing_env_var":
        var_name = _extract_env_var(line, context)
        return ClassifiedError(
            error_type="missing_env_var",
            description=f"Missing environment variable: {var_name or 'unknown'}",
            signal=signal,
            extracted={"var_name": var_name},
        )

    if signal.error_type == "build_failure":
        return ClassifiedError(
            error_type="build_failure",
            description=f"Build failed: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "version_mismatch":
        required = _extract_version(line)
        return ClassifiedError(
            error_type="version_mismatch",
            description=f"Version mismatch: {required or line[:80]}",
            signal=signal,
            extracted={"required": required, "runtime": runtime},
        )

    if signal.error_type == "permission_error":
        return ClassifiedError(
            error_type="permission_error",
            description=f"Permission denied: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "missing_config":
        return ClassifiedError(
            error_type="missing_config",
            description=f"Missing config file: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "bind_mount_is_directory":
        m = re.search(r"Is a directory:\s*['\"]([^'\"]+)['\"]", line, re.I)
        cpath = m.group(1) if m else ""
        return ClassifiedError(
            error_type="bind_mount_is_directory",
            description=f"Bind mount path is a directory in the container: {cpath}",
            signal=signal,
            extracted={"container_path": cpath},
        )

    if signal.error_type == "wrong_entry_point":
        # Extract the bad file path so the handler knows what was tried
        m = re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", line, re.I) or re.search(
            r"error: Module not found ['\"]([^'\"]+)['\"]", line, re.I
        )
        bad_path = m.group(1) if m else line[:120]
        return ClassifiedError(
            error_type="wrong_entry_point",
            description=f"Entry file not found: {bad_path}",
            signal=signal,
            extracted={"bad_path": bad_path},
        )

    if signal.error_type == "npm_lifecycle_failure":
        tool = _extract_npm_lifecycle_tool(line, context) or ""
        return ClassifiedError(
            error_type="npm_lifecycle_failure",
            description=f"npm lifecycle script failed — '{tool}' not found (try --ignore-scripts)",
            signal=signal,
            extracted={"tool_name": tool},
        )

    if signal.error_type == "cli_no_subcommand":
        return ClassifiedError(
            error_type="cli_no_subcommand",
            description="Detected a CLI tool that requires a subcommand",
            signal=signal,
        )

    if signal.error_type == "cli_needs_args":
        return ClassifiedError(
            error_type="cli_needs_args",
            description="CLI tool printed usage/help — requires positional arguments or file paths",
            signal=signal,
        )

    if signal.error_type == "ssl_error":
        return ClassifiedError(
            error_type="ssl_error",
            description=f"SSL/TLS certificate error: {line[:120]}",
            signal=signal,
            extracted={"runtime": runtime},
        )

    if signal.error_type == "node_openssl_legacy":
        return ClassifiedError(
            error_type="node_openssl_legacy",
            description="Node.js OpenSSL 3 / legacy crypto (webpack, old react-scripts)",
            signal=signal,
            extracted={"runtime": runtime},
        )

    if signal.error_type == "memory_limit":
        return ClassifiedError(
            error_type="memory_limit",
            description=f"Out of memory: {line[:120]}",
            signal=signal,
            extracted={"runtime": runtime},
        )

    if signal.error_type == "disk_space":
        return ClassifiedError(
            error_type="disk_space",
            description=f"Disk space / inotify watcher limit exceeded: {line[:120]}",
            signal=signal,
            extracted={"runtime": runtime},
        )

    if signal.error_type == "network_error":
        host, port = _extract_host_port(line, context)
        return ClassifiedError(
            error_type="network_error",
            description=f"Network connection error: {line[:120]}",
            signal=signal,
            extracted={"host": host, "conn_port": port},
        )

    if signal.error_type == "database_error":
        db_type = _extract_db_type(line, context)
        return ClassifiedError(
            error_type="database_error",
            description=f"Database connection/config error: {line[:120]}",
            signal=signal,
            extracted={"db_type": db_type},
        )

    if signal.error_type == "peer_dependency":
        return ClassifiedError(
            error_type="peer_dependency",
            description=f"npm peer dependency conflict: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "bundler_version":
        return ClassifiedError(
            error_type="bundler_version",
            description="Ruby Bundler version mismatch with Gemfile.lock",
            signal=signal,
        )

    if signal.error_type == "system_dependency":
        lib_name = _extract_system_lib(line) or _extract_shared_lib_from_line(line)
        return ClassifiedError(
            error_type="system_dependency",
            description=f"Missing system library or header: {lib_name or line[:80]}",
            signal=signal,
            extracted={"lib": lib_name},
        )

    if signal.error_type == "compiler_error":
        return ClassifiedError(
            error_type="compiler_error",
            description=f"C/C++ compiler not available or failed: {line[:120]}",
            signal=signal,
            extracted={"runtime": runtime},
        )

    if signal.error_type == "lock_file_conflict":
        return ClassifiedError(
            error_type="lock_file_conflict",
            description=f"Lock file conflict or out of sync: {line[:120]}",
            signal=signal,
            extracted={"runtime": runtime},
        )

    if signal.error_type == "metadata_generation":
        return ClassifiedError(
            error_type="metadata_generation",
            description=f"pip metadata generation failed: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "node_gyp":
        return ClassifiedError(
            error_type="node_gyp",
            description=f"node-gyp native addon build error: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "java_version":
        version = _extract_java_version(line, context)
        return ClassifiedError(
            error_type="java_version",
            description=f"Java version issue: {line[:120]}",
            signal=signal,
            extracted={"version": version, "runtime": runtime},
        )

    if signal.error_type == "gradle_error":
        return ClassifiedError(
            error_type="gradle_error",
            description=f"Gradle/Maven build error: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "docker_error":
        return ClassifiedError(
            error_type="docker_error",
            description=f"Docker error: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "git_submodule":
        return ClassifiedError(
            error_type="git_submodule",
            description="Git submodules not initialized or missing",
            signal=signal,
        )

    if signal.error_type == "git_remote_auth":
        return ClassifiedError(
            error_type="git_remote_auth",
            description=f"Git remote or authentication failure: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "pip_resolution":
        return ClassifiedError(
            error_type="pip_resolution",
            description=f"pip cannot resolve dependencies: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "corepack_required":
        return ClassifiedError(
            error_type="corepack_required",
            description=f"Corepack must be enabled for package.json packageManager: {line[:120]}",
            signal=signal,
            extracted={"runtime": runtime},
        )

    if signal.error_type == "package_manager_wrong":
        return ClassifiedError(
            error_type="package_manager_wrong",
            description=f"Wrong package manager for this repo's lockfile: {line[:120]}",
            signal=signal,
            extracted={"runtime": runtime},
        )

    if signal.error_type == "engines_strict":
        required = _extract_version(line) or _extract_engines_node_version(line, context)
        return ClassifiedError(
            error_type="engines_strict",
            description=f"package.json engines rejected by package manager: {line[:120]}",
            signal=signal,
            extracted={"required": required, "runtime": runtime},
        )

    if signal.error_type == "glibc_toolchain":
        glibc = _extract_glibc_version(line, context)
        return ClassifiedError(
            error_type="glibc_toolchain",
            description=f"glibc/libstdc++/manylinux wheel mismatch: {line[:120]}",
            signal=signal,
            extracted={"glibc_need": glibc},
        )

    if signal.error_type == "gpu_cuda_runtime":
        return ClassifiedError(
            error_type="gpu_cuda_runtime",
            description=f"GPU/CUDA not available or driver mismatch: {line[:120]}",
            signal=signal,
            extracted={"runtime": runtime},
        )

    if signal.error_type == "git_lfs_error":
        return ClassifiedError(
            error_type="git_lfs_error",
            description=f"Git LFS missing or LFS objects not fetched: {line[:120]}",
            signal=signal,
        )

    if signal.error_type == "playwright_browsers":
        return ClassifiedError(
            error_type="playwright_browsers",
            description=f"Playwright/Puppeteer browser binary missing: {line[:120]}",
            signal=signal,
            extracted={"runtime": runtime},
        )

    if signal.error_type == "rust_linker":
        lib = _extract_system_lib(line)
        return ClassifiedError(
            error_type="rust_linker",
            description=f"Rust linker error (missing system library): {line[:120]}",
            signal=signal,
            extracted={"lib": lib},
        )

    if signal.error_type == "ruby_gem_error":
        gem = _extract_gem_name(line, context)
        return ClassifiedError(
            error_type="ruby_gem_error",
            description=f"Ruby native gem build error: {gem or line[:80]}",
            signal=signal,
            extracted={"gem": gem},
        )

    if signal.error_type == "missing_tool":
        tool_name = _extract_tool_name(line, context)
        return ClassifiedError(
            error_type="missing_tool",
            description=f"Missing CLI tool: {tool_name or line[:80]}",
            signal=signal,
            extracted={"tool_name": tool_name},
        )

    if signal.error_type == "go_mod_bad_version":
        info = _extract_go_mod_bad_version(line, context)
        if info:
            return ClassifiedError(
                error_type="go_mod_bad_version",
                description=f"Invalid go directive in go.mod: {info.get('bad_version', '')}",
                signal=signal,
                extracted=info,
            )
        return ClassifiedError(
            error_type="go_mod_bad_version",
            description=line[:200],
            signal=signal,
        )

    return ClassifiedError(
        error_type="generic_error",
        description=line[:200],
        signal=signal,
    )


_ERROR_PRIORITY: dict[str, int] = {
    # Most specific / actionable first — these have targeted fix rules.
    "npm_lifecycle_failure": 0,
    "node_gyp": 1,
    "missing_dependency": 2,
    "bind_mount_is_directory": 2,
    "wrong_entry_point": 3,
    "missing_config": 4,
    "missing_env_var": 5,
    "port_conflict": 6,
    "permission_error": 7,
    "version_mismatch": 8,
    "ssl_error": 9,
    "node_openssl_legacy": 8,
    "glibc_toolchain": 9,
    "gpu_cuda_runtime": 10,
    "playwright_browsers": 11,
    "system_dependency": 12,
    "compiler_error": 13,
    "java_version": 14,
    "gradle_error": 15,
    "docker_error": 16,
    "git_submodule": 17,
    "git_lfs_error": 18,
    "rust_linker": 19,
    "ruby_gem_error": 20,
    "network_error": 21,
    "database_error": 22,
    "peer_dependency": 23,
    "corepack_required": 24,
    "package_manager_wrong": 24,
    "engines_strict": 25,
    "lock_file_conflict": 26,
    "bundler_version": 27,
    "pip_resolution": 28,
    "build_failure": 29,
    "go_mod_bad_version": 3,
    "cli_no_subcommand": 30,
    "cli_needs_args": 31,
    "git_remote_auth": 33,
    # Generic / low-signal last
    "missing_tool": 90,
    "generic_error": 99,
}


def classify_all(signals: list[ErrorSignal], runtime: str = "unknown") -> list[ClassifiedError]:
    errors = [classify(s, runtime) for s in signals]
    errors.sort(key=lambda e: _ERROR_PRIORITY.get(e.error_type, 50))
    return errors


# ── Extractors ────────────────────────────────────────────────────────────────


def _extract_go_mod_bad_version(line: str, context: str) -> dict[str, str] | None:
    for text in (line, context):
        for chunk in text.splitlines():
            m = _GO_MOD_BAD_VER_RE.search(chunk)
            if m:
                want = (m.group("want") or "").strip()
                return {
                    "go_mod_path": m.group("path"),
                    "bad_version": m.group("bad"),
                    "wanted_version": want,
                }
    return None


def _extract_missing_package(line: str, context: str, runtime: str) -> str | None:
    for pattern in (_JS_MODULE_RE, _WEBPACK_MODULE_RE, _ROLLUP_RESOLVE_RE, _DENO_MODULE_RE, _CANNOT_FIND_PACKAGE_RE):
        m = pattern.search(line)
        if m:
            pkg = m.group(1)
            # Strip relative paths — those aren't installable packages
            if pkg.startswith("."):
                return None
            return pkg.lstrip("@").split("/")[0] if not pkg.startswith("@") else "/".join(pkg.split("/")[:2])

    for pattern in (_PY_MODULE_RE, _PY_MODULE2_RE):
        m = pattern.search(line) or pattern.search(context)
        if m:
            return m.group(1).split(".")[0]

    return None


def _extract_port(line: str, context: str) -> int:
    for text in (line, context):
        m = _PORT_RE.search(text)
        if m:
            return int(m.group(1))
    return 3000


def _extract_env_var(line: str, context: str) -> str | None:
    for pattern in (_ENV_KEY_RE, _ENV_PROC_RE, _ENV_MSG_RE):
        m = pattern.search(line) or pattern.search(context)
        if m:
            return m.group(1)
    return None


def _extract_version(line: str) -> str | None:
    m = _ENGINES_NODE_RE.search(line)
    if m:
        return m.group(1).rstrip("+")
    m = _PYTHON_CONSTRAINT_RE.search(line)
    if m:
        return m.group(1)
    return None


def _extract_host_port(line: str, context: str) -> tuple[str | None, int | None]:
    for text in (line, context):
        m = _HOST_PORT_RE.search(text)
        if m:
            try:
                return m.group(1), int(m.group(2))
            except (ValueError, IndexError):
                pass
    return None, None


def _extract_db_type(line: str, context: str) -> str | None:
    for text in (line, context):
        m = _DB_TYPE_RE.search(text)
        if m:
            raw = m.group(1).lower()
            # Normalise aliases
            return "mongodb" if raw == "mongo" else "postgresql" if raw == "postgres" else raw
    return None


def _extract_system_lib(line: str) -> str | None:
    m = _SYSTEM_LIB_RE.search(line)
    if m:
        return m.group(1) or m.group(2) or m.group(3)
    return None


def _extract_shared_lib_from_line(line: str) -> str | None:
    """Best-effort .so name from ImportError / loader messages."""
    m = _SHARED_LIB_LOAD_RE.search(line)
    if not m:
        return None
    return next((g for g in m.groups() if g), None)


def _extract_gem_name(line: str, context: str) -> str | None:
    for text in (line, context):
        m = _GEM_NAME_RE.search(text)
        if m:
            return m.group(1) or m.group(2)
    return None


def _extract_java_version(line: str, context: str) -> str | None:
    for text in (line, context):
        m = _JAVA_VERSION_RE.search(text)
        if m:
            return m.group(1) or m.group(2)
    return None


def _extract_tool_name(line: str, context: str) -> str | None:
    # Check for "X compiler/toolchain not found" prose first — gives canonical tool name
    for text in (line, context):
        m = _COMPILER_TOOL_RE.search(text)
        if m:
            raw = next((g for g in m.groups() if g), None)
            if raw:
                return _COMPILER_TOOL_CANONICAL.get(raw.lower(), raw.lower())
    # Fallback to shell-style "command not found" patterns
    for text in (line, context):
        m = _MISSING_TOOL_RE.search(text)
        if m:
            return next((g for g in m.groups() if g), None)
    return None


def _extract_npm_lifecycle_tool(line: str, context: str) -> str | None:
    for text in (line, context):
        m = _NPM_LIFECYCLE_TOOL_RE.search(text)
        if m:
            return m.group(1)
    return None


def _extract_engines_node_version(line: str, context: str) -> str | None:
    for text in (line, context):
        m = _ENGINES_WANTED_JSON_RE.search(text)
        if m:
            raw = m.group(1).strip()
            return re.sub(r"^[>=^~]+", "", raw).split()[0] if raw else None
    return None


def _extract_glibc_version(line: str, context: str) -> str | None:
    for text in (line, context):
        m = _GLIBC_TAG_RE.search(text)
        if m:
            return m.group(1) or m.group(2)
    return None
