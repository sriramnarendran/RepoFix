"""Detect prebuilt binary artifacts from GitHub Releases or local repo directories."""

from __future__ import annotations

import json
import platform
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_GITHUB_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

# Architecture keyword aliases used for scoring artifact filenames
_ARCH_ALIASES: dict[str, list[str]] = {
    "x86_64":  ["x86_64", "amd64", "x64", "64bit", "64-bit"],
    "aarch64": ["aarch64", "arm64", "armv8"],
    "armv7l":  ["armv7", "armhf", "arm"],
    "i386":    ["i386", "i686", "32bit", "x86_32"],
}


@dataclass
class ArtifactInfo:
    name: str
    url: str
    format: str                      # deb | rpm | appimage | exe | msi | dmg | pkg | tar.gz | zip
    size_bytes: int = 0
    score: int = 0
    source: str = "github_releases"  # "github_releases" or "local"
    local_path: Path | None = None


@dataclass
class ArtifactScan:
    available: list[ArtifactInfo] = field(default_factory=list)
    best: ArtifactInfo | None = None
    os_system: str = ""
    os_arch: str = ""
    release_tag: str | None = None
    release_name: str | None = None

    def has_artifacts(self) -> bool:
        return bool(self.available)


def scan(source_url: str | None, repo_path: Path | None) -> ArtifactScan:
    """
    Scan for prebuilt artifacts:
      1. GitHub Releases API (if source_url is a GitHub URL)
      2. Local artifact files inside the repo directory
    Returns a scored, OS-filtered ArtifactScan.
    """
    os_system = platform.system().lower()   # linux | windows | darwin
    os_arch   = platform.machine().lower()  # x86_64 | aarch64 | arm64 | i386

    result = ArtifactScan(os_system=os_system, os_arch=os_arch)
    artifacts: list[ArtifactInfo] = []

    # 1. GitHub Releases
    if source_url:
        m = _GITHUB_RE.match(source_url)
        if m:
            gh_artifacts, tag, release_name = _from_github_releases(
                m.group("owner"), m.group("repo")
            )
            artifacts.extend(gh_artifacts)
            result.release_tag  = tag
            result.release_name = release_name

    # 2. Local repo scan — skip names already found via GitHub
    if repo_path and repo_path.exists():
        existing_names = {a.name for a in artifacts}
        for la in _from_local(repo_path):
            if la.name not in existing_names:
                artifacts.append(la)

    # Score and filter incompatible artifacts
    scored: list[ArtifactInfo] = []
    for a in artifacts:
        s = _score(a.name, a.format, os_system, os_arch)
        if s > 0:
            a.score = s
            scored.append(a)

    scored.sort(key=lambda a: a.score, reverse=True)
    result.available = scored
    result.best = scored[0] if scored else None
    return result


# ── GitHub Releases ───────────────────────────────────────────────────────────

def _from_github_releases(
    owner: str, repo: str
) -> tuple[list[ArtifactInfo], str | None, str | None]:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "repofix/1.0",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data: dict = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return [], None, None

    tag          = data.get("tag_name")
    release_name = data.get("name")
    assets       = data.get("assets", [])

    result: list[ArtifactInfo] = []
    for asset in assets:
        name = asset.get("name", "")
        fmt  = _detect_format(name)
        if fmt == "unknown":
            continue
        result.append(ArtifactInfo(
            name=name,
            url=asset.get("browser_download_url", ""),
            format=fmt,
            size_bytes=asset.get("size", 0),
            source="github_releases",
        ))

    return result, tag, release_name


# ── Local repo scan ───────────────────────────────────────────────────────────

_LOCAL_SEARCH_DIRS = [
    "",           # repo root
    "releases",
    "dist",
    "build",
    "bin",
    "artifacts",
    "out",
    "packages",
]


def _from_local(repo_path: Path) -> list[ArtifactInfo]:
    result: list[ArtifactInfo] = []
    seen: set[str] = set()

    for rel in _LOCAL_SEARCH_DIRS:
        d = repo_path / rel if rel else repo_path
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.is_file() and f.name not in seen:
                fmt = _detect_format(f.name)
                if fmt != "unknown":
                    seen.add(f.name)
                    result.append(ArtifactInfo(
                        name=f.name,
                        url=f.as_uri(),
                        format=fmt,
                        size_bytes=f.stat().st_size,
                        source="local",
                        local_path=f,
                    ))

    return result


# ── Format detection ──────────────────────────────────────────────────────────

def _detect_format(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".deb"):
        return "deb"
    if lower.endswith(".rpm"):
        return "rpm"
    if lower.endswith(".appimage"):
        return "appimage"
    if lower.endswith(".exe"):
        return "exe"
    if lower.endswith(".msi"):
        return "msi"
    if lower.endswith(".dmg"):
        return "dmg"
    if lower.endswith(".pkg"):
        return "pkg"
    if lower.endswith(".tar.gz") or lower.endswith(".tgz"):
        return "tar.gz"
    if lower.endswith(".zip"):
        return "zip"
    return "unknown"


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score(name: str, fmt: str, os_system: str, os_arch: str) -> int:
    """
    Score an artifact for the current OS and architecture.
    Returns 0 for incompatible artifacts.
    Higher score = better match.
    """
    n = name.lower()
    base = _format_base_score(fmt, n, os_system)
    if base == 0:
        return 0

    base += _arch_bonus(n, os_arch)
    return max(base, 0)


def _format_base_score(fmt: str, name_lower: str, os_system: str) -> int:
    n = name_lower

    if os_system == "linux":
        if fmt == "deb":           return 20
        if fmt == "rpm":           return 18
        if fmt == "appimage":      return 15
        if fmt == "tar.gz":
            if "linux" in n or "gnu" in n or "musl" in n: return 13
            if "darwin" in n or "windows" in n or "win" in n: return 0
            return 7
        if fmt == "zip":
            if "linux" in n:       return 11
            if "win" in n or "darwin" in n or "macos" in n or "osx" in n: return 0
            return 5
        if fmt in ("exe", "msi", "dmg", "pkg"): return 0

    elif os_system == "windows":
        if fmt == "msi":           return 20
        if fmt == "exe":           return 18
        if fmt == "zip":
            if "win" in n or "windows" in n: return 13
            if "linux" in n or "darwin" in n or "macos" in n: return 0
            return 7
        if fmt in ("deb", "rpm", "appimage", "dmg", "pkg"): return 0

    elif os_system == "darwin":
        if fmt == "dmg":           return 20
        if fmt == "pkg":           return 18
        if fmt == "tar.gz":
            if "darwin" in n or "macos" in n or "osx" in n or "apple" in n: return 15
            if "linux" in n or "win" in n: return 0
            return 7
        if fmt == "zip":
            if "darwin" in n or "macos" in n: return 13
            if "linux" in n or "win" in n: return 0
            return 5
        if fmt in ("deb", "rpm", "appimage", "exe", "msi"): return 0

    else:
        # Unknown OS — only accept generic archives
        if fmt in ("tar.gz", "zip"): return 5

    return 0


def _arch_bonus(name_lower: str, os_arch: str) -> int:
    current_aliases = _ARCH_ALIASES.get(os_arch, [os_arch])
    all_arch_terms  = [a for aliases in _ARCH_ALIASES.values() for a in aliases]

    arch_in_name = [term for term in all_arch_terms if term in name_lower]

    if not arch_in_name:
        return 2   # generic / universal binary
    if any(alias in name_lower for alias in current_aliases):
        return 10  # exact arch match
    return -5      # arch-specific but wrong architecture


# ── Human-readable helpers ───────────────────────────────────────────────────

_FORMAT_LABELS: dict[str, str] = {
    "deb":      "Debian package (.deb)",
    "rpm":      "RPM package (.rpm)",
    "appimage": "AppImage (portable Linux app)",
    "exe":      "Windows executable (.exe)",
    "msi":      "Windows installer (.msi)",
    "dmg":      "macOS disk image (.dmg)",
    "pkg":      "macOS package (.pkg)",
    "tar.gz":   "Archive (.tar.gz)",
    "zip":      "Archive (.zip)",
}


def format_label(fmt: str) -> str:
    return _FORMAT_LABELS.get(fmt, fmt)
