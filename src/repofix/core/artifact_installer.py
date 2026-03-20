"""Download and install prebuilt binary artifacts (deb, rpm, AppImage, tar.gz, zip, exe, msi, dmg, pkg)."""

from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import time
import urllib.request
import urllib.error
import zipfile
from dataclasses import dataclass
from pathlib import Path

from repofix import config as cfg
from repofix.detection.artifacts import ArtifactInfo
from repofix.output import display


@dataclass
class ArtifactInstallResult:
    success: bool
    artifact: ArtifactInfo
    local_path: Path | None = None
    run_command: str | None = None   # shell command to start the app (if applicable)
    installed_system: bool = False   # True when system-wide install (deb/rpm/dmg/pkg)
    error: str | None = None


def install(artifact: ArtifactInfo, auto_approve: bool = False) -> ArtifactInstallResult:
    """
    Download the artifact if necessary, then install / prepare it.
    Returns an ArtifactInstallResult with a run_command when the artifact
    starts a long-lived process (AppImage, extracted binary, exe).
    """
    try:
        local = _ensure_local(artifact)
    except Exception as exc:
        return ArtifactInstallResult(
            success=False, artifact=artifact, error=f"Download failed: {exc}"
        )

    fmt = artifact.format

    if fmt == "deb":
        return _install_deb(artifact, local, auto_approve)
    if fmt == "rpm":
        return _install_rpm(artifact, local, auto_approve)
    if fmt == "appimage":
        return _setup_appimage(artifact, local)
    if fmt == "tar.gz":
        return _install_tar(artifact, local)
    if fmt == "zip":
        return _install_zip(artifact, local)
    if fmt == "exe":
        return _run_exe(artifact, local)
    if fmt == "msi":
        return _install_msi(artifact, local)
    if fmt == "dmg":
        return _install_dmg(artifact, local, auto_approve)
    if fmt == "pkg":
        return _install_pkg(artifact, local, auto_approve)

    return ArtifactInstallResult(
        success=False, artifact=artifact, error=f"Unsupported format: {fmt}"
    )


# ── Download helper ───────────────────────────────────────────────────────────

def _ensure_local(artifact: ArtifactInfo) -> Path:
    """Return a local Path for the artifact, downloading from URL if needed."""
    if artifact.local_path and artifact.local_path.exists():
        return artifact.local_path

    # Use a stable cache directory so repeat runs don't re-download
    app_cfg = cfg.load()
    cache_dir = Path(app_cfg.clone_base_dir).parent / "artifacts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / artifact.name

    if dest.exists():
        display.info(f"Using cached artifact: [bold]{artifact.name}[/bold]")
        return dest

    size_mb = artifact.size_bytes / 1024 / 1024
    size_hint = f" ({size_mb:.1f} MB)" if size_mb > 0.1 else ""
    display.step(f"Downloading [bold]{artifact.name}[/bold]{size_hint}…")

    _download(artifact.url, dest)
    display.success(f"Downloaded to [bold]{dest}[/bold]")
    return dest


def _download(url: str, dest: Path) -> None:
    """Stream-download url → dest with periodic progress prints."""
    req = urllib.request.Request(url, headers={"User-Agent": "repofix/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total      = int(resp.getheader("Content-Length", 0))
        downloaded = 0
        chunk_size = 65536  # 64 KB
        last_print = time.monotonic()

        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if now - last_print > 2.0 and total:
                    pct = downloaded / total * 100
                    display.muted(
                        f"  {pct:.0f}%  "
                        f"{downloaded // (1024*1024)}MB / {total // (1024*1024)}MB"
                    )
                    last_print = now


# ── Format-specific installers ────────────────────────────────────────────────

def _install_deb(
    artifact: ArtifactInfo, path: Path, auto_approve: bool
) -> ArtifactInstallResult:
    if not shutil.which("dpkg"):
        return ArtifactInstallResult(
            success=False, artifact=artifact,
            error=f"dpkg not found. Install manually: sudo dpkg -i {path}",
        )

    display.step(f"Installing Debian package: [bold]{path.name}[/bold]")

    if not auto_approve:
        ok = display.prompt_confirm(
            f"This will run [bold]sudo dpkg -i {path.name}[/bold] (requires root). Proceed?"
        )
        if not ok:
            return ArtifactInstallResult(
                success=False, artifact=artifact, error="Installation declined by user"
            )

    # Read the real package name from the .deb metadata BEFORE installing
    pkg_name = _deb_real_package_name(path)

    result = subprocess.run(["sudo", "dpkg", "-i", str(path)])
    if result.returncode != 0:
        # Attempt to resolve broken dependencies automatically
        subprocess.run(["sudo", "apt-get", "install", "-f", "-y"])

    display.success(f"Package [bold]{pkg_name}[/bold] installed")

    run_cmd = _find_installed_binary(pkg_name)
    if run_cmd:
        display.info(f"Binary detected: [bold]{run_cmd}[/bold]")
    else:
        display.warning(
            f"Could not auto-detect the binary for [bold]{pkg_name}[/bold]. "
            f"Run [bold]dpkg -L {pkg_name}[/bold] to list installed files."
        )

    return ArtifactInstallResult(
        success=True, artifact=artifact, local_path=path,
        run_command=run_cmd, installed_system=True,
    )


def _install_rpm(
    artifact: ArtifactInfo, path: Path, auto_approve: bool
) -> ArtifactInstallResult:
    tool = shutil.which("dnf") or shutil.which("rpm")
    if not tool:
        return ArtifactInstallResult(
            success=False, artifact=artifact,
            error=f"Neither dnf nor rpm found. Install manually: sudo rpm -i {path}",
        )

    display.step(f"Installing RPM package: [bold]{path.name}[/bold]")

    if not auto_approve:
        ok = display.prompt_confirm(
            f"This will run [bold]sudo {Path(tool).name} install {path.name}[/bold] (requires root). Proceed?"
        )
        if not ok:
            return ArtifactInstallResult(
                success=False, artifact=artifact, error="Installation declined by user"
            )

    cmd = (
        ["sudo", "dnf", "install", "-y", str(path)]
        if "dnf" in tool
        else ["sudo", "rpm", "-i", str(path)]
    )
    result = subprocess.run(cmd)
    if result.returncode != 0:
        return ArtifactInstallResult(
            success=False, artifact=artifact, error="RPM install failed"
        )

    display.success("RPM package installed")
    return ArtifactInstallResult(
        success=True, artifact=artifact, local_path=path, installed_system=True
    )


def _setup_appimage(artifact: ArtifactInfo, path: Path) -> ArtifactInstallResult:
    display.step(f"Preparing AppImage: [bold]{path.name}[/bold]")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    display.success(f"AppImage ready: [bold]{path}[/bold]")
    return ArtifactInstallResult(
        success=True, artifact=artifact, local_path=path,
        run_command=str(path),
    )


def _install_tar(artifact: ArtifactInfo, path: Path) -> ArtifactInstallResult:
    stem = path.name
    for suffix in (".tar.gz", ".tgz"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break

    extract_dir = path.parent / stem
    extract_dir.mkdir(exist_ok=True)

    display.step(f"Extracting [bold]{path.name}[/bold]…")
    try:
        with tarfile.open(path, "r:gz") as tf:
            tf.extractall(extract_dir)
    except Exception as exc:
        return ArtifactInstallResult(
            success=False, artifact=artifact,
            error=f"Extraction failed: {exc}",
        )

    binary = _find_binary_in_dir(extract_dir)
    if not binary:
        return ArtifactInstallResult(
            success=False, artifact=artifact,
            error=f"No executable found in archive. Inspect: {extract_dir}",
        )

    display.success(f"Extracted binary: [bold]{binary}[/bold]")
    return ArtifactInstallResult(
        success=True, artifact=artifact, local_path=path,
        run_command=str(binary),
    )


def _install_zip(artifact: ArtifactInfo, path: Path) -> ArtifactInstallResult:
    extract_dir = path.parent / path.stem
    extract_dir.mkdir(exist_ok=True)

    display.step(f"Extracting [bold]{path.name}[/bold]…")
    try:
        with zipfile.ZipFile(path) as zf:
            zf.extractall(extract_dir)
    except Exception as exc:
        return ArtifactInstallResult(
            success=False, artifact=artifact,
            error=f"Extraction failed: {exc}",
        )

    binary = _find_binary_in_dir(extract_dir)
    if not binary:
        return ArtifactInstallResult(
            success=False, artifact=artifact,
            error=f"No executable found in archive. Inspect: {extract_dir}",
        )

    display.success(f"Extracted binary: [bold]{binary}[/bold]")
    return ArtifactInstallResult(
        success=True, artifact=artifact, local_path=path,
        run_command=str(binary),
    )


def _run_exe(artifact: ArtifactInfo, path: Path) -> ArtifactInstallResult:
    os_name = platform.system().lower()
    if os_name == "windows":
        return ArtifactInstallResult(
            success=True, artifact=artifact, local_path=path,
            run_command=str(path),
        )
    if shutil.which("wine"):
        display.warning("Running Windows .exe via Wine")
        return ArtifactInstallResult(
            success=True, artifact=artifact, local_path=path,
            run_command=f"wine {path}",
        )
    return ArtifactInstallResult(
        success=False, artifact=artifact,
        error=f"Cannot run .exe on {os_name} without Wine. Install Wine or use a Windows machine.",
    )


def _install_msi(artifact: ArtifactInfo, path: Path) -> ArtifactInstallResult:
    os_name = platform.system().lower()
    if os_name == "windows":
        return ArtifactInstallResult(
            success=True, artifact=artifact, local_path=path,
            run_command=f"msiexec /i \"{path}\"",
        )
    return ArtifactInstallResult(
        success=False, artifact=artifact,
        error=f"Cannot install .msi on {os_name}.",
    )


def _install_dmg(
    artifact: ArtifactInfo, path: Path, auto_approve: bool
) -> ArtifactInstallResult:
    if platform.system().lower() != "darwin":
        return ArtifactInstallResult(
            success=False, artifact=artifact,
            error="Cannot install .dmg outside macOS.",
        )

    display.step(f"Mounting [bold]{path.name}[/bold]…")
    proc = subprocess.run(
        ["hdiutil", "attach", str(path), "-nobrowse"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return ArtifactInstallResult(
            success=False, artifact=artifact, error="hdiutil attach failed"
        )

    mount_point: str | None = None
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            mount_point = parts[-1].strip()

    if not mount_point:
        return ArtifactInstallResult(
            success=False, artifact=artifact, error="Could not determine mount point"
        )

    apps = list(Path(mount_point).glob("*.app"))
    if not apps:
        subprocess.run(["hdiutil", "detach", mount_point], capture_output=True)
        return ArtifactInstallResult(
            success=False, artifact=artifact, error="No .app bundle found in disk image"
        )

    app_bundle = apps[0]
    dest = Path("/Applications") / app_bundle.name

    if not auto_approve:
        ok = display.prompt_confirm(
            f"Copy [bold]{app_bundle.name}[/bold] to /Applications (requires disk access)?"
        )
        if not ok:
            subprocess.run(["hdiutil", "detach", mount_point], capture_output=True)
            return ArtifactInstallResult(
                success=False, artifact=artifact, error="Installation declined by user"
            )

    display.step(f"Copying [bold]{app_bundle.name}[/bold] → /Applications…")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(str(app_bundle), str(dest))
    subprocess.run(["hdiutil", "detach", mount_point], capture_output=True)

    display.success(f"Installed [bold]{app_bundle.name}[/bold] to /Applications")
    return ArtifactInstallResult(
        success=True, artifact=artifact, local_path=path,
        run_command=f"open \"{dest}\"", installed_system=True,
    )


def _install_pkg(
    artifact: ArtifactInfo, path: Path, auto_approve: bool
) -> ArtifactInstallResult:
    if platform.system().lower() != "darwin":
        return ArtifactInstallResult(
            success=False, artifact=artifact,
            error="Cannot install .pkg outside macOS.",
        )

    display.step(f"Installing macOS package: [bold]{path.name}[/bold]")

    if not auto_approve:
        ok = display.prompt_confirm(
            f"This will run [bold]sudo installer -pkg {path.name}[/bold] (requires root). Proceed?"
        )
        if not ok:
            return ArtifactInstallResult(
                success=False, artifact=artifact, error="Installation declined by user"
            )

    result = subprocess.run(["sudo", "installer", "-pkg", str(path), "-target", "/"])
    if result.returncode != 0:
        return ArtifactInstallResult(
            success=False, artifact=artifact, error="macOS installer failed"
        )

    display.success("Package installed successfully")
    return ArtifactInstallResult(
        success=True, artifact=artifact, local_path=path, installed_system=True
    )


# ── Binary detection helpers ──────────────────────────────────────────────────

_BIN_DIRS = (
    "/usr/bin", "/usr/local/bin",
    "/usr/sbin", "/usr/local/sbin",
    "/bin", "/sbin",
    "/opt/bin",
)


def _deb_real_package_name(path: Path) -> str:
    """
    Read the Package: field from .deb metadata via ``dpkg --info``.
    The filename (e.g. yazi-x86_64_linux.deb) often differs from the installed
    package name (e.g. yazi), so we must not rely on the filename alone.
    Falls back to a filename-based heuristic if dpkg is unavailable.
    """
    try:
        proc = subprocess.run(
            ["dpkg", "--info", str(path)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line.lower().startswith("package:"):
                    return line.split(":", 1)[1].strip()
    except FileNotFoundError:
        pass

    # Filename fallback — Debian filenames follow name_version_arch.deb;
    # GitHub release assets often use name-arch-os.deb (no underscores in name part).
    stem = path.stem  # removes .deb

    if "_" in stem:
        # Debian convention: name_version_arch — first segment is the package name.
        # The name part itself may still contain arch tokens like "yazi-x86_64_unknown...",
        # so strip trailing arch suffixes from that first segment too.
        first = stem.split("_")[0]
    else:
        first = stem

    # Strip trailing architecture tokens (x86_64, aarch64, etc.) and generic OS tokens
    # (-linux, -gnu, -unknown).  Do NOT strip libc variants like -musl, -glibc because
    # those legitimately appear in package names (e.g. bat-musl).
    _ARCH_SUFFIX_RE = re.compile(
        r'[_-](x86[_-]64|x86_64|x86|amd64|aarch64|arm64|armhf|armv7|i386|i686'
        r'|linux|gnu|unknown|windows|darwin|macos|osx|static)[_-].*$'
        r'|[_-](x86[_-]64|x86_64|x86|amd64|aarch64|arm64|armhf|armv7|i386|i686)$',
        re.IGNORECASE,
    )
    cleaned = _ARCH_SUFFIX_RE.sub("", first).rstrip("-_")
    return cleaned or first


def _find_installed_binary(pkg_name: str) -> str | None:
    """
    Find the executable installed by a .deb/.rpm package.

    Strategy (in order):
      1. Ask dpkg -L for the exact list of installed files, look for bin/ entries.
      2. Check common bin directories for a file matching the package name.
      3. Use ``which`` to search the current PATH.
    Returns None if nothing is found (caller should warn the user).
    """
    # 1. Ask dpkg for the full file list — most accurate
    try:
        proc = subprocess.run(
            ["dpkg", "-L", pkg_name],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            executables: list[Path] = []
            for line in proc.stdout.splitlines():
                p = Path(line.strip())
                if (
                    p.parent.name in ("bin", "sbin")
                    and p.exists()
                    and os.access(str(p), os.X_OK)
                    and p.is_file()
                ):
                    executables.append(p)
            if executables:
                # Scoring: exact package-name match first, then longer names
                # (longer = less likely to be a tiny helper like "ya" vs "yazi").
                def _bin_score(p: Path) -> tuple[int, int]:
                    name = p.name
                    if name == pkg_name:
                        return (0, 0)             # exact match — best
                    if name.startswith(pkg_name):
                        return (1, -len(name))    # starts with pkg name
                    return (2, -len(name))         # unrelated — prefer longer
                executables.sort(key=_bin_score)
                return str(executables[0])
    except FileNotFoundError:
        pass

    # 2. Direct path check in common bin directories
    for prefix in _BIN_DIRS:
        candidate = Path(prefix) / pkg_name
        if candidate.exists() and os.access(str(candidate), os.X_OK):
            return str(candidate)

    # 3. Try with ``which`` — respects the current PATH
    found = shutil.which(pkg_name)
    if found:
        return found

    # 4. The package name might have a hyphen prefix like "yazi-x86" but binary is "yazi".
    #    Try progressively shorter stems.
    parts = re.split(r'[-_]', pkg_name)
    for i in range(len(parts) - 1, 0, -1):
        stem = "-".join(parts[:i])
        for prefix in _BIN_DIRS:
            candidate = Path(prefix) / stem
            if candidate.exists() and os.access(str(candidate), os.X_OK):
                return str(candidate)
        found = shutil.which(stem)
        if found:
            return found

    return None


def _find_binary_in_dir(directory: Path) -> Path | None:
    """Find the most likely main executable in an extracted archive directory."""
    os_name = platform.system().lower()
    candidates: list[tuple[int, Path]] = []

    for f in directory.rglob("*"):
        if not f.is_file():
            continue

        if os_name == "windows":
            if f.suffix.lower() == ".exe":
                depth = len(f.relative_to(directory).parts)
                candidates.append((10 - depth, f))
        else:
            mode = f.stat().st_mode
            if mode & (stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH):
                depth = len(f.relative_to(directory).parts)
                prio  = 10 - depth
                if f.parent.name in ("bin", ""):
                    prio += 5
                if f.suffix in (".sh", ".py", ".rb", ".js"):
                    prio -= 3  # prefer compiled binaries
                candidates.append((prio, f))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]
