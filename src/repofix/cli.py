"""CLI entry point — all commands and flags."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from repofix import config as cfg
from repofix.output import display

app = typer.Typer(
    name="repofix",
    help="Run any GitHub repo locally with one command.",
    add_completion=True,
    rich_markup_mode="rich",
    no_args_is_help=True,
)


# ── One-time local model setup ────────────────────────────────────────────────

def _auto_setup_local_llm() -> None:
    """
    On the very first `repofix run`, ask the user whether they want
    advanced on-device AI (Qwen2.5-Coder-3B via llama-cpp-python).

    - If YES  → install llama-cpp-python (pre-built wheel) + download model
                (~2 GB), save use_local_llm=True — never asked again.
    - If NO   → save use_local_llm=False; cloud AI (if any) is configured
                in the separate first-run prompt — never asked again.

    On subsequent runs the saved preference is honoured instantly.
    Works on Linux, macOS, and Windows.
    """
    try:
        from repofix.fixing import local_llm

        runner_cfg = cfg.load()

        # Already answered — honour the saved preference.
        if runner_cfg.local_llm_prompted:
            if runner_cfg.use_local_llm and not local_llm.is_downloaded():
                display.ai_action("Local model not found — re-downloading…")
                local_llm.ensure_ready()
            return

        # ── First-ever run: ask the user ──────────────────────────────────────
        display.rule()
        display.console.print(
            "\n[bold cyan]✦  Advanced AI detection & fixing[/bold cyan]\n\n"
            "  repofix can use a [bold]local Qwen2.5-Coder-3B[/bold] model "
            "(runs entirely on your machine, no API key needed)\n"
            "  for smarter error analysis and stack detection.\n\n"
            "  One-time setup:\n"
            "    • llama-cpp-python  [dim]pre-built wheel, no compilation[/dim]\n"
            f"    • Model weights     [dim]~{local_llm._MODEL_SIZE_GB:.0f} GB[/dim]"
            "  → [dim]~/.repofix/models/[/dim]\n\n"
            "  Change this later: "
            "[bold]repofix config set-default --local-llm / --no-local-llm[/bold]\n"
        )

        want_local = typer.confirm(
            "  Enable on-device AI? (recommended)",
            default=True,
        )

        runner_cfg.local_llm_prompted = True
        runner_cfg.use_local_llm = want_local
        cfg.save(runner_cfg)

        if want_local:
            display.rule()
            local_llm.ensure_ready()
        else:
            display.info(
                "On-device AI skipped. "
                "You will be asked next about [bold]cloud AI[/bold] (Gemini, OpenAI, or Anthropic); "
                "re-enable local model anytime: [bold]repofix config set-default --local-llm[/bold]"
            )

        display.rule()

    except KeyboardInterrupt:
        display.warning(
            "On-device AI setup cancelled. "
            "You can still set up cloud AI when prompted, or run: [bold]repofix config set-key[/bold]"
        )
        try:
            runner_cfg = cfg.load()
            runner_cfg.local_llm_prompted = True
            runner_cfg.use_local_llm = False
            cfg.save(runner_cfg)
        except Exception:
            pass
    except Exception as exc:
        display.warning(f"Local LLM setup failed (continuing without it): {exc}")


def _optional_cloud_ai_setup() -> None:
    """
    After local-LLM setup, optionally prompt once to store a cloud API key
    (Gemini, OpenAI, or Anthropic) for stronger fixes when local inference fails.
    """
    runner_cfg = cfg.load()
    if runner_cfg.ai_cloud_setup_prompted:
        return
    if cfg.any_cloud_ai_configured():
        runner_cfg.ai_cloud_setup_prompted = True
        cfg.save(runner_cfg)
        return
    try:
        need_cloud_without_local = not runner_cfg.use_local_llm and not cfg.any_cloud_ai_configured()
        display.rule()
        display.console.print(
            "\n[bold cyan]✦  Cloud AI (optional)[/bold cyan]\n\n"
            "  For harder errors, repofix can call [bold]OpenAI[/bold], [bold]Anthropic[/bold], "
            "or [bold]Google Gemini[/bold].\n"
            "  Skip now and configure later:\n"
            "    [bold]repofix config set-key -p openai[/bold]  "
            "[dim](or anthropic / gemini)[/dim]\n"
        )
        if not typer.confirm(
            "  Add a cloud API key now?",
            default=need_cloud_without_local,
        ):
            runner_cfg.ai_cloud_setup_prompted = True
            cfg.save(runner_cfg)
            display.rule()
            return

        while True:
            prov = typer.prompt("  Provider (gemini / openai / anthropic)").strip().lower()
            if prov in ("gemini", "openai", "anthropic"):
                break
            display.warning("Enter one of: gemini, openai, anthropic.")

        key = typer.prompt("  API key", hide_input=True)
        if prov == "gemini":
            cfg.set_gemini_key(key)
        elif prov == "openai":
            cfg.set_openai_key(key)
        else:
            cfg.set_anthropic_key(key)

        runner_cfg = cfg.load()
        runner_cfg.ai_cloud_provider = prov if prov != "gemini" else "auto"
        runner_cfg.ai_cloud_setup_prompted = True
        cfg.save(runner_cfg)
        display.success(f"{prov.title()} API key saved to ~/.repofix/config.toml")
        display.rule()
    except KeyboardInterrupt:
        display.warning("Cloud setup cancelled.")
        try:
            runner_cfg = cfg.load()
            runner_cfg.ai_cloud_setup_prompted = True
            cfg.save(runner_cfg)
        except Exception:
            pass
        display.rule()


# ── repofix run ───────────────────────────────────────────────────────────

@app.command()
def run(
    repo: Annotated[str, typer.Argument(help="GitHub URL or local path to the repo")],
    branch: Annotated[Optional[str], typer.Option("--branch", "-b", help="Git branch to checkout")] = None,
    mode: Annotated[str, typer.Option("--mode", "-m", help="auto | assist | debug")] = "auto",
    port: Annotated[Optional[int], typer.Option("--port", "-p", help="Override the default port")] = None,
    retries: Annotated[int, typer.Option("--retries", "-r", help="Max fix/retry cycles")] = 5,
    env_file: Annotated[Optional[Path], typer.Option("--env-file", "-e", help="Path to a .env file")] = None,
    no_fix: Annotated[bool, typer.Option("--no-fix", help="Disable automated fixes")] = False,
    auto_approve: Annotated[bool, typer.Option("--auto-approve", help="Skip all confirmation prompts")] = False,
    command: Annotated[Optional[str], typer.Option("--command", "-c", help="Override the run command")] = None,
    install: Annotated[Optional[str], typer.Option("--install", "-i", help="Override the install command")] = None,
    binary: Annotated[bool, typer.Option("--binary", help="Use prebuilt binary if available (skip source build)")] = False,
    source: Annotated[bool, typer.Option("--source", help="Always run from source (skip binary check)")] = False,
    prod: Annotated[bool, typer.Option("--prod", help="Use production/self-hosting deployment (skip dev prompt)")] = False,
    dev: Annotated[bool, typer.Option("--dev", help="Use local development mode (skip prod prompt)")] = False,
) -> None:
    """
    Clone (or use) a repo, detect its stack, install dependencies, and run it.

    When a repo has both a self-hosting path and a local-dev path (detected from
    the README), you will be asked which to use.  Use --prod / --dev to skip that
    prompt.  When a prebuilt binary is available on GitHub Releases you will be
    asked whether to install it; use --binary / --source to skip that prompt.

    Examples:

      repofix run https://github.com/user/my-app

      repofix run https://github.com/elie222/inbox-zero --prod

      repofix run https://github.com/elie222/inbox-zero --dev

      repofix run ./local-project --mode assist

      repofix run https://github.com/user/api --branch develop --port 8080

      repofix run https://github.com/user/app --binary
    """
    if mode not in ("auto", "assist", "debug"):
        display.error("--mode must be one of: auto, assist, debug")
        raise typer.Exit(1)

    if binary and source:
        display.error("--binary and --source are mutually exclusive")
        raise typer.Exit(1)

    if prod and dev:
        display.error("--prod and --dev are mutually exclusive")
        raise typer.Exit(1)

    display.banner()
    _auto_setup_local_llm()
    _optional_cloud_ai_setup()

    app_cfg = cfg.load()
    effective_retries      = retries if retries != 5 else app_cfg.default_retries
    effective_auto_approve = auto_approve or app_cfg.auto_approve

    install_mode: Optional[str] = None
    if binary:
        install_mode = "binary"
    elif source:
        install_mode = "source"

    deploy_mode: Optional[str] = None
    if prod:
        deploy_mode = "prod"
    elif dev:
        deploy_mode = "dev"

    from repofix.core.git import GitError, resolve_repo
    from repofix.core.runner import RunOptions, run as do_run

    options = RunOptions(
        branch=branch,
        mode=mode,
        port=port,
        max_retries=effective_retries,
        env_file=env_file,
        no_fix=no_fix,
        auto_approve=effective_auto_approve,
        override_command=command,
        override_install=install,
        install_mode=install_mode,
        deploy_mode=deploy_mode,
    )

    try:
        with display.spinner("Preparing repository…"):
            repo_path = resolve_repo(repo, branch)
    except GitError as exc:
        display.error(str(exc))
        raise typer.Exit(1)

    result = do_run(repo_path, repo, options)

    if not result.success:
        raise typer.Exit(1)


# ── repofix completion ────────────────────────────────────────────────────────

completion_app = typer.Typer(
    help="Shell tab completion for subcommands and options (bash, zsh, fish, PowerShell).",
    no_args_is_help=True,
)
app.add_typer(completion_app, name="completion")

_COMPLETION_SHELLS = frozenset({"bash", "zsh", "fish", "powershell", "pwsh"})


def _completion_install_impl(shell: str | None) -> None:
    from typer._completion_shared import install as typer_install_completion

    shell_name, path = typer_install_completion(
        shell=shell,
        prog_name="repofix",
        complete_var="_REPOFIX_COMPLETE",
    )
    display.success(f"{shell_name} completion installed → {path}")
    display.info("Open a new terminal tab/window (or reload your shell config) for completion to take effect.")


def _completion_show_impl(shell: str | None) -> None:
    from typer._completion_shared import _get_shell_name, get_completion_script

    resolved: str
    if shell:
        resolved = shell.lower().strip()
    else:
        resolved = _get_shell_name() or "bash"
    if resolved not in _COMPLETION_SHELLS:
        display.error(f"Unsupported shell {resolved!r}. Use: {', '.join(sorted(_COMPLETION_SHELLS))}")
        raise typer.Exit(1)
    script = get_completion_script(
        prog_name="repofix",
        complete_var="_REPOFIX_COMPLETE",
        shell=resolved,
    )
    print(script)


@completion_app.command("install")
def completion_install(
    shell: Annotated[
        Optional[str],
        typer.Argument(help="bash | zsh | fish | powershell | pwsh — default: detect from your terminal"),
    ] = None,
) -> None:
    """Install tab completion (same as `repofix --install-completion`, easier to discover)."""
    s = shell.lower().strip() if shell else None
    if s and s not in _COMPLETION_SHELLS:
        display.error(f"Unsupported shell {shell!r}. Choose one of: {', '.join(sorted(_COMPLETION_SHELLS))}")
        raise typer.Exit(1)
    _completion_install_impl(s)


@completion_app.command("show")
def completion_show(
    shell: Annotated[
        Optional[str],
        typer.Argument(help="bash | zsh | fish | powershell | pwsh — default: detect"),
    ] = None,
) -> None:
    """Print the completion script to stdout (for manual install or customization)."""
    s = shell.lower().strip() if shell else None
    if s and s not in _COMPLETION_SHELLS:
        display.error(f"Unsupported shell {shell!r}. Choose one of: {', '.join(sorted(_COMPLETION_SHELLS))}")
        raise typer.Exit(1)
    _completion_show_impl(s)


# ── repofix config ────────────────────────────────────────────────────────

config_app = typer.Typer(help="Manage repofix configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("set-key")
def config_set_key(
    key: Annotated[Optional[str], typer.Argument(help="API key (omit to enter interactively)")] = None,
    provider: Annotated[
        str,
        typer.Option("--provider", "-p", help="gemini | openai | anthropic"),
    ] = "gemini",
) -> None:
    """Save an API key for AI-powered error analysis (Gemini, OpenAI, or Anthropic)."""
    p = provider.strip().lower()
    if p not in ("gemini", "openai", "anthropic"):
        display.error("--provider must be one of: gemini, openai, anthropic")
        raise typer.Exit(1)
    label = {"gemini": "Gemini", "openai": "OpenAI", "anthropic": "Anthropic"}[p]
    if not key:
        key = typer.prompt(f"Enter your {label} API key", hide_input=True)
    if p == "gemini":
        cfg.set_gemini_key(key)
    elif p == "openai":
        cfg.set_openai_key(key)
    else:
        cfg.set_anthropic_key(key)
    display.success(f"{label} API key saved to ~/.repofix/config.toml")


@config_app.command("show")
def config_show() -> None:
    """Show current configuration."""
    from repofix.fixing import local_llm

    current = cfg.load()

    local_llm_status: str
    if not current.use_local_llm:
        local_llm_status = "disabled"
    elif not local_llm.is_available():
        local_llm_status = "✘ llama-cpp-python not installed  (run: repofix model download)"
    elif not local_llm.is_downloaded():
        local_llm_status = "✘ model not downloaded  (run: repofix model download)"
    else:
        size_gb = local_llm.model_path().stat().st_size / 1_073_741_824
        local_llm_status = f"✔ ready  ({size_gb:.1f} GB)"

    def _ai_line(file_nonempty: bool, env_name: str, getter) -> str:
        if not getter():
            return "✘ not set"
        src = "config" if file_nonempty else f"env {env_name}"
        return f"✔ ({src})"

    display.detection_panel({
        "Mode": current.default_mode,
        "Max retries": str(current.default_retries),
        "Auto-approve": str(current.auto_approve),
        "AI primary (cloud)": current.ai_cloud_provider,
        "Cloud fallback": str(current.ai_cloud_fallback),
        "Gemini": _ai_line(bool(current.gemini_api_key.strip()), "GEMINI_API_KEY", cfg.get_gemini_key),
        "OpenAI": _ai_line(bool(current.openai_api_key.strip()), "OPENAI_API_KEY", cfg.get_openai_api_key),
        "Anthropic": _ai_line(
            bool(current.anthropic_api_key.strip()), "ANTHROPIC_API_KEY", cfg.get_anthropic_api_key
        ),
        "Models": (
            f"gemini={current.gemini_model}, openai={current.openai_model}, "
            f"anthropic={current.anthropic_model}"
        ),
        "OpenAI base URL": current.openai_base_url or "(default)",
        "Local LLM": local_llm_status,
        "Clone dir": current.clone_base_dir,
    })


@config_app.command("set-default")
def config_set_default(
    retries: Annotated[Optional[int], typer.Option("--retries", help="Default max retries")] = None,
    mode: Annotated[Optional[str], typer.Option("--mode", help="Default mode (auto|assist|debug)")] = None,
    auto_approve: Annotated[Optional[bool], typer.Option("--auto-approve/--no-auto-approve")] = None,
    local_llm: Annotated[Optional[bool], typer.Option("--local-llm/--no-local-llm", help="Enable/disable local Qwen2.5 model")] = None,
    ai_provider: Annotated[
        Optional[str],
        typer.Option("--ai-provider", help="Primary cloud LLM: auto | gemini | openai | anthropic"),
    ] = None,
    ai_fallback: Annotated[
        Optional[bool],
        typer.Option("--ai-fallback/--no-ai-fallback", help="Try other cloud providers after failure"),
    ] = None,
    gemini_model: Annotated[Optional[str], typer.Option("--gemini-model")] = None,
    openai_model: Annotated[Optional[str], typer.Option("--openai-model")] = None,
    anthropic_model: Annotated[Optional[str], typer.Option("--anthropic-model")] = None,
    openai_base_url: Annotated[Optional[str], typer.Option("--openai-base-url")] = None,
) -> None:
    """Update default configuration values."""
    current = cfg.load()
    if retries is not None:
        current.default_retries = retries
    if mode is not None:
        if mode not in ("auto", "assist", "debug"):
            display.error("--mode must be one of: auto, assist, debug")
            raise typer.Exit(1)
        current.default_mode = mode
    if auto_approve is not None:
        current.auto_approve = auto_approve
    if local_llm is not None:
        current.use_local_llm = local_llm
    if ai_provider is not None:
        ap = ai_provider.strip().lower()
        if ap not in ("auto", "gemini", "openai", "anthropic"):
            display.error("--ai-provider must be one of: auto, gemini, openai, anthropic")
            raise typer.Exit(1)
        current.ai_cloud_provider = ap
    if ai_fallback is not None:
        current.ai_cloud_fallback = ai_fallback
    if gemini_model is not None:
        current.gemini_model = gemini_model.strip()
    if openai_model is not None:
        current.openai_model = openai_model.strip()
    if anthropic_model is not None:
        current.anthropic_model = anthropic_model.strip()
    if openai_base_url is not None:
        current.openai_base_url = openai_base_url.strip().rstrip("/")
    cfg.save(current)
    display.success("Configuration updated")


# ── repofix ps ────────────────────────────────────────────────────────────

@app.command()
def ps() -> None:
    """List all processes started by repofix (running and recently stopped)."""
    from repofix.core import process_registry as registry

    entries = registry.reconcile()
    display.processes_table(entries)


# ── repofix logs ──────────────────────────────────────────────────────────

@app.command()
def logs(
    name: Annotated[str, typer.Argument(help="Process name (from repofix ps)")],
    lines: Annotated[int, typer.Option("--lines", "-n", help="Number of lines to show")] = 50,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow/tail the log")] = False,
) -> None:
    """Show logs for a running or recently-stopped process."""
    import time as _time

    from repofix.core import process_registry as registry

    entry = registry.get_by_name(name)
    if not entry:
        display.error(f"No process named '{name}'. Run [bold]repofix ps[/bold] to list them.")
        raise typer.Exit(1)

    log_file = registry.log_path_for(name)
    if not log_file or not log_file.exists():
        display.error(f"No log file found for '{name}'.")
        raise typer.Exit(1)

    display.info(f"Logs for [bold]{name}[/bold] ({log_file})")
    display.rule()

    # Print last N lines
    with open(log_file) as fh:
        all_lines = fh.readlines()
    tail = all_lines[-lines:]
    for raw in tail:
        raw = raw.rstrip("\n")
        if raw.startswith("[stderr]"):
            display.log_line(raw[len("[stderr]"):].lstrip(), "stderr")
        else:
            display.log_line(raw.removeprefix("[stdout]").lstrip(), "stdout")

    if not follow:
        return

    # Tail mode — watch for new content
    display.info("Following logs… (Ctrl+C to stop)")
    try:
        with open(log_file) as fh:
            fh.seek(0, 2)   # seek to end
            while True:
                line = fh.readline()
                if line:
                    line = line.rstrip("\n")
                    if line.startswith("[stderr]"):
                        display.log_line(line[len("[stderr]"):].lstrip(), "stderr")
                    else:
                        display.log_line(line.removeprefix("[stdout]").lstrip(), "stdout")
                else:
                    _time.sleep(0.2)
    except KeyboardInterrupt:
        pass


# ── repofix stop ──────────────────────────────────────────────────────────

@app.command()
def stop(
    name: Annotated[str, typer.Argument(help="Process name (from repofix ps)")],
    force: Annotated[bool, typer.Option("--force", help="Send SIGKILL instead of SIGTERM")] = False,
) -> None:
    """Stop a running process by name."""
    import signal as _signal

    from repofix.core import process_registry as registry

    entry = registry.get_by_name(name)
    if not entry:
        display.error(f"No process named '{name}'. Run [bold]repofix ps[/bold] to list them.")
        raise typer.Exit(1)

    if entry.status != "running":
        display.warning(f"Process '{name}' is not running (status: {entry.status}).")
        raise typer.Exit(0)

    if not registry.is_alive(entry.pid):
        display.warning(f"PID {entry.pid} is no longer alive. Updating registry.")
        registry.set_status(name, "crashed")
        raise typer.Exit(0)

    try:
        sig = _signal.SIGKILL if force else _signal.SIGTERM
        import os as _os
        _os.kill(entry.pid, sig)
        registry.set_status(name, "stopped")
        display.success(f"Process '{name}' (PID {entry.pid}) stopped.")
    except ProcessLookupError:
        display.warning(f"PID {entry.pid} not found — already exited.")
        registry.set_status(name, "crashed")
    except PermissionError:
        display.error(f"Permission denied to signal PID {entry.pid}.")
        raise typer.Exit(1)


# ── repofix restart ───────────────────────────────────────────────────────

@app.command()
def restart(
    name: Annotated[str, typer.Argument(help="Process name (from repofix ps)")],
) -> None:
    """
    Stop a running process and re-launch it with the same command and environment.

    This re-runs only the start command (no install/build). For a full re-run
    use [bold]repofix run <repo>[/bold].
    """
    import os as _os
    import signal as _signal
    import time as _time
    from pathlib import Path as _Path

    from repofix.core import process_registry as registry
    from repofix.core.executor import run_long_lived
    from repofix.core.process_registry import ProcessEntry, make_log_path

    entry = registry.get_by_name(name)
    if not entry:
        display.error(f"No process named '{name}'. Run [bold]repofix ps[/bold] to list them.")
        raise typer.Exit(1)

    # Stop the current instance if alive
    if entry.status == "running" and registry.is_alive(entry.pid):
        display.step(f"Stopping '{name}' (PID {entry.pid})…")
        try:
            _os.kill(entry.pid, _signal.SIGTERM)
            _time.sleep(1)
            if registry.is_alive(entry.pid):
                _os.kill(entry.pid, _signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        registry.set_status(name, "stopped")

    repo_path = _Path(entry.repo_path)
    if not repo_path.exists():
        display.error(f"Repo path no longer exists: {repo_path}")
        raise typer.Exit(1)

    display.step(f"Restarting '{name}': [bold]{entry.run_command}[/bold]")
    display.rule()

    log_path = make_log_path(name)
    proc = run_long_lived(
        entry.run_command,
        repo_path,
        env=entry.env or None,
        log_file=log_path,
    )

    # Give it a moment to start
    _time.sleep(2)
    if not proc.is_running():
        display.error(f"Process exited immediately (exit code {proc.exit_code()}).")
        registry.set_status(name, "crashed")
        raise typer.Exit(1)

    new_entry = ProcessEntry(
        name=name,
        pid=proc.pid,
        repo_url=entry.repo_url,
        repo_path=entry.repo_path,
        run_command=entry.run_command,
        log_file=str(log_path),
        started_at=_time.time(),
        status="running",
        app_url=entry.app_url,
        stack=entry.stack,
        port=entry.port,
        env=entry.env,
    )
    registry.register(new_entry)
    display.success(
        f"Process '{name}' restarted (PID {proc.pid}).\n"
        f"  Logs: {log_path}\n"
        f"  Use [bold]repofix logs {name} --follow[/bold] to tail output."
    )

    # Stay in foreground
    try:
        proc.wait_until_done()
        registry.set_status(name, "crashed")
        display.warning("Process exited unexpectedly.")
    except KeyboardInterrupt:
        proc.terminate()
        registry.set_status(name, "stopped")
        display.success("Process stopped.")


# ── repofix start ────────────────────────────────────────────────────────

@app.command()
def start(
    name: Annotated[Optional[str], typer.Argument(help="Process name (from repofix ps). Omit to start the most recently stopped process.")] = None,
) -> None:
    """
    Start a stopped or crashed process without reinstalling dependencies.

    If no name is given, the most recently stopped process is started automatically.
    The process runs in the foreground; use Ctrl+C to stop it again.

    Examples:

      repofix start                  # restart last stopped process

      repofix start my-app           # restart by name
    """
    import time as _time
    from pathlib import Path as _Path

    from repofix.core import process_registry as registry
    from repofix.core.executor import run_long_lived
    from repofix.core.process_registry import ProcessEntry, make_log_path

    # ── Resolve which entry to start ─────────────────────────────────────────
    if name is None:
        all_entries = registry.reconcile()
        stoppable = [e for e in all_entries if e.status in ("stopped", "crashed")]
        if not stoppable:
            display.error(
                "No stopped processes found. "
                "Run [bold]repofix ps[/bold] to see all processes."
            )
            raise typer.Exit(1)
        stoppable.sort(key=lambda e: e.started_at, reverse=True)
        entry = stoppable[0]
        display.info(f"Resuming most recently stopped process: [bold]{entry.name}[/bold]")
    else:
        entry = registry.get_by_name(name)
        if not entry:
            display.error(
                f"No process named '{name}'. "
                "Run [bold]repofix ps[/bold] to list them."
            )
            raise typer.Exit(1)

    # ── Guard: already running ────────────────────────────────────────────────
    if entry.status == "running" and registry.is_alive(entry.pid):
        display.warning(f"Process '{entry.name}' is already running (PID {entry.pid}).")
        display.info(f"Use [bold]repofix restart {entry.name}[/bold] to restart it.")
        raise typer.Exit(0)

    # ── Sanity check: repo path still exists ─────────────────────────────────
    repo_path = _Path(entry.repo_path)
    if not repo_path.exists():
        display.error(f"Repo path no longer exists: {repo_path}")
        raise typer.Exit(1)

    # ── Launch ────────────────────────────────────────────────────────────────
    display.step(f"Starting [bold]{entry.name}[/bold]: [dim]{entry.run_command}[/dim]")
    display.rule()

    log_path = make_log_path(entry.name)
    proc = run_long_lived(
        entry.run_command,
        repo_path,
        env=entry.env or None,
        log_file=log_path,
    )

    # Brief wait to detect immediate crashes
    _time.sleep(2)
    if not proc.is_running():
        display.error(f"Process exited immediately (exit code {proc.exit_code()}).")
        display.info(
            f"Run [bold]repofix logs {entry.name}[/bold] to see what went wrong, "
            f"or [bold]repofix run {entry.repo_url}[/bold] for a full reinstall."
        )
        registry.set_status(entry.name, "crashed")
        raise typer.Exit(1)

    new_entry = ProcessEntry(
        name=entry.name,
        pid=proc.pid,
        repo_url=entry.repo_url,
        repo_path=entry.repo_path,
        run_command=entry.run_command,
        log_file=str(log_path),
        started_at=_time.time(),
        status="running",
        app_url=entry.app_url,
        stack=entry.stack,
        port=entry.port,
        env=entry.env,
    )
    registered_name = registry.register(new_entry)
    display.success(f"Process [bold]{registered_name}[/bold] started (PID {proc.pid})")
    if entry.app_url:
        display.info(f"App URL: [link={entry.app_url}]{entry.app_url}[/link]")
    display.info(f"Logs:    {log_path}")

    # Stay in foreground — stream until crash or Ctrl+C
    try:
        proc.wait_until_done()
        registry.set_status(registered_name, "crashed")
        display.warning("Process exited unexpectedly.")
        display.info(f"Run [bold]repofix logs {registered_name}[/bold] to diagnose.")
    except KeyboardInterrupt:
        display.info("Stopping…")
        proc.terminate()
        registry.set_status(registered_name, "stopped")
        display.success(
            f"Process stopped. Run [bold]repofix start {registered_name}[/bold] to start again."
        )


# ── repofix history ───────────────────────────────────────────────────────

@app.command()
def history(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of runs to show")] = 20,
) -> None:
    """Show past run history."""
    from repofix.memory import store as memory

    rows = memory.get_recent_runs(limit)
    if not rows:
        display.info("No runs recorded yet.")
        return
    display.runs_table(rows)


# ── repofix branches ──────────────────────────────────────────────────────

@app.command()
def branches(
    repo: Annotated[Optional[str], typer.Argument(
        help="Filter by repo URL or local path (optional)"
    )] = None,
) -> None:
    """
    List all cached branch environments.

    repofix caches the installed dependencies for each branch so that
    switching back to a branch you've already set up skips the install step.

    Examples:

      repofix branches

      repofix branches https://github.com/user/my-app

      repofix branches ./local-project
    """
    from repofix.branch import cache as branch_cache
    from repofix.memory import store as memory

    repo_key: Optional[str] = None
    if repo:
        from pathlib import Path as _Path
        repo_key = branch_cache.normalize_repo_key(repo, _Path(repo).expanduser().resolve())

    states = memory.list_branch_states(repo_key=repo_key)
    display.branches_table(states)


# ── repofix branch-clean ──────────────────────────────────────────────────

@app.command("branch-clean")
def branch_clean(
    repo: Annotated[Optional[str], typer.Argument(
        help="Repo URL or local path whose cache to clear (omit for all repos)"
    )] = None,
    branch: Annotated[Optional[str], typer.Option(
        "--branch", "-b",
        help="Specific branch to remove (requires --repo)"
    )] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """
    Remove cached branch environments.

    Without arguments, clears ALL branch caches across all repos.
    With a repo argument, clears only that repo's caches.
    With --branch, clears a single branch entry.

    The next run will perform a fresh install for the affected branch(es).

    Examples:

      repofix branch-clean

      repofix branch-clean https://github.com/user/my-app

      repofix branch-clean https://github.com/user/my-app --branch feature-x
    """
    from pathlib import Path as _Path

    from repofix.branch import cache as branch_cache
    from repofix.memory import store as memory

    repo_key: Optional[str] = None
    if repo:
        repo_key = branch_cache.normalize_repo_key(repo, _Path(repo).expanduser().resolve())

    if branch and not repo_key:
        display.error("--branch requires a repo argument. Example: repofix branch-clean <repo> --branch <name>")
        raise typer.Exit(1)

    # Describe what we're about to delete
    if branch and repo_key:
        target_desc = f"branch [bold]{branch}[/bold] of [bold]{repo}[/bold]"
    elif repo_key:
        target_desc = f"all branches of [bold]{repo}[/bold]"
    else:
        target_desc = "ALL cached branch environments"

    if not yes:
        confirmed = display.prompt_confirm(f"Remove {target_desc}?")
        if not confirmed:
            display.info("Cancelled.")
            return

    if branch and repo_key:
        deleted = memory.delete_branch_state(repo_key, branch)
        if deleted:
            display.success(f"Removed cache for branch [bold]{branch}[/bold].")
        else:
            display.warning(f"No cache found for branch '{branch}' of that repo.")
    else:
        count = memory.clear_branch_states(repo_key=repo_key)
        display.success(f"Removed {count} cached branch environment(s).")


# ── repofix model ─────────────────────────────────────────────────────────

model_app = typer.Typer(help="Manage the local Qwen2.5-Coder-3B model.", no_args_is_help=True)
app.add_typer(model_app, name="model")


@model_app.command("download")
def model_download() -> None:
    """
    Install llama-cpp-python and download the Qwen2.5-Coder-3B model (~2 GB).

    Runs automatically on the first `repofix run` — only needed here
    if you want to pre-fetch before going offline.
    Works on Linux, macOS, and Windows.
    """
    from repofix.fixing import local_llm

    if local_llm.is_available() and local_llm.is_downloaded():
        display.success(f"Model already ready: {local_llm.model_path()}")
        return

    try:
        local_llm.ensure_ready()
    except Exception as exc:
        display.error(f"Setup failed: {exc}")
        raise typer.Exit(1)


@model_app.command("status")
def model_status() -> None:
    """Show llama-cpp-python and model status."""
    from repofix.fixing import local_llm

    deps_ok = local_llm.is_available()
    downloaded = local_llm.is_downloaded()
    path = local_llm.model_path()

    size_str = "—"
    if downloaded:
        size_str = f"{path.stat().st_size / 1_073_741_824:.1f} GB"

    display.detection_panel({
        "Model":            "Qwen2.5-Coder-3B-Instruct Q4_K_M",
        "llama-cpp-python": "✔ installed" if deps_ok else "✘ not installed",
        "Model file":       f"✔ {path}" if downloaded else "✘ not downloaded",
        "Size on disk":     size_str,
        "Models dir":       str(local_llm.models_dir()),
    })


@model_app.command("remove")
def model_remove(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete the local model file to reclaim ~2 GB of disk space."""
    from repofix.fixing import local_llm

    path = local_llm.model_path()
    if not path.exists():
        display.info("Model file is not present — nothing to remove.")
        return

    size_gb = path.stat().st_size / 1_073_741_824
    if not yes:
        confirmed = display.prompt_confirm(f"Remove {path} ({size_gb:.1f} GB)?")
        if not confirmed:
            display.info("Cancelled.")
            return

    path.unlink()
    display.success(f"Removed {path} ({size_gb:.1f} GB freed).")


# ── repofix clear-memory ──────────────────────────────────────────────────

@app.command("clear-memory")
def clear_memory(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Clear the fix memory store and run history."""
    if not yes:
        confirmed = display.prompt_confirm("This will delete all fix memory and run history. Continue?")
        if not confirmed:
            display.info("Cancelled.")
            return
    from repofix.memory import store as memory
    memory.clear_all()
    display.success("Fix memory and run history cleared.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    from repofix.shell_completion_auto import maybe_install_shell_completion

    maybe_install_shell_completion()
    app()


if __name__ == "__main__":
    main()
