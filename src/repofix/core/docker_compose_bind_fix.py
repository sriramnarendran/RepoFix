"""Fix Docker Compose bind mounts where a host path should be a file but is a directory.

Docker creates an empty directory when a bind source is missing; mounting it over
``/app/ov.conf`` (etc.) then triggers IsADirectoryError in the container.
"""

from __future__ import annotations

import json
import subprocess
import shutil
from pathlib import Path
from typing import Any

import yaml

from repofix.output import display

_COMPOSE_NAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

# Container paths that clearly expect a single file (not a directory mount).
_FILE_SUFFIXES = (".conf", ".json", ".yaml", ".yml", ".toml", ".env", ".ini", ".config")

_MINIMAL_JSON = "{}\n"


def _split_short_volume(vol: str) -> tuple[str, str] | None:
    s = vol.strip()
    if not s:
        return None
    if s.startswith("-"):
        s = s[1:].strip()
    for suf in (":ro", ":rw", ":z", ":Z"):
        if len(s) > len(suf) and s.endswith(suf):
            s = s[: -len(suf)]
            break
    if ":" not in s:
        return None
    i = s.index(":")
    host, container = s[:i], s[i + 1 :]
    if not host or not container:
        return None
    return host, container


def _iter_compose_files(repo_path: Path) -> list[Path]:
    return [repo_path / n for n in _COMPOSE_NAMES if (repo_path / n).is_file()]


def _volumes_from_service(svc: dict[str, Any]) -> list[Any]:
    raw = svc.get("volumes")
    if not raw:
        return []
    return list(raw) if isinstance(raw, list) else []


def _normalize_container_target(container_path: str) -> str:
    return container_path.rstrip("/") or "/"


def _container_looks_like_file(container_path: str) -> bool:
    c = _normalize_container_target(container_path)
    if c.endswith("/"):
        return False
    base = Path(c).name
    return base.startswith(".") or any(base.endswith(ext) for ext in _FILE_SUFFIXES)


def _resolve_host_path(repo_path: Path, host_raw: str) -> Path | None:
    h = host_raw.strip()
    if not h:
        return None
    if h.startswith("/"):
        return Path(h)
    if h == "." or h.startswith("./"):
        return (repo_path / h.removeprefix("./")).resolve()
    if h.startswith("~/"):
        return Path(h).expanduser()
    if h.startswith("../"):
        return (repo_path / h).resolve()
    if "/" not in h and "\\" not in h and not h.startswith("."):
        # Named volume (no path separators)
        return None
    return (repo_path / h).resolve()


def iter_bind_file_mounts(repo_path: Path) -> list[tuple[Path, str]]:
    """Return (host_path, container_path) for short binds where the container side looks like a file."""
    out: list[tuple[Path, str]] = []
    for cf in _iter_compose_files(repo_path):
        try:
            data = yaml.safe_load(cf.read_text()) or {}
        except Exception:
            continue
        services = data.get("services") or {}
        if not isinstance(services, dict):
            continue
        for _svc_name, svc in services.items():
            if not isinstance(svc, dict):
                continue
            for vol in _volumes_from_service(svc):
                host_s: str | None
                cont_s: str | None
                if isinstance(vol, str):
                    sp = _split_short_volume(vol)
                    if not sp:
                        continue
                    host_s, cont_s = sp
                elif isinstance(vol, dict):
                    if vol.get("type") not in (None, "bind"):
                        continue
                    host_s = vol.get("source")
                    cont_s = vol.get("target")
                    if not isinstance(host_s, str) or not isinstance(cont_s, str):
                        continue
                else:
                    continue
                if not _container_looks_like_file(cont_s):
                    continue
                rp = _resolve_host_path(repo_path, host_s)
                if rp is None:
                    continue
                out.append((rp, _normalize_container_target(cont_s)))
    return out


def _openviking_example_content(repo_path: Path) -> str | None:
    ex = repo_path / "examples" / "ov.conf.example"
    if not ex.is_file():
        return None
    try:
        data = json.loads(ex.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    storage = data.get("storage")
    if isinstance(storage, dict):
        storage["workspace"] = "/app/data"
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _default_file_body(repo_path: Path, container_path: str) -> str:
    c = _normalize_container_target(container_path).lower()
    if c.endswith("/ov.conf") or c.endswith("ov.conf"):
        body = _openviking_example_content(repo_path)
        if body:
            return body
    if c.endswith(".json") or c.endswith("/config.json"):
        return _MINIMAL_JSON
    if c.endswith(".conf"):
        return _MINIMAL_JSON
    if c.endswith(".yaml") or c.endswith(".yml"):
        return "{}\n"
    if c.endswith(".env"):
        return ""
    return _MINIMAL_JSON


def _write_bind_file(host: Path, body: str) -> bool:
    try:
        host.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        display.warning(
            f"Cannot create directory for bind-mount file {host}: {e}. "
            f"You may need: sudo mkdir -p {host.parent}"
        )
        return False
    try:
        host.write_text(body, encoding="utf-8")
        return True
    except OSError as e:
        display.warning(
            f"Cannot write bind-mount file {host}: {e}. "
            f"If this path is under /var/lib, try: sudo rm -rf {host}  then re-run."
        )
        return False


def repair_bind_host(repo_path: Path, host: Path, container_path: str) -> bool:
    """
    Ensure ``host`` is a regular file suitable for the given container bind.

    - If ``host`` is a directory (common Docker mistake), remove it and write a starter file.
    - If ``host`` does not exist, create parent dirs and write a starter file.
    - If ``host`` is already a file, return True.
    """
    if host.is_file():
        return True
    body = _default_file_body(repo_path, container_path)
    allow_sudo = container_path.rstrip("/").lower().endswith("ov.conf") and str(host).startswith(
        "/var/lib/openviking/"
    )

    if host.exists() and host.is_dir():
        iterdir_permission_error = False
        try:
            if any(host.iterdir()):
                display.warning(
                    f"Bind-mount path {host} is a non-empty directory — "
                    "remove it manually if it should be a config file."
                )
                return False
        except PermissionError:
            iterdir_permission_error = True
        except OSError:
            pass
        try:
            host.rmdir()
        except PermissionError as e:
            if not allow_sudo:
                display.warning(f"Permission denied removing {host}: {e}")
                return False
            if iterdir_permission_error:
                display.warning(f"Permission denied removing {host}; retrying with sudo…")
            try:
                subprocess.run(
                    ["sudo", "rm", "-rf", str(host)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except Exception as e2:
                display.warning(f"sudo rm -rf failed for {host}: {e2}")
                return False
        except OSError as e:
            try:
                shutil.rmtree(host)
            except OSError as e2:
                display.warning(f"Cannot remove directory {host}: {e} / {e2}")
                return False

    if host.exists() and host.is_dir():
        # Removal failed (e.g. sudo not permitted).
        return False

    ok = _write_bind_file(host, body)
    if ok:
        display.info(
            f"Prepared bind-mount file [bold]{host}[/bold] → container [bold]{container_path}[/bold]"
        )
        return True

    if allow_sudo:
        try:
            subprocess.run(
                ["sudo", "tee", str(host)],
                input=body.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
            )
            display.info(
                f"Prepared bind-mount file [bold]{host}[/bold] with sudo → container [bold]{container_path}[/bold]"
            )
            return True
        except Exception as e:
            display.warning(f"sudo tee failed for {host}: {e}")

    return ok


def ensure_docker_compose_bind_files(
    repo_path: Path,
    *,
    run_command: str | None = None,
    stack_is_docker: bool = False,
) -> None:
    """
    Before ``docker compose up``, fix host paths that must be files but are empty dirs.

    Idempotent when binds are already correct. Only runs when a compose file exists
    and the session is Docker-related (stack or run command).
    """
    if not _iter_compose_files(repo_path):
        return
    rc = (run_command or "").lower()
    dockerish = stack_is_docker or ("docker" in rc and "compose" in rc)
    if not dockerish:
        return

    for host, container_path in iter_bind_file_mounts(repo_path):
        if host.is_file():
            continue
        if host.is_dir() or not host.exists():
            display.step(
                f"Fixing Docker bind mount: [bold]{host}[/bold] "
                f"(must be a file for {container_path})"
            )
            repair_bind_host(repo_path, host, container_path)


def fix_host_for_container_path(repo_path: Path, container_path: str) -> bool:
    """
    Map a container path from logs (e.g. ``/app/ov.conf``) to a host path via compose
    and run :func:`repair_bind_host`.
    """
    want = _normalize_container_target(container_path)
    for host, cpath in iter_bind_file_mounts(repo_path):
        if cpath == want or cpath.endswith(want) or want.endswith(cpath):
            return repair_bind_host(repo_path, host, container_path)
    # Fallback: scan for basename match
    base = Path(want).name
    for host, cpath in iter_bind_file_mounts(repo_path):
        if Path(cpath).name == base:
            return repair_bind_host(repo_path, host, container_path)
    display.warning(
        f"No docker-compose bind mount found for container path {container_path!r} — "
        "edit docker-compose volumes manually."
    )
    return False
