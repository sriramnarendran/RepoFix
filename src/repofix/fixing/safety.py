
"""Command safety validation — allowlist and blocklist checks."""

from __future__ import annotations

import re
import shlex


# ── Allowlist ─────────────────────────────────────────────────────────────────
# Only these base executables may be run as automated fixes.

BASE_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Node / JS ecosystem
        "npm", "npx", "yarn", "pnpm", "bun",
        "node",
        "corepack",
        # Python ecosystem
        "pip", "pip3", "python", "python3", "uv", "poetry",
        # Go / Rust
        "go",
        "cargo", "rustup",
        # JVM
        "mvn", "gradle", "gradlew", "java", "javac",
        # Ruby
        "bundle", "gem", "ruby",
        # PHP
        "composer", "php",
        # Dart / Flutter
        "flutter", "dart",
        # Docker
        "docker", "docker-compose",
        # Build tools
        "make", "cmake",
        # File permissions / process control
        "chmod", "kill", "pkill",
        # Git
        "git",
        "ssh",
        # File operations (gated by blocklist patterns)
        "rm", "mkdir", "touch", "cp", "mv", "echo",
        # In-place text edits (version bumps in go.mod, package.json, etc.)
        "sed",
        # System package managers (for native dep fixes)
        "apt", "apt-get", "yum", "dnf", "brew", "apk",
        # Service management (for DB start fixes)
        "systemctl", "service", "pg_ctlcluster",
        # Kernel parameter tuning (for inotify/memory fixes)
        "sysctl",
        # Redis standalone server start
        "redis-server",
        # Privilege escalation — allowed for targeted install/start commands;
        # dangerous sudo patterns are still blocked below
        "sudo",
        # Certificate tools
        "update-ca-certificates", "update-ca-trust",
        # Misc helpers
        "env",
        "command",  # POSIX "command -v" checks (e.g. before corepack enable)
    }
)

# ── Blocklist ─────────────────────────────────────────────────────────────────
# Patterns that are NEVER allowed regardless of allowlist.

_BLOCK_PATTERNS: list[re.Pattern] = [
    # Dangerous rm variants (recursive deletes are allowed only for relative targets;
    # absolute paths, ~, and parent traversal stay blocked.)
    re.compile(r"\brm\b[^;|&]*-[a-zA-Z]*[rR][a-zA-Z]*[^;|&]*\.\./"),  # recursive rm + ../
    re.compile(r"\brm\b[^;|&]*-[a-zA-Z]*[rR][a-zA-Z]*\s+\.\.(\s|;|&|$)"),  # rm -rf ..
    re.compile(r"\brm\s+(-\S+\s+)?/"),                     # rm targeting absolute path
    re.compile(r"\brm\s+(-\S+\s+)?~"),                     # rm targeting home dir
    re.compile(r"&&\s*rm\b"),                               # chained rm
    re.compile(r"\|\s*rm\b"),                               # piped rm
    # Dangerous mv/cp to system paths
    re.compile(r"\b(mv|cp)\s+.*\s+/(?!tmp/)"),             # mv/cp to absolute path (except /tmp)
    # Dangerous sudo — specific destructive usages (blanket sudo is no longer blocked;
    # it is needed for apt-get install, systemctl start, sysctl, etc.)
    re.compile(r"\bsudo\s+rm\b"),                           # sudo rm (any)
    re.compile(r"\bsudo\s+dd\b"),                           # sudo dd
    re.compile(r"\bsudo\s+mkfs\b"),                         # sudo mkfs
    re.compile(r"\bsudo\s+fdisk\b"),                        # sudo fdisk
    re.compile(r"\bsudo\s+passwd\b"),                       # sudo passwd
    re.compile(r"\bsudo\s+visudo\b"),                       # sudo visudo
    re.compile(r"\bsudo\s+usermod\b"),                      # sudo usermod
    re.compile(r"\bsudo\s+userdel\b"),                      # sudo userdel
    re.compile(r"\bsudo\s+groupdel\b"),                     # sudo groupdel
    re.compile(r"\bsudo\s+chown\s+-R\s+/"),                 # sudo chown -R /
    re.compile(r"\bsudo\s+chmod\s+777\s+/"),                # sudo chmod 777 /
    re.compile(r"\bsudo\s+poweroff\b|\bsudo\s+reboot\b|\bsudo\s+shutdown\b"),
    # su escalation
    re.compile(r"\bsu\s"),                                  # su <user>
    # Remote code execution
    re.compile(r"curl\s+.*\|\s*(ba)?sh"),                   # curl | sh
    re.compile(r"wget\s+.*\|\s*(ba)?sh"),                   # wget | sh
    # Disk operations
    re.compile(r"\bdd\b.*if="),                             # dd disk operations
    re.compile(r">\s*/dev/(sd|hd|nvme)"),                   # redirect to disk
    re.compile(r"\bformat\b"),
    re.compile(r"\bfdisk\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bpoweroff\b|\breboot\b|\bshutdown\b"),
    re.compile(r"\bchown\s+-R\s+/"),                        # chown root recursively
    re.compile(r"\bchmod\s+777\s+/"),                       # chmod 777 /
    re.compile(r"\beval\b.*\$\("),                          # eval $(...) injection
]

_PIPE_DOWNLOAD_RE = re.compile(r"(curl|wget).*\|")
_SHELL_EXPANSION_RE = re.compile(r"\$\([^)]+\)")


class UnsafeCommandError(Exception):
    def __init__(self, command: str, reason: str):
        super().__init__(f"Unsafe command rejected: {reason}")
        self.command = command
        self.reason = reason


def validate(command: str, extra_allowlist: set[str] | None = None) -> None:
    """
    Raise UnsafeCommandError if the command is not safe to execute.
    """
    allowlist = BASE_ALLOWLIST | (extra_allowlist or set())

    # Check blocklist patterns first
    for pattern in _BLOCK_PATTERNS:
        if pattern.search(command):
            raise UnsafeCommandError(command, f"Matched blocklist pattern: {pattern.pattern}")

    # Check for shell injection via expansions in non-trusted contexts
    if _SHELL_EXPANSION_RE.search(command):
        raise UnsafeCommandError(command, "Command contains shell expansion $(...)")

    # Check base executable is in allowlist
    try:
        parts = shlex.split(command)
    except ValueError:
        raise UnsafeCommandError(command, "Could not parse command")

    if not parts:
        raise UnsafeCommandError(command, "Empty command")

    base = parts[0]
    # Handle paths like /usr/bin/python
    base_name = base.split("/")[-1]

    if base_name not in allowlist:
        raise UnsafeCommandError(command, f"Base executable '{base_name}' not in allowlist")


def is_safe(command: str, extra_allowlist: set[str] | None = None) -> tuple[bool, str]:
    """Return (is_safe, reason). Non-raising variant of validate()."""
    try:
        validate(command, extra_allowlist)
        return True, ""
    except UnsafeCommandError as exc:
        return False, exc.reason
