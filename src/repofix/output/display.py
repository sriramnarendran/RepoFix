"""Rich terminal display helpers for repofix."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    Progress,
)
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

_THEME = Theme(
    {
        "info": "cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "muted": "dim white",
        "step": "bold blue",
        "fix": "bold magenta",
        "ai": "bold cyan",
    }
)

console = Console(theme=_THEME, highlight=False)


# ── Simple message helpers ────────────────────────────────────────────────────

def info(msg: str) -> None:
    console.print(f"[info]ℹ[/]  {msg}")


def success(msg: str) -> None:
    console.print(f"[success]✔[/]  {msg}")


def warning(msg: str) -> None:
    console.print(f"[warning]⚠[/]  {msg}")


def error(msg: str) -> None:
    console.print(f"[error]✘[/]  {msg}")


def step(msg: str) -> None:
    console.print(f"[step]→[/]  {msg}")


def fix_applied(msg: str) -> None:
    console.print(f"[fix]⚙[/]  {msg}")


def ai_action(msg: str) -> None:
    console.print(f"[ai]✦[/]  {msg}")


def muted(msg: str) -> None:
    console.print(f"[muted]{msg}[/]")


def rule() -> None:
    console.rule(style="dim")


_NPM_GLOBAL_FLAG_RE = re.compile(r"(?:^|\s)(-g|--global)(?:\s|$)")


def command_uses_npm_global_install(command: str) -> bool:
    """True if the shell command looks like npm/pnpm global package install."""
    if not command:
        return False
    c = command.lower()
    if not _NPM_GLOBAL_FLAG_RE.search(c):
        return False
    return "npm" in c or "pnpm" in c


def npm_global_cli_hint(repo_path: Path, *, npm_prefix_is_repo: bool) -> None:
    """
    Explain where global npm CLIs landed and how to run them outside repofix.
    See README § «Node.js: CLIs after npm install -g».
    """
    rp = str(repo_path)
    if npm_prefix_is_repo:
        lines = [
            "[bold]npm install -g[/bold] in this run used a [bold]clone-local[/bold] npm prefix — "
            "a normal shell will not have that CLI on PATH.",
            f"  [dim]•[/dim] Run the binary directly: [bold]{rp}/bin/<command>[/bold]",
            "  [dim]•[/dim] Search clones: "
            "[bold]find ~/.repofix/repos -path '*/bin/<command>'[/bold]",
            f"  [dim]•[/dim] Or: [bold]export PATH=\"{rp}/bin:$PATH\"[/bold]",
            "  [dim]•[/dim] Machine-wide install: [bold]npm install -g …[/bold] in a regular terminal.",
        ]
    else:
        lines = [
            "This flow ran [bold]npm install -g[/bold] — if you see [bold]command not found[/bold], "
            "your shell may be missing npm's global bin directory.",
            "  [dim]•[/dim] Try: [bold]export PATH=\"$(npm bin -g):$PATH\"[/bold] "
            "[dim](add to ~/.bashrc for persistence)[/dim]",
            f"  [dim]•[/dim] If you use repofix's isolated Node env, also check: [bold]{rp}/bin[/bold]",
        ]

    console.print(
        Panel(
            "\n".join(lines),
            title="[info]npm global CLI[/info]",
            border_style="cyan",
            padding=(0, 2),
        )
    )


# ── Panels ────────────────────────────────────────────────────────────────────

def banner() -> None:
    console.print(
        Panel.fit(
            "[bold white]RepoFix[/bold white]  [dim]— run any repo, instantly[/dim]",
            border_style="blue",
            padding=(0, 2),
        )
    )


def success_panel(url: str | None, summary: dict[str, str]) -> None:
    lines: list[str] = []
    if url:
        lines.append(f"[bold green]App running at:[/bold green]  [link={url}]{url}[/link]")
    for key, val in summary.items():
        lines.append(f"  [dim]{key}:[/dim] {val}")
    console.print(
        Panel(
            "\n".join(lines),
            title="[bold green]Success[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


def failure_panel(reason: str, suggestions: list[str]) -> None:
    lines = [f"[bold red]{reason}[/bold red]"]
    if suggestions:
        lines.append("")
        lines.append("[bold]Suggestions:[/bold]")
        for s in suggestions:
            lines.append(f"  • {s}")
    console.print(
        Panel(
            "\n".join(lines),
            title="[bold red]Failed[/bold red]",
            border_style="red",
            padding=(1, 2),
        )
    )


def partial_services_panel(
    url: str | None,
    running: dict[str, str],
    crashed_names: list[str],
    summary: dict[str, str],
) -> None:
    """Multi-service run: some processes stayed up, others exited during the warm-up window."""
    lines: list[str] = []
    lines.append(
        "[bold yellow]Not all services survived startup.[/bold yellow] "
        "[dim](See warnings above for each crashed service.)[/dim]"
    )
    if url:
        lines.append("")
        lines.append(f"[bold]Reachable:[/bold]  [link={url}]{url}[/link]")
    if running:
        lines.append("")
        lines.append("[bold green]Still running:[/bold green]")
        for name, link in running.items():
            lines.append(f"  [dim]{name}:[/dim] {link}")
    if crashed_names:
        lines.append("")
        lines.append("[bold red]Exited / crashed:[/bold red]")
        for name in crashed_names:
            lines.append(f"  • {name}")
    for key, val in summary.items():
        lines.append(f"  [dim]{key}:[/dim] {val}")
    console.print(
        Panel(
            "\n".join(lines),
            title="[bold yellow]Incomplete[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        )
    )


def info_panel(reason: str, suggestions: list[str]) -> None:
    """Neutral panel for outcomes that are not errors (e.g. CLI tool detected)."""
    lines = [f"[bold yellow]{reason}[/bold yellow]"]
    if suggestions:
        lines.append("")
        lines.append("[bold]Next steps:[/bold]")
        for s in suggestions:
            lines.append(f"  • {s}")
    console.print(
        Panel(
            "\n".join(lines),
            title="[bold yellow]Not a runnable service[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        )
    )


def cli_tool_ready_panel(
    tool_name: str,
    repo_path: str,
    venv_activate: str | None = None,
) -> None:
    """Success panel shown when a CLI tool is installed and ready to use."""
    lines: list[str] = [
        f"[bold green]{tool_name}[/bold green] installed successfully and is ready to use.",
        "",
    ]

    if venv_activate:
        # Venv must be activated before the tool will be on PATH — make this
        # the very first step so it can't be missed.
        lines += [
            "[bold yellow]⚠  The tool lives inside a virtual environment.[/bold yellow]",
            "   You must activate it first or the commands below won't work.",
            "",
            "[bold]To use it, open a terminal and run:[/bold]",
            "",
            f"  [bold cyan][1][/bold cyan]  cd [bold]{repo_path}[/bold]",
            f"  [bold cyan][2][/bold cyan]  source [bold]{venv_activate}[/bold]",
            f"  [bold cyan][3][/bold cyan]  [bold]{tool_name} --help[/bold]"
            "    [dim]← see all available subcommands[/dim]",
            f"  [bold cyan][4][/bold cyan]  [bold]{tool_name} <subcommand>[/bold]"
            "    [dim]← run a specific subcommand[/dim]",
        ]
    else:
        lines += [
            "[bold]To use it, open a terminal and run:[/bold]",
            "",
            f"  [bold cyan][1][/bold cyan]  cd [bold]{repo_path}[/bold]",
            f"  [bold cyan][2][/bold cyan]  [bold]{tool_name} --help[/bold]"
            "    [dim]← see all available subcommands[/dim]",
            f"  [bold cyan][3][/bold cyan]  [bold]{tool_name} <subcommand>[/bold]"
            "    [dim]← run a specific subcommand[/dim]",
        ]

    console.print(
        Panel(
            "\n".join(lines),
            title="[bold green]✔ CLI Tool Ready[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


def detection_panel(stack: dict[str, str]) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column()
    for key, val in stack.items():
        table.add_row(key, f"[bold]{val}[/bold]")
    console.print(
        Panel(table, title="[step]Stack Detected[/step]", border_style="blue", padding=(0, 1))
    )


def fix_panel(attempt: int, error_type: str, fix_desc: str, source: str) -> None:
    console.print(
        Panel(
            f"[dim]Attempt {attempt}[/dim]  [bold]{error_type}[/bold]\n"
            f"[fix]Fix:[/fix] {fix_desc}  [dim]({source})[/dim]",
            title="[fix]Applying Fix[/fix]",
            border_style="magenta",
            padding=(0, 2),
        )
    )


def batch_fix_panel(fixes: list[tuple[str, str, str]]) -> None:
    """
    Display a panel listing all fixes that will be applied in the last-resort batch pass.

    Args:
        fixes: list of (error_type, description, source) tuples.
    """
    lines: list[str] = [
        f"[bold]Applying {len(fixes)} fix{'es' if len(fixes) != 1 else ''} before final retry:[/bold]",
        "",
    ]
    source_color = {"rule": "cyan", "memory": "green", "ai": "yellow", "so": "blue"}
    for error_type, desc, source in fixes:
        color = source_color.get(source, "white")
        lines.append(
            f"  [fix]⚙[/fix]  [bold]{error_type}[/bold]  [dim]({source})[/dim]"
        )
        lines.append(f"     [{color}]{desc}[/{color}]")
    console.print(
        Panel(
            "\n".join(lines),
            title="[fix]Last-Resort Batch Fix[/fix]",
            border_style="magenta",
            padding=(1, 2),
        )
    )


# ── Spinners / progress ───────────────────────────────────────────────────────

@contextmanager
def spinner(label: str) -> Generator[None, None, None]:
    with console.status(f"[cyan]{label}[/cyan]", spinner="dots"):
        yield


@contextmanager
def live_step(label: str) -> Generator[None, None, None]:
    """Show a spinner + elapsed time while a long-running step executes.

    Log lines printed via ``log_line()`` appear above the spinner so it acts
    as a persistent "still alive" indicator during silent stretches (e.g.
    Gradle resolving dependencies with no output for minutes at a time).
    Uses the same ``console`` instance as ``log_line()``, so Rich handles
    thread-safe interleaving automatically.
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}[/bold blue]"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
        refresh_per_second=4,
    ) as progress:
        progress.add_task(label, total=None)
        yield


def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


# ── History / run tables ──────────────────────────────────────────────────────

def runs_table(rows: list[dict]) -> None:
    table = Table(title="Run History", border_style="blue", header_style="bold")
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("Stack")
    table.add_column("Status")
    table.add_column("Fixes", justify="right")
    table.add_column("Duration")
    table.add_column("When")

    for row in rows:
        status_text = Text("✔ success", style="green") if row.get("success") else Text("✘ failed", style="red")
        table.add_row(
            row.get("repo_url", ""),
            row.get("stack", ""),
            status_text,
            str(row.get("fix_count", 0)),
            f"{row.get('duration_s', 0):.1f}s",
            row.get("when", ""),
        )
    console.print(table)


def log_line(line: str, source: str = "stdout") -> None:
    """Print a single log line streamed from the running process."""
    prefix = "[dim cyan]│[/dim cyan]" if source == "stdout" else "[dim red]│[/dim red]"
    # escape() so paths like [/tmp/lint] are not parsed as Rich markup tags
    console.print(f"{prefix} {escape(line)}", highlight=False)


def log_line_labeled(line: str, label: str, color: str = "cyan", source: str = "stdout") -> None:
    """Print a log line prefixed with a service name label."""
    bar_color = color if source == "stdout" else "red"
    label_fmt = f"[bold {color}]{label:<12}[/bold {color}]"
    bar = f"[dim {bar_color}]│[/dim {bar_color}]"
    console.print(f"{label_fmt} {bar} {escape(line)}", highlight=False)


def multi_service_panel(services: list[dict]) -> None:
    """Show a summary panel listing all detected services."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column()
    table.add_column(style="dim")
    for svc in services:
        color = svc.get("color", "cyan")
        table.add_row(
            svc.get("role", "").upper(),
            f"[bold {color}]{svc['name']}[/bold {color}]",
            svc.get("path", ""),
        )
    console.print(
        Panel(table, title="[step]Multi-Service Repo Detected[/step]", border_style="blue", padding=(0, 1))
    )


def processes_table(entries: list) -> None:
    """Display a table of registered processes (from process_registry)."""
    import time as _time

    if not entries:
        info("No processes registered yet. Run [bold]repofix run <repo>[/bold] to start one.")
        return

    table = Table(title="Running Processes", border_style="blue", header_style="bold")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Stack")
    table.add_column("URL", style="cyan")
    table.add_column("PID", justify="right")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Uptime")

    for entry in entries:
        status = entry.status
        if status == "running":
            status_text = Text("● running", style="bold green")
        elif status == "stopped":
            status_text = Text("○ stopped", style="dim white")
        else:
            status_text = Text("✘ crashed", style="bold red")

        uptime = ""
        if entry.status == "running" and entry.uptime_s() is not None:
            secs = int(entry.uptime_s())
            h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
            uptime = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

        started = ""
        if entry.started_at:
            import datetime
            started = datetime.datetime.fromtimestamp(entry.started_at).strftime("%H:%M:%S")

        table.add_row(
            entry.name,
            entry.stack or "unknown",
            entry.app_url or "—",
            str(entry.pid),
            status_text,
            started,
            uptime or "—",
        )

    console.print(table)


def prompt_confirm(question: str) -> bool:
    answer = console.input(f"[bold yellow]?[/bold yellow] {question} [dim](y/N)[/dim] ")
    return answer.strip().lower() in {"y", "yes"}


# ── Branch cache display ──────────────────────────────────────────────────────

def branch_cache_hit(branch: str, dep_hash_short: str, installed_when: str) -> None:
    """Show a cache-hit notice for the current branch."""
    console.print(
        f"[success]⚡[/]  Branch [bold cyan]{branch}[/bold cyan] — "
        f"deps cached [dim]({dep_hash_short})[/dim]  "
        f"[dim]last installed {installed_when}[/dim]"
    )


def branch_cache_miss(branch: str, reason: str = "no cache yet") -> None:
    """Show a cache-miss notice for the current branch."""
    console.print(
        f"[step]⎇[/]  Branch [bold cyan]{branch}[/bold cyan] — "
        f"[dim]{reason}[/dim]"
    )


def branches_table(states: list[dict]) -> None:
    """Render a table of cached branch states."""
    if not states:
        info("No branch caches recorded yet.")
        return

    table = Table(
        title="Cached Branch Environments",
        border_style="blue",
        header_style="bold",
    )
    table.add_column("Repo", style="dim", max_width=40, no_wrap=True)
    table.add_column("Branch", style="bold cyan", no_wrap=True)
    table.add_column("Dep Hash", style="dim", no_wrap=True)
    table.add_column("Dep Files", style="dim", max_width=36)
    table.add_column("Env Dir", style="dim", max_width=36, no_wrap=True)
    table.add_column("Installed", no_wrap=True)
    table.add_column("Status")

    for s in states:
        import json as _json

        # Shorten repo key to last two path segments / owner+repo
        repo_key = s.get("repo_key", "")
        parts = repo_key.rstrip("/").split("/")
        repo_short = "/".join(parts[-2:]) if len(parts) >= 2 else repo_key

        dep_hash_short = s.get("dep_hash", "")[:8]

        raw_files = s.get("dep_files", "[]")
        try:
            files_list: list[str] = _json.loads(raw_files)
        except Exception:
            files_list = []
        dep_files_str = ", ".join(files_list[:3])
        if len(files_list) > 3:
            dep_files_str += f" +{len(files_list) - 3}"

        env_dir = s.get("env_dir", "")
        # Show only the last two path parts to keep it short
        env_parts = env_dir.rstrip("/").split("/")
        env_short = "/".join(env_parts[-2:]) if len(env_parts) >= 2 else env_dir

        ok = s.get("install_success", True)
        status_text = Text("✔ ok", style="bold green") if ok else Text("✘ failed", style="bold red")

        table.add_row(
            repo_short,
            s.get("branch", ""),
            dep_hash_short,
            dep_files_str or "—",
            env_short or "—",
            s.get("installed_when", ""),
            status_text,
        )

    console.print(table)


def prompt_value(name: str, default: str = "") -> str:
    hint = f"[dim](default: {default})[/dim] " if default else ""
    value = console.input(f"[bold yellow]?[/bold yellow] Enter value for [bold]{name}[/bold]: {hint}")
    return value.strip() or default


# ── Artifact panels ───────────────────────────────────────────────────────────

def artifacts_panel(scan: "ArtifactScan") -> None:  # type: ignore[name-defined]
    """Display a panel listing available prebuilt artifacts for the current OS."""
    from repofix.detection.artifacts import ArtifactScan, format_label  # local import

    lines: list[str] = []

    if scan.release_tag:
        rel_label = scan.release_name or scan.release_tag
        lines.append(f"[dim]Release:[/dim] [bold]{rel_label}[/bold]  [dim]({scan.release_tag})[/dim]")
        lines.append("")

    os_display = {"linux": "Linux", "windows": "Windows", "darwin": "macOS"}.get(
        scan.os_system, scan.os_system.capitalize()
    )
    arch_display = scan.os_arch
    lines.append(f"[dim]Platform:[/dim] {os_display} / {arch_display}")
    lines.append("")
    lines.append("[bold]Available binaries:[/bold]")

    for i, a in enumerate(scan.available[:6]):  # show at most 6
        size_hint = f"  [dim]{a.size_bytes // (1024*1024)} MB[/dim]" if a.size_bytes > 0 else ""
        src_hint  = f"  [dim]({a.source.replace('_', ' ')})[/dim]"
        star      = " [bold yellow]★ best match[/bold yellow]" if i == 0 else ""
        lines.append(
            f"  [cyan]•[/cyan] [bold]{a.name}[/bold]{size_hint}{src_hint}{star}"
        )
        lines.append(f"    [dim]{format_label(a.format)}[/dim]")

    console.print(
        Panel(
            "\n".join(lines),
            title="[bold cyan]Prebuilt Binary Available[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def deploy_mode_panel(opts: "DeployModeOptions") -> None:  # type: ignore[name-defined]
    """Display a panel listing available deployment modes (prod vs dev)."""
    from repofix.detection.deploy_mode import DeployModeOptions  # local import

    lines: list[str] = []
    lines.append("[bold]This repo supports multiple deployment modes:[/bold]")
    lines.append("")

    for i, mode in enumerate(opts.modes, 1):
        key_color = "green" if mode.key == "prod" else "cyan"
        lines.append(
            f"  [bold {key_color}][{i}][/bold {key_color}]  "
            f"[bold]{mode.label}[/bold]"
            f"  [dim]({mode.source})[/dim]"
        )
        lines.append(f"       [dim]{mode.description}[/dim]")
        if mode.prerequisites:
            prereqs = ", ".join(mode.prerequisites)
            lines.append(f"       [dim]Requires: {prereqs}[/dim]")
        if mode.steps:
            for step in mode.steps[:4]:
                marker = "[dim]⚙[/dim]" if step.interactive else "[dim]$[/dim]"
                lines.append(f"         {marker} [dim]{step.command[:72]}[/dim]")
            if len(mode.steps) > 4:
                lines.append(f"         [dim]… and {len(mode.steps) - 4} more steps[/dim]")
        lines.append("")

    console.print(
        Panel(
            "\n".join(lines).rstrip(),
            title="[bold green]Deployment Mode Detected[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )


def prompt_deploy_mode(opts: "DeployModeOptions") -> str:  # type: ignore[name-defined]
    """
    Ask the user to choose a deployment mode.
    Returns the key of the chosen mode (e.g. "prod" or "dev").
    """
    mode_keys = [m.key for m in opts.modes]
    n = len(mode_keys)

    while True:
        raw = console.input(
            f"[bold yellow]?[/bold yellow] Choose a deployment mode "
            f"[dim](1–{n})[/dim]: "
        ).strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < n:
                return mode_keys[idx]
        except ValueError:
            pass
        # Also accept key names directly ("prod", "dev")
        if raw in mode_keys:
            return raw
        console.print(f"[dim]Please enter a number between 1 and {n}[/dim]")


def prompt_npm_global_scope(*, auto_approve: bool) -> str:
    """
    Ask where ``npm install -g`` / ``pnpm add -g`` should put the CLI.

    Returns:
        ``\"isolated\"`` — clone-local prefix (repo ``bin/``, no admin).
        ``\"machine\"`` — npm's default global prefix for the current user.
    """
    if auto_approve:
        return "isolated"
    console.print(
        "\n[bold cyan]Global npm/pnpm install (-g) detected[/bold cyan]\n\n"
        "  [bold cyan][1][/bold cyan]  [bold]Isolated[/bold] — CLI under this repo checkout "
        "[dim](no sudo; add repo [bold]bin[/bold] to PATH to use the tool elsewhere)[/dim]\n"
        "  [bold cyan][2][/bold cyan]  [bold]Machine-wide[/bold] — your normal npm global prefix "
        "[dim](on Linux may need sudo or a user-level npm prefix)[/dim]\n"
    )
    while True:
        raw = console.input(
            "[bold yellow]?[/bold yellow] Install scope [dim](1–2, default 1)[/dim]: "
        ).strip() or "1"
        if raw in ("1", "i", "I", "isolated"):
            return "isolated"
        if raw in ("2", "m", "M", "machine", "machine-wide", "global"):
            return "machine"
        console.print("[dim]Enter 1 (isolated) or 2 (machine-wide)[/dim]")


def prompt_npm_global_prefix_unwritable(*, auto_approve: bool) -> str:
    """
    The default npm global prefix is not writable (e.g. ``/usr/local`` on Linux).

    Returns:
        ``\"isolated\"`` — use a clone-local ``npm_config_prefix``.
        ``\"sudo\"`` — run global install command(s) with ``sudo`` (user must approve).
        ``\"abort\"`` — stop the run.
    """
    if auto_approve:
        return "isolated"
    console.print(
        "\n[bold yellow]npm's default global directory is not writable[/bold yellow] "
        "[dim](typical on Linux when the prefix is /usr/local).[/dim]\n\n"
        "  [bold cyan][1][/bold cyan]  [bold]Isolated[/bold] — install under this repo checkout "
        "[dim](recommended; no elevated permissions)[/dim]\n"
        "  [bold cyan][2][/bold cyan]  [bold]sudo[/bold] — run the [bold]npm install -g[/bold] step(s) with "
        "[bold]sudo[/bold] [dim](password prompt; only if you trust this repo)[/dim]\n"
        "  [bold cyan][3][/bold cyan]  [bold]Abort[/bold] — cancel this run\n"
    )
    while True:
        raw = console.input(
            "[bold yellow]?[/bold yellow] How to proceed [dim](1–3, default 1)[/dim]: "
        ).strip() or "1"
        if raw in ("1", "i", "I", "isolated"):
            return "isolated"
        if raw in ("2", "s", "S", "sudo"):
            return "sudo"
        if raw in ("3", "a", "A", "abort", "q", "quit", "cancel"):
            return "abort"
        console.print("[dim]Enter 1 (isolated), 2 (sudo), or 3 (abort)[/dim]")


def non_runnable_panel(repo_type: str, details: dict) -> None:
    """Show an informational panel for repos that are not meant to be run as services."""
    if repo_type == "agent_plugin":
        platforms: list[str] = details.get("platforms", [])
        platform_str = ", ".join(p.capitalize() for p in platforms) if platforms else "your coding agent"
        lines: list[str] = [
            "[bold yellow]This repo is an AI agent plugin / skills framework.[/bold yellow]",
            "",
            "It is designed to be [bold]installed into a coding agent[/bold], not run directly.",
            "",
            f"[dim]Supports:[/dim]  [bold]{platform_str}[/bold]",
            "",
            "[bold]Installation (examples):[/bold]",
        ]
        install_hints = {
            "claude": "  [dim]•[/dim] Claude Code:  [bold]/plugin install <name>[/bold]",
            "cursor": "  [dim]•[/dim] Cursor:       [bold]/add-plugin <name>[/bold]",
            "opencode": "  [dim]•[/dim] OpenCode:     follow [bold].opencode/INSTALL.md[/bold]",
            "codex": "  [dim]•[/dim] Codex:        follow [bold].codex/INSTALL.md[/bold]",
        }
        shown = False
        for p in platforms:
            hint = install_hints.get(p.lower())
            if hint:
                lines.append(hint)
                shown = True
        if not shown:
            lines.append("  [dim]•[/dim] See the [bold]README[/bold] for installation instructions")
    else:
        lines = [
            "[bold yellow]This repo does not appear to be a runnable service.[/bold yellow]",
            "",
            "Check the README for usage instructions.",
        ]

    console.print(
        Panel(
            "\n".join(lines),
            title="[bold yellow]Not a Runnable Service[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        )
    )


def cli_needs_args_panel(base_cmd: str, usage_synopsis: str, options_preview: list[str]) -> None:
    """Show a panel when a CLI tool printed usage/help and needs positional arguments."""
    lines: list[str] = [
        f"[bold yellow]{base_cmd}[/bold yellow] is a CLI tool that requires arguments.",
    ]
    if usage_synopsis:
        lines += ["", f"[dim]Usage:[/dim]  [bold]{base_cmd} {usage_synopsis}[/bold]"]
    if options_preview:
        lines += ["", "[bold]Available options (sample):[/bold]"]
        for opt in options_preview[:8]:
            lines.append(f"  [cyan]•[/cyan] [dim]{opt}[/dim]")
    console.print(
        Panel(
            "\n".join(lines),
            title="[bold yellow]CLI Tool — Arguments Required[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        )
    )


def prompt_cli_args(base_cmd: str) -> str:
    """Ask the user to enter arguments for a CLI tool. Returns the full command."""
    raw = console.input(
        f"\n[bold yellow]?[/bold yellow] Enter arguments for "
        f"[bold]{base_cmd}[/bold] [dim](leave blank to skip)[/dim]: "
    ).strip()
    return f"{base_cmd} {raw}" if raw else base_cmd


def prompt_install_mode() -> str:
    """
    Ask the user whether to install the prebuilt binary or run from source.
    Returns "binary" or "source".
    """
    console.print(
        "\n  [bold cyan][1][/bold cyan]  Install / run the [bold]prebuilt binary[/bold]"
        "  [dim](faster — no build step)[/dim]"
    )
    console.print(
        "  [bold cyan][2][/bold cyan]  Clone & run [bold]from source[/bold]"
        "  [dim](build from code)[/dim]\n"
    )
    while True:
        raw = console.input(
            "[bold yellow]?[/bold yellow] Choose an option [dim](1/2)[/dim]: "
        ).strip()
        if raw == "1":
            return "binary"
        if raw == "2":
            return "source"
        console.print("[dim]Please enter 1 or 2[/dim]")
