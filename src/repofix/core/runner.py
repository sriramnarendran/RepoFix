"""Main orchestrator — the central brain of repofix."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import json

from repofix import config as cfg
from repofix.branch import cache as branch_cache
from repofix.detection import commands as cmd_detect
from repofix.detection import stack as stack_detect
from repofix.detection.commands import CommandSet, find_node_entry
from repofix.detection.multi import ServiceSpec, detect_services
from repofix.detection.stack import StackInfo
from repofix.env import manager as env_manager
from repofix.env import venv as venv_mgr
from repofix.env.port import find_free_port, resolve_port
from repofix.fixing import ai_fixer
from repofix.fixing.classifier import classify_all
from repofix.fixing.detector import detect_errors, is_fatal_exit
from repofix.fixing.rules import FixAction
from repofix.fixing.retry import (
    RetryState,
    apply_fix_commands,
    build_suggestions,
    collect_pending_fixes,
    note_failed_rule_memory_fix,
    pick_and_validate_fix,
    update_force_ai_after_ai_fix_commands,
)
from repofix.core import process_registry as registry
from repofix.core.install_fallback import suggest_node_install_after_make_shell_bug
from repofix.core.process_registry import ProcessEntry, make_log_path
from repofix.memory import store as memory
from repofix.output import display


@dataclass
class RunOptions:
    branch: str | None = None
    mode: str = "auto"          # auto | assist | debug
    port: int | None = None
    max_retries: int = 5
    env_file: Path | None = None
    no_fix: bool = False
    auto_approve: bool = False
    override_command: str | None = None
    override_install: str | None = None
    install_mode: str | None = None   # None = ask | "binary" | "source"
    deploy_mode: str | None = None    # None = ask | "prod" | "dev"


@dataclass
class RunResult:
    success: bool
    repo_path: Path
    stack: StackInfo
    commands: CommandSet
    duration_s: float
    fix_count: int = 0
    app_url: str | None = None
    error_summary: str = ""
    suggestions: list[str] = field(default_factory=list)
    isolated_env_path: str | None = None
    used_artifact: bool = False


def run(repo_path: Path, source: str, options: RunOptions) -> RunResult:
    """
    Full execution pipeline:
      -1. Artifact detection — offer prebuilt binary when available
      0. Multi-service detection (branch to run_multi if needed)
      1. Detect stack
      2. Discover commands
      3. Isolated environment setup (venv for Python, etc.)
      4. Resolve env vars
      5. Port management
      6–8. Install → Build → Run with fix-retry loop
      9. Record outcome
    """
    start_time = time.monotonic()
    app_cfg = cfg.load()
    debug = options.mode == "debug"
    has_ai = ai_fixer.ai_fix_available()

    # ── -2. Deployment mode detection ────────────────────────────────────────
    deploy_result = _check_deploy_mode(repo_path, source, options, start_time)
    if deploy_result is not None:
        return deploy_result

    # ── -1. Artifact detection ────────────────────────────────────────────────
    artifact_result = _check_artifacts(repo_path, source, options, start_time)
    if artifact_result is not None:
        return artifact_result

    # ── 0. Multi-service detection ────────────────────────────────────────────
    display.step("Scanning repo structure…")
    services = detect_services(repo_path)
    if services and len(services) >= 2:
        display.multi_service_panel([
            {"name": s.name, "role": s.role, "path": str(s.path.relative_to(repo_path) if s.path != repo_path else "."), "color": s.log_color}
            for s in services
        ])
        return _run_multi(repo_path, source, services, options, start_time, app_cfg, has_ai, debug)

    # ── Branch cache: fingerprint this branch's dependencies ─────────────────
    _repo_key      = branch_cache.normalize_repo_key(source, repo_path)
    _current_branch = branch_cache.get_current_branch(repo_path)
    _dep_hash, _dep_files = branch_cache.compute_dep_hash(repo_path)
    _venv_dir_name = branch_cache.branch_venv_name(_current_branch)

    # ── 1. Stack detection ────────────────────────────────────────────────────
    display.step("Detecting stack…")
    ai_stack_fn = ai_fixer.detect_stack_from_readme if has_ai else None
    stack = stack_detect.detect(repo_path, readme_ai_fallback=ai_stack_fn)

    # In "dev deploy mode", prefer running from source even if Docker artifacts
    # (docker-compose.yml) exist.
    if options.deploy_mode == "dev" and stack.runtime == "docker":
        display.step("Prefer source over Docker (dev mode)…")
        stack = stack_detect.detect_without_docker(repo_path, readme_ai_fallback=ai_stack_fn)

    if not stack.is_known():
        display.warning("Could not determine stack — will attempt generic execution")
    else:
        display.detection_panel({
            **stack.as_display_dict(),
        })

    # Resolve the cached state now that we know the runtime
    _cached_branch = memory.get_branch_state(_repo_key, _current_branch)
    _branch_cache_hit = (
        _cached_branch is not None
        and _cached_branch["dep_hash"] == _dep_hash
        and _cached_branch["install_success"]
        and branch_cache.is_env_valid(
            repo_path,
            stack.runtime,
            _cached_branch.get("env_dir", ""),
        )
    )
    _build_cache_hit = _branch_cache_hit and bool(_cached_branch and _cached_branch.get("build_success"))

    if _branch_cache_hit:
        display.branch_cache_hit(_current_branch, _dep_hash[:8], _cached_branch["installed_when"])
    else:
        if _cached_branch:
            display.branch_cache_miss(_current_branch, reason="deps changed")
        else:
            display.branch_cache_miss(_current_branch, reason="no cache yet")

    # ── 2. Command discovery ──────────────────────────────────────────────────
    display.step("Discovering commands…")
    ai_cmd_fn = ai_fixer.extract_commands_from_readme if has_ai else None
    commands = cmd_detect.discover(
        repo_path,
        stack,
        override_install=options.override_install,
        override_run=options.override_command,
        readme_ai_fallback=ai_cmd_fn,
    )

    if debug:
        display.detection_panel(commands.as_display_dict())

    if not commands.run:
        display.failure_panel(
            "Could not determine how to run this project",
            [
                "Use --command to specify the run command manually",
                "Check the README for instructions",
            ],
        )
        return RunResult(
            success=False,
            repo_path=repo_path,
            stack=stack,
            commands=commands,
            duration_s=time.monotonic() - start_time,
            error_summary="No run command found",
            suggestions=["Use --command to specify the run command"],
        )

    # ── 2b. Non-runnable repo detection ───────────────────────────────────────
    # Check before spending time on env setup / install / build / run.
    # Only skip when the user explicitly overrode the run command — they know best.
    if not options.override_command:
        _non_runnable = cmd_detect.detect_non_runnable(repo_path)
        if _non_runnable:
            display.non_runnable_panel(_non_runnable["type"], _non_runnable)
            return RunResult(
                success=True,
                repo_path=repo_path,
                stack=stack,
                commands=commands,
                duration_s=time.monotonic() - start_time,
                suggestions=[
                    "This repo is not a runnable service",
                    "See the README for installation instructions",
                ],
            )

    # ── 3. Isolated environment setup ─────────────────────────────────────────
    display.step("Setting up isolated environment…")
    isolation_env = venv_mgr.setup(repo_path, stack, venv_dir_name=_venv_dir_name)
    if isolation_env:
        runtime_label = {
            "python": f"Python venv ({venv_mgr.venv_path(repo_path, _venv_dir_name)})",
            "pip": f"Python venv ({venv_mgr.venv_path(repo_path, _venv_dir_name)})",
            "node": "node_modules (local)",
            "ruby": "Bundler vendor/bundle",
        }.get(stack.runtime.lower(), stack.runtime)
        display.success(f"Isolated environment ready: [bold]{runtime_label}[/bold]")

    # ── 4. Env var resolution ─────────────────────────────────────────────────
    display.step("Resolving environment variables…")
    env = env_manager.resolve_env(
        repo_path,
        extra_env_file=options.env_file,
        auto_approve=options.auto_approve,
        mode=options.mode,
    )
    # Merge isolation overrides: isolation_env takes priority for PATH/VIRTUAL_ENV
    env = {**env, **isolation_env}
    if stack.runtime.lower() in ("node", "npm") and any(
        display.command_uses_npm_global_install(c or "")
        for c in (commands.install, commands.build, commands.run)
        if c
    ):
        if display.prompt_npm_global_scope(auto_approve=options.auto_approve) == "machine":
            if venv_mgr.machine_npm_global_prefix_writable(repo_path):
                env.pop("npm_config_prefix", None)
            else:
                resolution = _resolve_npm_global_machine_unwritable(
                    auto_approve=options.auto_approve,
                )
                if resolution == "abort":
                    display.error(
                        "Aborted — npm global directory is not writable without sudo or an isolated prefix."
                    )
                    return RunResult(
                        success=False,
                        repo_path=repo_path,
                        stack=stack,
                        commands=commands,
                        duration_s=time.monotonic() - start_time,
                        error_summary="npm global install aborted (prefix not writable)",
                    )
                if resolution == "sudo":
                    env.pop("npm_config_prefix", None)
                    if commands.install and display.command_uses_npm_global_install(
                        commands.install
                    ):
                        commands.install = _sudo_wrap_npm_global_command(commands.install)
                    if commands.build and display.command_uses_npm_global_install(
                        commands.build
                    ):
                        commands.build = _sudo_wrap_npm_global_command(commands.build)
                    if commands.run and display.command_uses_npm_global_install(commands.run):
                        commands.run = _sudo_wrap_npm_global_command(commands.run)
                # isolated: keep npm_config_prefix from isolation_env

    # ── 5. Port management ────────────────────────────────────────────────────
    desired_port = options.port or _detect_default_port(stack)
    if desired_port:
        actual_port = resolve_port(desired_port, auto_approve=options.auto_approve, mode=options.mode)
        if actual_port != desired_port:
            env["PORT"] = str(actual_port)
            env["HOST_PORT"] = str(actual_port)

    # ── 6-8. Install → Build → Run with fix-retry loop ────────────────────────
    retry_state = RetryState(max_retries=options.max_retries)
    fix_count = 0
    final_errors = []

    while not retry_state.exhausted():
        retry_state.increment()
        attempt = retry_state.attempt
        is_first = attempt == 1

        # Install — skip on first attempt when branch cache is valid
        if commands.install:
            if _branch_cache_hit and is_first:
                display.step(
                    f"Skipping install — branch [bold]{_current_branch}[/bold] "
                    f"deps cached [dim]({_dep_hash[:8]})[/dim]"
                )
            else:
                display.step(f"Installing dependencies: [bold]{commands.install}[/bold]")
                with display.live_step(commands.install):
                    install_result = _run_step(commands.install, repo_path, env, debug)
                if not install_result.succeeded:
                    alt_install = suggest_node_install_after_make_shell_bug(
                        repo_path, commands.install, install_result.full_output
                    )
                    if alt_install:
                        display.warning(
                            "Make failed: GNU Make passes [bold]-c[/bold] to the shell, but this "
                            "repo's Makefile also ends [bold].SHELLFLAGS[/bold] with [bold]-c[/bold], "
                            "so the shell runs an empty script. "
                            f"Retrying with [bold]{alt_install}[/bold]…"
                        )
                        with display.live_step(alt_install):
                            install_result = _run_step(alt_install, repo_path, env, debug)
                        if install_result.succeeded:
                            commands.install = alt_install
                if not install_result.succeeded:
                    if options.no_fix:
                        break
                    signals = detect_errors(install_result.all_lines)
                    errors = classify_all(signals, stack.runtime)
                    if errors:
                        retry_state.prepare_escalation(errors)
                        fix_result = pick_and_validate_fix(
                            errors, stack, repo_path, options.mode,
                            options.auto_approve, has_ai,
                            install_result.full_output, retry_state.applied_fingerprints,
                            retry_state.force_ai_fingerprints,
                            retry_state.ai_invocation_count,
                        )
                        if fix_result.attempted and fix_result.action:
                            action = fix_result.action
                            _apply_fix_env(action, env, options.mode, options.auto_approve)
                            display.fix_panel(attempt, errors[0].error_type, action.description, action.source)
                            ok = apply_fix_commands(action, repo_path, env, debug)
                            if fix_result.error:
                                memory.record_fix(fix_result.error, action, ok, str(stack.runtime))
                                retry_state.applied_fingerprints.add(fix_result.error.fingerprint())
                                if ok:
                                    retry_state.last_successful_fix = (
                                        fix_result.error.fingerprint(),
                                        action.source,
                                    )
                                    retry_state.force_ai_fingerprints.discard(
                                        fix_result.error.fingerprint()
                                    )
                                elif action.source == "ai" and fix_result.error:
                                    update_force_ai_after_ai_fix_commands(
                                        ok=ok,
                                        fingerprint=fix_result.error.fingerprint(),
                                        force_ai_fingerprints=retry_state.force_ai_fingerprints,
                                        ai_invocation_count=retry_state.ai_invocation_count,
                                    )
                            if ok:
                                fix_count += 1
                                if action.next_step == "run":
                                    # Fix already completed the install (e.g. --ignore-scripts).
                                    # Clear install so the next loop iteration skips it.
                                    commands.install = None
                                continue
                    final_errors = errors
                    break
                else:
                    # Persist successful install into the branch cache
                    memory.save_branch_state(
                        repo_key=_repo_key,
                        branch=_current_branch,
                        dep_hash=_dep_hash,
                        env_dir=str(venv_mgr.venv_path(repo_path, _venv_dir_name)),
                        stack_json=json.dumps(stack.as_display_dict()),
                        commands_json=json.dumps(commands.as_display_dict()),
                        dep_files=json.dumps(_dep_files),
                        install_success=True,
                        build_success=False,  # build hasn't run yet
                    )

        # Build — skip on first attempt when the previous build was cached
        if commands.build:
            if _build_cache_hit and is_first:
                display.step(
                    f"Skipping build — branch [bold]{_current_branch}[/bold] "
                    f"build cached [dim]({_dep_hash[:8]})[/dim]"
                )
            else:
                display.step(f"Building: [bold]{commands.build}[/bold]")
                with display.live_step(commands.build):
                    build_result = _run_step(commands.build, repo_path, env, debug)
                if not build_result.succeeded:
                    if options.no_fix:
                        break
                    signals = detect_errors(build_result.all_lines)
                    errors = classify_all(signals, stack.runtime)
                    if errors:
                        retry_state.prepare_escalation(errors)
                        fix_result = pick_and_validate_fix(
                            errors, stack, repo_path, options.mode,
                            options.auto_approve, has_ai,
                            build_result.full_output, retry_state.applied_fingerprints,
                            retry_state.force_ai_fingerprints,
                            retry_state.ai_invocation_count,
                        )
                        if fix_result.attempted and fix_result.action:
                            action = fix_result.action
                            _apply_fix_env(action, env, options.mode, options.auto_approve)
                            display.fix_panel(attempt, errors[0].error_type, action.description, action.source)
                            ok = apply_fix_commands(action, repo_path, env, debug)
                            if fix_result.error:
                                memory.record_fix(fix_result.error, action, ok, str(stack.runtime))
                                retry_state.applied_fingerprints.add(fix_result.error.fingerprint())
                                if ok:
                                    retry_state.last_successful_fix = (
                                        fix_result.error.fingerprint(),
                                        action.source,
                                    )
                                    retry_state.force_ai_fingerprints.discard(
                                        fix_result.error.fingerprint()
                                    )
                                elif action.source == "ai" and fix_result.error:
                                    update_force_ai_after_ai_fix_commands(
                                        ok=ok,
                                        fingerprint=fix_result.error.fingerprint(),
                                        force_ai_fingerprints=retry_state.force_ai_fingerprints,
                                        ai_invocation_count=retry_state.ai_invocation_count,
                                    )
                            if ok:
                                fix_count += 1
                                continue
                    final_errors = errors
                    break
                else:
                    # Persist successful build into the branch cache
                    _build_cache_hit = True
                    memory.save_branch_state(
                        repo_key=_repo_key,
                        branch=_current_branch,
                        dep_hash=_dep_hash,
                        env_dir=str(venv_mgr.venv_path(repo_path, _venv_dir_name)),
                        stack_json=json.dumps(stack.as_display_dict()),
                        commands_json=json.dumps(commands.as_display_dict()),
                        dep_files=json.dumps(_dep_files),
                        install_success=True,
                        build_success=True,
                    )

        # Run (long-lived)
        display.step(f"Starting app: [bold]{commands.run}[/bold]")
        display.rule()

        from repofix.core.executor import run_long_lived

        collected_lines = []

        def _on_line(source: str, line: str) -> None:
            collected_lines.append((source, line))

        # Derive a stable name for this process from the source URL/path
        _proc_name = _process_name(source)
        _log_path = make_log_path(_proc_name)

        from repofix.core.docker_compose_bind_fix import ensure_docker_compose_bind_files

        ensure_docker_compose_bind_files(
            repo_path,
            run_command=commands.run,
            stack_is_docker=stack.is_docker(),
        )

        proc = run_long_lived(
            commands.run, repo_path, env=env, debug=debug,
            on_line=_on_line, log_file=_log_path,
        )

        # Wait for the process to either succeed or crash
        _wait_for_exit_or_ready(proc, stack)

        if not proc.is_running():
            _raw_exit = proc.exit_code()
            # Preserve exit code 0 (clean/library run success). Only fall back to 1
            # when exit_code is None, which shouldn't happen after is_running() is
            # False but guards against a race in the process wrapper.
            exit_code = _raw_exit if _raw_exit is not None else 1
            signals = detect_errors(proc.all_lines)
            errors = classify_all(signals, stack.runtime)

            if not is_fatal_exit(exit_code, signals):
                # If the command that just ran was a Java build step (e.g. an npm
                # script wrapping Maven) and it produced a runnable JAR, pivot to
                # starting the Java server without going through install/build again.
                _not_jar_launch = not (commands.run or "").lstrip().startswith("java -jar")
                if _not_jar_launch:
                    from repofix.detection.commands import find_best_jar, has_java_build_files
                    _built_jar = find_best_jar(repo_path) if has_java_build_files(repo_path) else None
                    if _built_jar:
                        try:
                            _jar_rel = _built_jar.relative_to(repo_path)
                        except ValueError:
                            _jar_rel = _built_jar
                        _jar_cmd = f"java -jar {_jar_rel}"
                        display.success("Build complete.")
                        display.step(f"Starting Java server: [bold]{_jar_cmd}[/bold]")
                        # Swap run to the JAR; skip install + build on the next
                        # iteration since they already succeeded.
                        commands.run = _jar_cmd
                        commands.build = None
                        commands.install = None
                        continue

                # CLI tool invoked without a subcommand — must be checked before
                # cli_needs_args because a subcommand-style "Usage: tool [COMMAND]"
                # line also triggers the broader cli_needs_args pattern.
                if any(e.error_type == "cli_no_subcommand" for e in errors):
                    run_cmd = commands.run or "the detected command"
                    venv_activate: str | None = None
                    if stack.runtime.lower() in ("python", "pip"):
                        venv_activate = f"{venv_mgr.venv_path(repo_path, _venv_dir_name)}/bin/activate"
                    display.cli_tool_ready_panel(run_cmd, str(repo_path), venv_activate)
                    duration = time.monotonic() - start_time
                    memory.record_run(source, str(stack.runtime), True, duration, fix_count)
                    return RunResult(
                        success=True,
                        repo_path=repo_path,
                        stack=stack,
                        commands=commands,
                        duration_s=duration,
                        fix_count=fix_count,
                    )

                # CLI tool that printed usage/help and needs positional arguments
                if any(e.error_type == "cli_needs_args" for e in errors):
                    from repofix.fixing.detector import parse_usage_help
                    usage_info = parse_usage_help(proc.full_output)
                    run_cmd = commands.run or "the detected command"
                    display.cli_needs_args_panel(
                        run_cmd,
                        usage_info["usage_synopsis"],
                        usage_info["options_preview"],
                    )
                    if not options.auto_approve:
                        new_cmd = display.prompt_cli_args(run_cmd)
                        if new_cmd.strip() != run_cmd.strip():
                            commands.run = new_cmd
                            commands.install = None
                            commands.build = None
                            continue
                    # User skipped or auto_approve — return with guidance
                    return RunResult(
                        success=True,
                        repo_path=repo_path,
                        stack=stack,
                        commands=commands,
                        duration_s=time.monotonic() - start_time,
                        fix_count=fix_count,
                        suggestions=[
                            f"Repo: {repo_path}",
                            f"Usage: {run_cmd} {usage_info['usage_synopsis']}",
                            f"Re-run: --command '{run_cmd} <your-args>'",
                        ],
                    )

                # Clean exit (build-only tool, one-shot CLI, SIGINT, etc.)
                duration = time.monotonic() - start_time
                memory.record_run(source, str(stack.runtime), True, duration, fix_count)
                return RunResult(
                    success=True,
                    repo_path=repo_path,
                    stack=stack,
                    commands=commands,
                    duration_s=duration,
                    fix_count=fix_count,
                )

            # Wrong entry file — discover the actual entry point and retry
            if any(e.error_type == "wrong_entry_point" for e in errors):
                from repofix.detection.commands import find_node_entry
                bad = next(
                    (e.extracted.get("bad_path", "") for e in errors if e.error_type == "wrong_entry_point"),
                    commands.run or "",
                )
                new_entry_cmd = find_node_entry(repo_path)
                if new_entry_cmd and new_entry_cmd.strip() != (commands.run or "").strip():
                    display.fix_panel(
                        attempt,
                        "wrong_entry_point",
                        f"Entry file not found ({bad}) — switching to: {new_entry_cmd}",
                        "rule",
                    )
                    commands.run = new_entry_cmd
                    commands.install = None
                    commands.build = None
                    fix_count += 1
                    continue

            # CLI tool invoked without a subcommand — installed & ready, just not a service
            if any(e.error_type == "cli_no_subcommand" for e in errors):
                run_cmd = commands.run or "the detected command"
                venv_activate: str | None = None
                if stack.runtime.lower() in ("python", "pip"):
                    venv_activate = f"{venv_mgr.venv_path(repo_path, _venv_dir_name)}/bin/activate"
                display.cli_tool_ready_panel(run_cmd, str(repo_path), venv_activate)
                duration = time.monotonic() - start_time
                memory.record_run(source, str(stack.runtime), True, duration, fix_count)
                return RunResult(
                    success=True,
                    repo_path=repo_path,
                    stack=stack,
                    commands=commands,
                    duration_s=duration,
                    fix_count=fix_count,
                    suggestions=[
                        f"Repo: {repo_path}",
                        f"Run `{run_cmd} --help` to list available subcommands",
                        f"Use --command '{run_cmd} <subcommand>' to run a specific subcommand",
                    ],
                )

            if options.no_fix or not errors:
                final_errors = errors
                break

            retry_state.prepare_escalation(errors)
            fix_result = pick_and_validate_fix(
                errors, stack, repo_path, options.mode,
                options.auto_approve, has_ai,
                proc.full_output, retry_state.applied_fingerprints,
                retry_state.force_ai_fingerprints,
                retry_state.ai_invocation_count,
            )

            if fix_result.attempted and fix_result.action:
                action = fix_result.action
                _apply_fix_env(action, env, options.mode, options.auto_approve)
                display.fix_panel(attempt, errors[0].error_type, action.description, action.source)
                ok = apply_fix_commands(action, repo_path, env, debug)

                if fix_result.error:
                    memory.record_fix(fix_result.error, action, ok, str(stack.runtime))
                    retry_state.applied_fingerprints.add(fix_result.error.fingerprint())
                    if ok:
                        retry_state.last_successful_fix = (
                            fix_result.error.fingerprint(),
                            action.source,
                        )
                        retry_state.force_ai_fingerprints.discard(
                            fix_result.error.fingerprint()
                        )
                    elif action.source == "ai":
                        update_force_ai_after_ai_fix_commands(
                            ok=ok,
                            fingerprint=fix_result.error.fingerprint(),
                            force_ai_fingerprints=retry_state.force_ai_fingerprints,
                            ai_invocation_count=retry_state.ai_invocation_count,
                        )

                if ok:
                    fix_count += 1
                    # Adjust next step
                    if action.next_step == "reinstall":
                        pass  # loop will reinstall
                    elif action.next_step == "rebuild":
                        pass  # loop will rebuild
                    continue
            else:
                final_errors = errors
                break
        else:
            # App is still running — success!
            app_url = _detect_app_url(stack, env, collected_lines)
            duration = time.monotonic() - start_time
            isolated_env = isolation_env.get("VIRTUAL_ENV") or isolation_env.get("BUNDLE_PATH")
            display.rule()
            summary: dict[str, str] = {
                "Stack": f"{stack.language} / {stack.framework}",
                "Duration": f"{duration:.1f}s",
                "Fixes applied": str(fix_count),
                "Logs": str(_log_path),
            }
            if isolated_env:
                summary["Isolated env"] = isolated_env
            display.success_panel(app_url, summary)

            # Register in the process registry
            port_val: int | None = None
            try:
                port_val = int(env.get("PORT", "")) or None
            except (ValueError, TypeError):
                pass
            entry = ProcessEntry(
                name=_proc_name,
                pid=proc.pid,
                repo_url=source,
                repo_path=str(repo_path),
                run_command=commands.run,
                log_file=str(_log_path),
                started_at=time.time(),
                status="running",
                app_url=app_url,
                stack=f"{stack.language} / {stack.framework}",
                port=port_val,
                env={k: v for k, v in env.items() if not _is_sensitive_key(k)},
            )
            registered_name = registry.register(entry)
            display.info(
                f"Process registered as [bold]{registered_name}[/bold] — "
                f"use [bold]repofix ps[/bold] / [bold]repofix logs {registered_name}[/bold]"
            )
            _maybe_npm_global_cli_hint(
                repo_path, env, commands.install, commands.build, commands.run
            )
            memory.record_run(source, str(stack.runtime), True, duration, fix_count)

            # Stay in foreground — keep streaming logs until crash or Ctrl+C
            _pivot_jar_cmd: str | None = None
            try:
                proc.wait_until_done()
                final_exit = proc.exit_code()
                if final_exit in (0, None):
                    # Check if this was a Java build that left runnable JARs.
                    # If so, pivot to starting the JAR server instead of returning.
                    _is_not_jar = not (commands.run or "").lstrip().startswith("java -jar")
                    if _is_not_jar:
                        from repofix.detection.commands import find_best_jar, has_java_build_files
                        if has_java_build_files(repo_path):
                            _jar = find_best_jar(repo_path)
                            if _jar:
                                try:
                                    _jar_rel = _jar.relative_to(repo_path)
                                except ValueError:
                                    _jar_rel = _jar
                                _pivot_jar_cmd = f"java -jar {_jar_rel}"

                    if _pivot_jar_cmd:
                        registry.set_status(registered_name, "stopped")
                        display.success("Build complete.")
                    else:
                        # Clean completion — build-only tool, one-shot CLI, etc.
                        registry.set_status(registered_name, "stopped")
                        display.success("App process completed successfully.")
                elif final_exit in (130, 143):
                    # SIGINT / SIGTERM — user interrupted from another terminal
                    registry.set_status(registered_name, "stopped")
                    display.info("App process was stopped.")
                else:
                    # Check if the process just printed usage and exited — that's not a crash.
                    _post_signals = detect_errors(proc.all_lines)
                    if any(s.error_type == "cli_no_subcommand" for s in _post_signals):
                        registry.set_status(registered_name, "stopped")
                        venv_activate: str | None = None
                        if stack.runtime.lower() in ("python", "pip"):
                            venv_activate = f"{venv_mgr.venv_path(repo_path, _venv_dir_name)}/bin/activate"
                        display.cli_tool_ready_panel(commands.run or registered_name, str(repo_path), venv_activate)
                    else:
                        registry.set_status(registered_name, "crashed")
                        display.warning("App process exited unexpectedly.")
                        display.info(f"Run [bold]repofix logs {registered_name}[/bold] to diagnose.")
            except KeyboardInterrupt:
                display.info("Stopping app…")
                proc.terminate()
                registry.set_status(registered_name, "stopped")
                display.success("App stopped.")
                display.info(
                    f"Run [bold]repofix start {registered_name}[/bold] to start again "
                    f"without reinstalling."
                )

            # If we detected a built JAR, pivot: skip install + build on the next
            # iteration and go straight to running the Java server.
            if _pivot_jar_cmd:
                display.step(f"Starting Java server: [bold]{_pivot_jar_cmd}[/bold]")
                commands.run = _pivot_jar_cmd
                commands.build = None
                commands.install = None
                continue

            return RunResult(
                success=True,
                repo_path=repo_path,
                stack=stack,
                commands=commands,
                duration_s=time.monotonic() - start_time,
                fix_count=fix_count,
                app_url=app_url,
                isolated_env_path=isolated_env,
            )

    # ── Last-resort batch fix pass ────────────────────────────────────────────
    # When the per-error retry loop is exhausted, collect ALL remaining fix
    # actions at once, apply them in bulk, then attempt one final pipeline run.
    if final_errors and not options.no_fix:
        retry_state.prepare_escalation(final_errors)
        recent_logs = "\n".join(line for _, line in (
            # pull from the last run's output if available
            getattr(proc, "all_lines", [])[-200:]
            if "proc" in dir() else []
        ))
        batch = collect_pending_fixes(
            final_errors, stack, repo_path,
            has_ai, recent_logs,
            retry_state.applied_fingerprints,
            retry_state.force_ai_fingerprints,
            retry_state.ai_invocation_count,
        )

        if batch:
            # Show what we're about to apply
            fix_summaries = [
                (e.error_type, a.description, a.source) for e, a in batch
            ]
            display.batch_fix_panel(fix_summaries)

            # In assist mode, ask the user before bulk-applying
            confirmed = True
            if options.mode == "assist" and not options.auto_approve:
                confirmed = display.prompt_confirm(
                    f"Apply {len(batch)} last-resort fix{'es' if len(batch) != 1 else ''}?"
                )

            if confirmed:
                batch_ok = True
                for err, action in batch:
                    _apply_fix_env(action, env, options.mode, options.auto_approve)
                    ok = apply_fix_commands(action, repo_path, env, debug)
                    memory.record_fix(err, action, ok, str(stack.runtime))
                    retry_state.applied_fingerprints.add(err.fingerprint())
                    if ok:
                        fix_count += 1
                    else:
                        batch_ok = False

                if batch_ok:
                    display.step("Last-resort fixes applied — retrying pipeline…")
                    display.rule()
                    lr_result = _last_resort_pipeline(
                        commands, repo_path, env, stack, source, debug, fix_count, start_time
                    )
                    if lr_result is not None:
                        memory.record_run(source, str(stack.runtime), True, lr_result.duration_s, lr_result.fix_count)
                        return lr_result
                    # If None, last-resort run also failed — fall through to failure panel

    # All attempts exhausted
    duration = time.monotonic() - start_time
    suggestions = build_suggestions(final_errors, stack)
    error_summary = final_errors[0].description if final_errors else "Unknown failure"

    display.failure_panel(error_summary, suggestions)
    memory.record_run(source, str(stack.runtime), False, duration, fix_count)

    return RunResult(
        success=False,
        repo_path=repo_path,
        stack=stack,
        commands=commands,
        duration_s=duration,
        fix_count=fix_count,
        error_summary=error_summary,
        suggestions=suggestions,
    )


# ── Binary crash diagnosis ────────────────────────────────────────────────────

import re as _re

_GLIBC_RE = _re.compile(
    r"(GLIBC_[\d.]+)['\"]?\s+not found",
    _re.IGNORECASE,
)


def _glibc_required_version(output: str) -> str | None:
    """Return the required GLIBC version string from crash output, or None."""
    m = _GLIBC_RE.search(output)
    return m.group(1) if m else None


def _glibc_system_version() -> str:
    """Return the system GLIBC version string (e.g. '2.35'), or 'unknown'."""
    import subprocess as _sp
    try:
        ldd_out = _sp.run(
            ["ldd", "--version"], capture_output=True, text=True
        ).stdout
        sys_ver = _re.search(r"(\d+\.\d+)", ldd_out)
        return sys_ver.group(1) if sys_ver else "unknown"
    except Exception:
        return "unknown"


def _glibc_fallback_artifacts(scan) -> list:
    """
    Return alternative artifacts that avoid a GLIBC dependency, ordered by
    preference: musl-linked binaries first, then AppImages.
    """
    musl_alts = [
        a for a in scan.available
        if a is not scan.best and "musl" in a.name.lower()
    ]
    appimage_alts = [
        a for a in scan.available
        if a is not scan.best and a.format == "appimage"
    ]
    return musl_alts + appimage_alts


def _diagnose_binary_crash(
    output: str,
    exit_code: int,
    scan,  # ArtifactScan
) -> tuple[str, list[str]]:
    """
    Inspect the binary's crash output and return a (summary, suggestions) pair.
    Handles common failure modes: GLIBC mismatch, permission denied, missing deps.
    """
    suggestions: list[str] = []

    # GLIBC version mismatch — most common binary incompatibility
    required = _glibc_required_version(output)
    if required:
        sys_ver_str = _glibc_system_version()
        summary = f"Binary requires {required} but system has GLIBC {sys_ver_str}"

        fallbacks = _glibc_fallback_artifacts(scan)
        musl_alts     = [a for a in fallbacks if "musl" in a.name.lower()]
        appimage_alts = [a for a in fallbacks if a.format == "appimage"]

        if musl_alts:
            suggestions.append(
                f"Try a statically-linked variant: [bold]{musl_alts[0].name}[/bold] "
                f"(re-run and select it from the list)"
            )
        if appimage_alts:
            suggestions.append(
                f"Try the AppImage (no GLIBC requirement): "
                f"[bold]{appimage_alts[0].name}[/bold]"
            )
        suggestions.append(
            f"Upgrade your system GLIBC to {required} "
            f"(requires a newer OS/distro)"
        )
        suggestions.append("Run from source instead: [bold]repofix run <url> --source[/bold]")
        return summary, suggestions

    # Permission denied
    if "permission denied" in output.lower() or exit_code == 126:
        return (
            "Binary permission denied (exit 126)",
            ["Run: [bold]chmod +x <binary_path>[/bold] and retry"],
        )

    # Missing shared library (not GLIBC specifically)
    lib_m = _re.search(r"error while loading shared libraries: ([^\s:]+)", output, _re.I)
    if lib_m:
        lib = lib_m.group(1)
        return (
            f"Missing shared library: {lib}",
            [
                f"Install the library: [bold]sudo apt-get install {lib.split('.so')[0].lstrip('lib')}[/bold]",
                "Or run from source instead: [bold]repofix run <url> --source[/bold]",
            ],
        )

    # Generic crash
    return (
        f"Binary exited immediately (code {exit_code})",
        ["Run from source instead: [bold]repofix run <url> --source[/bold]"],
    )


# ── Deployment mode detection & execution ────────────────────────────────────

_DEPLOY_NODE_PKG_RE = re.compile(r"\b(?:npm|pnpm|npx|yarn)\b", re.IGNORECASE)


def _deploy_steps_use_node_tooling(steps: list) -> bool:
    """True if any deploy step invokes npm/pnpm/yarn (needs clone-local npm prefix)."""
    return any(_DEPLOY_NODE_PKG_RE.search(getattr(s, "command", None) or "") for s in steps)


def _deploy_steps_use_npm_global(steps: list) -> bool:
    return any(
        display.command_uses_npm_global_install(getattr(s, "command", None) or "")
        for s in steps
    )


def _sudo_wrap_npm_global_command(cmd: str) -> str:
    """Prefix a shell command with sudo for machine-wide global npm installs."""
    c = (cmd or "").strip()
    if not c:
        return cmd
    if c.lower().startswith("sudo "):
        return cmd
    return f"sudo {c}"


def _resolve_npm_global_machine_unwritable(
    *,
    auto_approve: bool,
) -> str:
    """
    User chose machine-wide but npm prefix is not writable.
    Returns: isolated | sudo | abort
    """
    return display.prompt_npm_global_prefix_unwritable(auto_approve=auto_approve)


def _check_deploy_mode(
    repo_path: Path,
    source: str,
    options: RunOptions,
    start_time: float,
) -> RunResult | None:
    """
    Detect prod vs dev deployment modes.  When multiple modes are found and
    no explicit --prod/--dev flag was passed, prompt the user.

    Returns:
      - A completed RunResult if a deployment mode was fully executed.
      - None to signal the caller should continue with the normal source pipeline.
    """
    from repofix.detection.deploy_mode import detect as detect_modes
    from repofix.core.executor import run_interactive, run_long_lived
    from repofix.detection.stack import StackInfo
    from repofix.detection.commands import CommandSet

    # --dev flag: bypass detection, use the normal auto-detect pipeline
    if options.deploy_mode == "dev":
        return None

    display.step("Detecting deployment modes…")
    deploy_opts = detect_modes(repo_path)

    if not deploy_opts.has_multiple():
        # Zero or one mode found — nothing to prompt about, continue normally
        return None

    # ── Present the choice ────────────────────────────────────────────────────
    display.deploy_mode_panel(deploy_opts)

    if options.deploy_mode == "prod":
        chosen_key = "prod"
    else:
        chosen_key = display.prompt_deploy_mode(deploy_opts)

    chosen = deploy_opts.get(chosen_key)
    if chosen is None:
        return None

    if chosen_key == "dev":
        # Dev mode: use the normal auto-detect pipeline (it handles install/run/fix-retry)
        display.info(f"Running in [bold]dev[/bold] mode — using auto-detected pipeline…")
        # Ensure downstream stack/command discovery prefers source over Docker
        # when the user selected "dev" from the interactive prompt.
        options.deploy_mode = "dev"
        return None

    # ── Execute prod (or any explicitly-selected) mode steps ─────────────────
    display.rule()
    display.step(
        f"Running [bold]{chosen.label}[/bold] "
        f"[dim]({len(chosen.steps)} step{'s' if len(chosen.steps) != 1 else ''})[/dim]"
    )

    _debug = options.mode == "debug"
    env: dict[str, str] = {}
    deploy_npm_sudo_global = False
    if _deploy_steps_use_node_tooling(chosen.steps):
        use_isolated_prefix = True
        if _deploy_steps_use_npm_global(chosen.steps):
            scope = display.prompt_npm_global_scope(auto_approve=options.auto_approve)
            use_isolated_prefix = scope == "isolated"
            if not use_isolated_prefix and not venv_mgr.machine_npm_global_prefix_writable(repo_path):
                resolution = _resolve_npm_global_machine_unwritable(
                    auto_approve=options.auto_approve,
                )
                if resolution == "abort":
                    display.error(
                        "Aborted — npm global directory is not writable without sudo or an isolated prefix."
                    )
                    return RunResult(
                        success=False,
                        repo_path=repo_path,
                        stack=StackInfo(language=chosen_key),
                        commands=CommandSet(),
                        duration_s=time.monotonic() - start_time,
                        error_summary="npm global install aborted (prefix not writable)",
                    )
                if resolution == "isolated":
                    use_isolated_prefix = True
                else:
                    deploy_npm_sudo_global = True
                    use_isolated_prefix = False
        if use_isolated_prefix:
            # Clone-local prefix avoids EACCES on /usr/local for ``npm install -g``.
            env.update(venv_mgr.setup(repo_path, StackInfo(runtime="node")))

    def _deploy_cmd(cmd: str) -> str:
        if deploy_npm_sudo_global and display.command_uses_npm_global_install(cmd):
            return _sudo_wrap_npm_global_command(cmd)
        return cmd

    # Identify which step is the long-lived "run" step:
    # it's the last non-daemon step. Daemon steps (docker compose -d etc.) exit quickly.
    non_daemon = [s for s in chosen.steps if not s.daemon]
    run_step   = non_daemon[-1] if non_daemon else None
    setup_steps = [s for s in chosen.steps if s is not run_step]

    # Execute setup steps first
    for step in setup_steps:
        label = f" [dim]({step.label})[/dim]" if step.label else ""
        display.step(f"[bold]{step.command[:80]}[/bold]{label}")

        if step.interactive:
            display.info("This step requires your input…")
            code = run_interactive(_deploy_cmd(step.command), cwd=repo_path, env=env or None)
            if code != 0:
                display.warning(f"Step exited with code {code} — continuing")
        else:
            result = _run_step(_deploy_cmd(step.command), repo_path, env, _debug)
            if not result.succeeded:
                display.warning(f"Step failed (exit {result.exit_code}) — continuing")

    if run_step is None:
        # All steps are daemons / background — nothing long-lived to track
        duration = time.monotonic() - start_time
        display.success_panel(
            None,
            {
                "Mode":     chosen.label,
                "Steps":    str(len(chosen.steps)),
                "Duration": f"{duration:.1f}s",
            },
        )
        _maybe_npm_global_cli_hint(repo_path, env, *[s.command for s in chosen.steps])
        memory.record_run(source, chosen_key, True, duration)
        return RunResult(
            success=True,
            repo_path=repo_path,
            stack=StackInfo(language=chosen_key),
            commands=CommandSet(),
            duration_s=duration,
        )

    # ── Launch the run step as a long-lived process ───────────────────────────
    from repofix.core.process_registry import ProcessEntry, make_log_path

    run_cmd_deploy = _deploy_cmd(run_step.command)
    label = f" [dim]({run_step.label})[/dim]" if run_step.label else ""
    display.step(f"Starting: [bold]{run_cmd_deploy}[/bold]{label}")
    display.rule()

    proc_name = _process_name(source)
    log_path  = make_log_path(proc_name)

    if run_step.interactive:
        # Interactive run step (unusual, but handle it)
        display.info("Starting app interactively — no process tracking available")
        run_interactive(run_cmd_deploy, cwd=repo_path, env=env or None)
        duration = time.monotonic() - start_time
        memory.record_run(source, chosen_key, True, duration)
        return RunResult(
            success=True,
            repo_path=repo_path,
            stack=StackInfo(language=chosen_key),
            commands=CommandSet(),
            duration_s=duration,
        )

    from repofix.core.docker_compose_bind_fix import ensure_docker_compose_bind_files

    rc_deploy = run_cmd_deploy or ""
    ensure_docker_compose_bind_files(
        repo_path,
        run_command=rc_deploy,
        stack_is_docker="docker" in rc_deploy.lower() and "compose" in rc_deploy.lower(),
    )

    proc = run_long_lived(
        run_cmd_deploy,
        repo_path,
        env=env or None,
        debug=_debug,
        log_file=log_path,
    )

    _wait_for_exit_or_ready(proc, StackInfo())

    if not proc.is_running():
        display.error(f"Process exited immediately (code {proc.exit_code()})")
        duration = time.monotonic() - start_time
        memory.record_run(source, chosen_key, False, duration)
        return RunResult(
            success=False,
            repo_path=repo_path,
            stack=StackInfo(language=chosen_key),
            commands=CommandSet(),
            duration_s=duration,
            error_summary="Process exited immediately after launch",
        )

    duration = time.monotonic() - start_time
    display.rule()
    display.success_panel(
        None,
        {
            "Mode":     chosen.label,
            "Command":  run_cmd_deploy,
            "Logs":     str(log_path),
            "Duration": f"{duration:.1f}s",
        },
    )

    entry = ProcessEntry(
        name=proc_name,
        pid=proc.pid,
        repo_url=source,
        repo_path=str(repo_path),
        run_command=run_cmd_deploy,
        log_file=str(log_path),
        started_at=time.time(),
        status="running",
        app_url=None,
        stack=chosen_key,
        port=None,
        env={},
    )
    registered_name = registry.register(entry)
    display.info(
        f"Process registered as [bold]{registered_name}[/bold] — "
        f"use [bold]repofix ps[/bold] / [bold]repofix logs {registered_name}[/bold]"
    )
    _maybe_npm_global_cli_hint(repo_path, env, *[s.command for s in chosen.steps])
    memory.record_run(source, chosen_key, True, duration)

    try:
        proc.wait_until_done()
        final_exit = proc.exit_code()
        if final_exit in (0, None):
            registry.set_status(registered_name, "stopped")
            display.success("Process completed successfully.")
        elif final_exit in (130, 143):
            registry.set_status(registered_name, "stopped")
            display.info("Process was stopped.")
        else:
            registry.set_status(registered_name, "crashed")
            display.warning("Process exited unexpectedly.")
            display.info(f"Run [bold]repofix logs {registered_name}[/bold] to diagnose.")
    except KeyboardInterrupt:
        display.info("Stopping…")
        proc.terminate()
        registry.set_status(registered_name, "stopped")
        display.success(f"Process [bold]{registered_name}[/bold] stopped.")
        display.info(
            f"Run [bold]repofix start {registered_name}[/bold] to start again "
            f"without reinstalling."
        )

    return RunResult(
        success=True,
        repo_path=repo_path,
        stack=StackInfo(language=chosen_key),
        commands=CommandSet(),
        duration_s=time.monotonic() - start_time,
    )


# ── Artifact detection & installation ────────────────────────────────────────

def _check_artifacts(
    repo_path: Path,
    source: str,
    options: RunOptions,
    start_time: float,
) -> RunResult | None:
    """
    Scan for prebuilt binaries (GitHub Releases or local artifacts).
    If found, and the user (or --binary flag) chooses binary installation,
    install/run the artifact and return a RunResult early.
    Returns None to signal the caller should continue with the source pipeline.
    """
    from repofix.detection.artifacts import scan as artifact_scan
    from repofix.core.artifact_installer import install as artifact_install

    # --source flag: skip artifact detection entirely
    if options.install_mode == "source":
        return None

    display.step("Checking for prebuilt binaries…")

    github_url = source if source.startswith(("http://", "https://")) else None
    scan = artifact_scan(github_url, repo_path)

    if not scan.has_artifacts():
        return None  # nothing found — continue with source pipeline

    # Show what we found
    display.artifacts_panel(scan)

    # Decide: binary or source
    if options.install_mode == "binary":
        chosen = "binary"
    elif options.auto_approve:
        chosen = "binary"
    else:
        chosen = display.prompt_install_mode()

    if chosen == "source":
        display.info("Running from source code…")
        return None  # continue normal pipeline

    # ── Install the best artifact ─────────────────────────────────────────────
    assert scan.best is not None
    display.step(
        f"Installing [bold]{scan.best.name}[/bold] "
        f"[dim]({scan.best.format})[/dim]…"
    )
    result = artifact_install(scan.best, auto_approve=options.auto_approve)

    if not result.success:
        display.error(f"Binary install failed: {result.error}")
        display.info("Falling back to running from source…")
        return None  # fall through to source pipeline

    duration = time.monotonic() - start_time

    # If the artifact only installs system-wide (deb/rpm/pkg) and has no run
    # command, report success without launching a long-lived process.
    if result.installed_system and not result.run_command:
        display.success_panel(
            None,
            {
                "Package": scan.best.name,
                "Format":  scan.best.format,
                "Source":  scan.best.source.replace("_", " "),
                "Duration": f"{duration:.1f}s",
            },
        )
        from repofix.detection.stack import StackInfo
        from repofix.detection.commands import CommandSet
        memory.record_run(source, "binary", True, duration)
        return RunResult(
            success=True,
            repo_path=repo_path,
            stack=StackInfo(language="binary"),
            commands=CommandSet(),
            duration_s=duration,
            used_artifact=True,
        )

    # ── Launch the binary as a long-lived process ─────────────────────────────
    if result.run_command:
        from repofix.core.executor import run_long_lived
        from repofix.detection.stack import StackInfo
        from repofix.detection.commands import CommandSet
        from repofix.core.process_registry import ProcessEntry, make_log_path

        run_cmd   = result.run_command
        proc_name = _process_name(source)
        log_path  = make_log_path(proc_name)
        _debug    = options.mode == "debug"

        display.step(f"Starting: [bold]{run_cmd}[/bold]")
        display.rule()

        proc = run_long_lived(
            run_cmd,
            repo_path,
            env=None,
            debug=_debug,
            log_file=log_path,
        )

        # Brief wait to detect immediate crashes
        import time as _t
        try:
            deadline = _t.monotonic() + 8.0
            while _t.monotonic() < deadline and proc.is_running():
                _t.sleep(0.3)
        except KeyboardInterrupt:
            display.info("Stopping binary…")
            proc.terminate()
            from repofix.detection.stack import StackInfo
            from repofix.detection.commands import CommandSet
            return RunResult(
                success=False, repo_path=repo_path,
                stack=StackInfo(language="binary"), commands=CommandSet(),
                duration_s=_t.monotonic() - start_time, used_artifact=True,
            )

        if not proc.is_running():
            # Drain the stderr/stdout reader threads before inspecting output.
            # The process may have exited before the background threads finished
            # writing to all_lines (race condition), causing the GLIBC error
            # message (and other diagnostics) to appear missing.
            # wait_until_done() on an already-dead process is instant.
            proc.wait_until_done()

            output = "\n".join(line for _, line in proc.all_lines)

            # ── GLIBC mismatch: auto-retry with a compatible alternative ──────
            required_glibc = _glibc_required_version(output)
            if required_glibc:
                sys_ver_str = _glibc_system_version()
                display.warning(
                    f"Binary requires {required_glibc} but system has GLIBC {sys_ver_str} — "
                    f"looking for a compatible alternative…"
                )
                fallbacks = _glibc_fallback_artifacts(scan)
                for alt in fallbacks:
                    kind = "musl (statically linked)" if "musl" in alt.name.lower() else "AppImage"
                    display.step(f"Trying [bold]{alt.name}[/bold] ({kind})…")
                    alt_result = artifact_install(alt, auto_approve=options.auto_approve)
                    if not alt_result.success:
                        display.warning(f"Install failed: {alt_result.error}")
                        continue

                    if not alt_result.run_command:
                        # System-wide install (deb/rpm) without a run command
                        duration = _t.monotonic() - start_time
                        display.success_panel(
                            None,
                            {
                                "Package": alt.name,
                                "Format":  alt.format,
                                "Note":    f"Fallback for GLIBC {required_glibc} mismatch",
                                "Duration": f"{duration:.1f}s",
                            },
                        )
                        from repofix.detection.stack import StackInfo
                        from repofix.detection.commands import CommandSet
                        memory.record_run(source, "binary", True, duration)
                        return RunResult(
                            success=True,
                            repo_path=repo_path,
                            stack=StackInfo(language="binary"),
                            commands=CommandSet(),
                            duration_s=duration,
                            used_artifact=True,
                        )

                    # Launch the alternative binary
                    alt_run_cmd = alt_result.run_command
                    display.step(f"Starting: [bold]{alt_run_cmd}[/bold]")
                    display.rule()

                    alt_proc = run_long_lived(
                        alt_run_cmd,
                        repo_path,
                        env=None,
                        debug=_debug,
                        log_file=log_path,
                    )

                    try:
                        deadline2 = _t.monotonic() + 8.0
                        while _t.monotonic() < deadline2 and alt_proc.is_running():
                            _t.sleep(0.3)
                    except KeyboardInterrupt:
                        display.info("Stopping binary…")
                        alt_proc.terminate()
                        from repofix.detection.stack import StackInfo
                        from repofix.detection.commands import CommandSet
                        return RunResult(
                            success=False, repo_path=repo_path,
                            stack=StackInfo(language="binary"), commands=CommandSet(),
                            duration_s=_t.monotonic() - start_time, used_artifact=True,
                        )

                    if not alt_proc.is_running():
                        alt_proc.wait_until_done()
                        exit_code = alt_proc.exit_code() or 0
                        # User-initiated stop (clean exit, SIGINT, SIGTERM) — don't
                        # treat as a failure; just propagate the interrupt upward.
                        if exit_code in (0, 130, 143):
                            from repofix.detection.stack import StackInfo
                            from repofix.detection.commands import CommandSet
                            return RunResult(
                                success=False, repo_path=repo_path,
                                stack=StackInfo(language="binary"), commands=CommandSet(),
                                duration_s=_t.monotonic() - start_time, used_artifact=True,
                            )
                        alt_output = "\n".join(line for _, line in alt_proc.all_lines)
                        display.warning(
                            f"Alternative binary also failed "
                            f"(exit {exit_code}): {alt_output[:200]}"
                        )
                        continue

                    # Alternative is running — record success
                    duration = _t.monotonic() - start_time
                    display.rule()
                    display.success_panel(
                        None,
                        {
                            "Binary":   alt.name,
                            "Format":   alt.format,
                            "Command":  alt_run_cmd,
                            "Logs":     str(log_path),
                            "Duration": f"{duration:.1f}s",
                        },
                    )
                    from repofix.detection.stack import StackInfo
                    from repofix.detection.commands import CommandSet
                    from repofix.core.process_registry import ProcessEntry
                    entry = ProcessEntry(
                        name=proc_name,
                        pid=alt_proc.pid,
                        repo_url=source,
                        repo_path=str(repo_path),
                        run_command=alt_run_cmd,
                        log_file=str(log_path),
                        started_at=_t.time(),
                        status="running",
                        app_url=None,
                        stack="binary",
                        port=None,
                        env={},
                    )
                    registered_name = registry.register(entry)
                    display.info(
                        f"Process registered as [bold]{registered_name}[/bold] — "
                        f"use [bold]repofix ps[/bold] / [bold]repofix logs {registered_name}[/bold]"
                    )
                    memory.record_run(source, "binary", True, duration)
                    try:
                        alt_proc.wait_until_done()
                        final_exit = alt_proc.exit_code()
                        if final_exit in (0, None):
                            registry.set_status(registered_name, "stopped")
                            display.success("Binary process completed successfully.")
                        elif final_exit in (130, 143):
                            registry.set_status(registered_name, "stopped")
                            display.info("Binary process was stopped.")
                        else:
                            registry.set_status(registered_name, "crashed")
                            display.warning("Binary process exited unexpectedly.")
                            display.info(f"Run [bold]repofix logs {registered_name}[/bold] to diagnose.")
                    except KeyboardInterrupt:
                        display.info("Stopping binary…")
                        alt_proc.terminate()
                        registry.set_status(registered_name, "stopped")
                        display.success(f"Process [bold]{registered_name}[/bold] stopped.")
                        display.info(
                            f"Run [bold]repofix start {registered_name}[/bold] to start again."
                        )
                    return RunResult(
                        success=True,
                        repo_path=repo_path,
                        stack=StackInfo(language="binary"),
                        commands=CommandSet(),
                        duration_s=_t.monotonic() - start_time,
                        used_artifact=True,
                    )

                # All alternatives exhausted — fall back to source pipeline
                display.info("No compatible prebuilt binary found — falling back to running from source…")
                return None

            # ── Other crash: diagnose and report ──────────────────────────────
            error_summary, suggestions = _diagnose_binary_crash(
                output, proc.exit_code() or 1, scan
            )
            display.failure_panel(error_summary, suggestions)
            duration = _t.monotonic() - start_time
            memory.record_run(source, "binary", False, duration)
            from repofix.detection.stack import StackInfo
            from repofix.detection.commands import CommandSet
            return RunResult(
                success=False,
                repo_path=repo_path,
                stack=StackInfo(language="binary"),
                commands=CommandSet(),
                duration_s=duration,
                error_summary=error_summary,
                suggestions=suggestions,
                used_artifact=True,
            )

        duration = _t.monotonic() - start_time
        display.rule()
        display.success_panel(
            None,
            {
                "Binary":   scan.best.name,
                "Format":   scan.best.format,
                "Command":  run_cmd,
                "Logs":     str(log_path),
                "Duration": f"{duration:.1f}s",
            },
        )

        entry = ProcessEntry(
            name=proc_name,
            pid=proc.pid,
            repo_url=source,
            repo_path=str(repo_path),
            run_command=run_cmd,
            log_file=str(log_path),
            started_at=time.time(),
            status="running",
            app_url=None,
            stack="binary",
            port=None,
            env={},
        )
        registered_name = registry.register(entry)
        display.info(
            f"Process registered as [bold]{registered_name}[/bold] — "
            f"use [bold]repofix ps[/bold] / [bold]repofix logs {registered_name}[/bold]"
        )
        memory.record_run(source, "binary", True, duration)

        try:
            proc.wait_until_done()
            registry.set_status(registered_name, "crashed")
            display.warning("Binary process exited unexpectedly.")
            display.info(f"Run [bold]repofix logs {registered_name}[/bold] to diagnose.")
        except KeyboardInterrupt:
            display.info("Stopping binary…")
            proc.terminate()
            registry.set_status(registered_name, "stopped")
            display.success("Stopped.")
            display.info(
                f"Run [bold]repofix start {registered_name}[/bold] to start again."
            )

        from repofix.detection.stack import StackInfo
        from repofix.detection.commands import CommandSet
        return RunResult(
            success=True,
            repo_path=repo_path,
            stack=StackInfo(language="binary"),
            commands=CommandSet(),
            duration_s=time.monotonic() - start_time,
            used_artifact=True,
        )

    return None  # should not reach here, but fall through to source just in case


# ── Helpers ───────────────────────────────────────────────────────────────────

def _last_resort_pipeline(
    commands: CommandSet,
    repo_path: Path,
    env: dict[str, str],
    stack: StackInfo,
    source: str,
    debug: bool,
    fix_count: int,
    start_time: float,
) -> RunResult | None:
    """
    Run one final install → build → run attempt after the last-resort batch fix.
    Returns a successful RunResult if the app starts, or None on failure.
    """
    from repofix.core.executor import run_long_lived

    # Install
    if commands.install:
        with display.live_step(commands.install):
            lr_install = _run_step(commands.install, repo_path, env, debug)
        if not lr_install.succeeded:
            alt_install = suggest_node_install_after_make_shell_bug(
                repo_path, commands.install, lr_install.full_output
            )
            if alt_install:
                display.warning(
                    "Make failed: duplicate [bold]-c[/bold] in Makefile shell flags. "
                    f"Retrying with [bold]{alt_install}[/bold]…"
                )
                with display.live_step(alt_install):
                    lr_install = _run_step(alt_install, repo_path, env, debug)
                if lr_install.succeeded:
                    commands.install = alt_install
        if not lr_install.succeeded:
            return None

    # Build
    if commands.build:
        with display.live_step(commands.build):
            lr_build = _run_step(commands.build, repo_path, env, debug)
        if not lr_build.succeeded:
            return None

    from repofix.core.docker_compose_bind_fix import ensure_docker_compose_bind_files

    ensure_docker_compose_bind_files(
        repo_path,
        run_command=commands.run,
        stack_is_docker=stack.is_docker(),
    )

    # Run
    lr_collected: list[tuple[str, str]] = []
    lr_proc = run_long_lived(
        commands.run, repo_path, env=env, debug=debug,
        on_line=lambda src, ln: lr_collected.append((src, ln)),
        log_file=make_log_path(_process_name(source)),
    )
    _wait_for_exit_or_ready(lr_proc, stack)

    if not lr_proc.is_running():
        return None

    app_url = _detect_app_url(stack, env, lr_collected)
    duration = time.monotonic() - start_time
    display.rule()
    display.success_panel(
        app_url,
        {
            "Stack": f"{stack.language} / {stack.framework}",
            "Duration": f"{duration:.1f}s",
            "Fixes applied": str(fix_count),
            "Logs": str(make_log_path(_process_name(source))),
        },
    )
    try:
        lr_proc.wait_until_done()
    except KeyboardInterrupt:
        lr_proc.terminate()

    return RunResult(
        success=True,
        repo_path=repo_path,
        stack=stack,
        commands=commands,
        duration_s=duration,
        fix_count=fix_count,
        app_url=app_url,
    )


def _apply_fix_env(
    action: FixAction,
    env: dict[str, str],
    mode: str,
    auto_approve: bool,
) -> None:
    """
    Apply env_updates and port_override from a FixAction into the live env dict.
    Called for all three pipeline steps (install / build / run) so that fixes
    like memory_limit (NODE_OPTIONS) or ssl_error (NODE_TLS_REJECT_UNAUTHORIZED)
    actually take effect on the next retry attempt.
    """
    if action.port_override:
        from repofix.env.port import resolve_port
        actual_port = resolve_port(action.port_override, auto_approve, mode)
        env["PORT"] = str(actual_port)

    for var, val in action.env_updates.items():
        if not val:
            val = display.prompt_value(var)
        env[var] = val


_SENSITIVE_KEY_RE = re.compile(
    r"(key|token|secret|password|passwd|pwd|api_key|apikey|auth)", re.I
)


def _is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(key))


def _process_name(source: str) -> str:
    """Derive a short slug from a repo URL or local path."""
    # Strip trailing slashes and .git suffix
    slug = source.rstrip("/").removesuffix(".git")
    # Take the last path component
    slug = slug.split("/")[-1].split("\\")[-1]
    # Sanitize to alphanumeric + dashes
    slug = re.sub(r"[^a-zA-Z0-9-]", "-", slug).strip("-") or "app"
    return slug[:40]


def _maybe_npm_global_cli_hint(
    repo_path: Path,
    env: dict[str, str],
    *command_strings: str | None,
) -> None:
    """After a run that may have used npm -g, tell the user how to invoke the CLI."""
    texts = [c for c in command_strings if c]
    if not any(display.command_uses_npm_global_install(c) for c in texts):
        return
    prefix = env.get("npm_config_prefix", "")
    display.npm_global_cli_hint(
        repo_path,
        npm_prefix_is_repo=(prefix == str(repo_path)),
    )


def _run_step(command: str, repo_path: Path, env: dict, debug: bool):
    from repofix.core.executor import run_command
    return run_command(command, cwd=repo_path, env=env, stream=True, debug=debug)


def _multi_service_proc_healthy(proc) -> bool:
    """True if a service is still running or finished with exit code 0 (e.g. one-shot build)."""
    if proc.is_running():
        return True
    ec = proc.exit_code()
    return ec == 0 if ec is not None else False


def _wait_for_exit_or_ready(proc, stack: StackInfo, timeout: float = 15.0) -> None:
    """
    Wait up to `timeout` seconds. Stop early if the process dies or
    if we see a "ready" indicator in the logs.
    """
    import time

    _READY_SIGNALS = [
        "listening on", "running on", "started on", "ready on",
        "server running", "app running", "started server",
        "local:   http", "localhost:", "127.0.0.1:",
        "ready in ", "compiled successfully",
        "application started", "uvicorn running",
    ]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not proc.is_running():
            return
        recent = "\n".join(proc.all_lines[-1][1] for _ in range(1) if proc.all_lines)
        full_recent = " ".join(line for _, line in proc.all_lines[-10:]).lower()
        if any(sig in full_recent for sig in _READY_SIGNALS):
            return
        time.sleep(0.3)


def _detect_default_port(stack: StackInfo) -> int | None:
    port_map = {
        "next.js": 3000, "react": 3000, "vue": 5173, "angular": 4200,
        "svelte": 5173, "vite": 5173, "nuxt": 3000, "remix": 3000,
        "express": 3000, "fastify": 3000, "nestjs": 3000,
        "flask": 5000, "fastapi": 8000, "django": 8000,
        "rails": 3000, "sinatra": 4567,
        "go": 8080, "gin": 8080, "echo": 8080,
        "actix": 8080, "axum": 3000,
        "laravel": 8000,
        "spring boot": 8080,
    }
    key = stack.framework.lower()
    return port_map.get(key)


_URL_FROM_LOG_RE = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1):(\d+)",
    re.I,
)


def _detect_app_url(
    stack: StackInfo,
    env: dict[str, str],
    log_lines: list[tuple[str, str]] | None = None,
) -> str | None:
    # 1. Scan recent log output for the port the app actually bound to.
    #    Most frameworks print something like "Local: http://localhost:3001"
    if log_lines:
        for _, line in reversed(log_lines[-50:]):
            m = _URL_FROM_LOG_RE.search(line)
            if m:
                return f"http://localhost:{m.group(1)}"

    # 2. Fall back to PORT env var (set by port-conflict resolution).
    if env.get("PORT"):
        return f"http://localhost:{env['PORT']}"

    # 3. Last resort: framework default.
    default = _detect_default_port(stack)
    if default:
        return f"http://localhost:{default}"

    return None


# ── Multi-service orchestration ───────────────────────────────────────────────

@dataclass
class _PreparedService:
    spec: ServiceSpec
    stack: StackInfo
    commands: CommandSet
    env: dict[str, str]
    port: int | None


def _prepare_service(
    spec: ServiceSpec,
    options: RunOptions,
    has_ai: bool,
    debug: bool,
    used_ports: set[int],
) -> _PreparedService | None:
    """Detect stack, commands, venv, and env for a single service directory."""
    from repofix.fixing import ai_fixer

    display.step(f"[bold {spec.log_color}][{spec.name}][/bold {spec.log_color}] Detecting stack…")

    ai_stack_fn = ai_fixer.detect_stack_from_readme if has_ai else None
    stack = stack_detect.detect(spec.path, readme_ai_fallback=ai_stack_fn)
    if options.deploy_mode == "dev" and stack.runtime == "docker":
        stack = stack_detect.detect_without_docker(spec.path, readme_ai_fallback=ai_stack_fn)

    if debug:
        display.detection_panel({f"[{spec.name}] {k}": v for k, v in stack.as_display_dict().items()})

    ai_cmd_fn = ai_fixer.extract_commands_from_readme if has_ai else None
    commands = cmd_detect.discover(spec.path, stack, readme_ai_fallback=ai_cmd_fn)

    if not commands.run:
        display.warning(f"[{spec.name}] Could not determine run command — skipping this service")
        return None

    # Isolated env setup
    isolation_env = venv_mgr.setup(spec.path, stack)

    # Env vars
    env = env_manager.resolve_env(
        spec.path,
        extra_env_file=options.env_file,
        auto_approve=options.auto_approve,
        mode=options.mode,
    )
    env = {**env, **isolation_env}

    # Port — pick a free one that isn't already claimed by another service
    desired = options.port if spec.role == "frontend" else None
    desired = desired or _detect_default_port(stack)
    if desired:
        while desired in used_ports:
            desired += 1
        actual = resolve_port(desired, auto_approve=options.auto_approve, mode=options.mode)
    else:
        actual = find_free_port()
    used_ports.add(actual)
    env["PORT"] = str(actual)

    return _PreparedService(spec=spec, stack=stack, commands=commands, env=env, port=actual)


def _run_multi(
    repo_path: Path,
    source: str,
    services: list[ServiceSpec],
    options: RunOptions,
    start_time: float,
    app_cfg,
    has_ai: bool,
    debug: bool,
) -> RunResult:
    """Prepare and launch all services concurrently."""
    from repofix.core.executor import run_command, run_long_lived

    # ── Prepare each service (detect, install, build) ─────────────────────────
    prepared: list[_PreparedService] = []
    used_ports: set[int] = set()

    for spec in services:
        svc = _prepare_service(spec, options, has_ai, debug, used_ports)
        if svc is None:
            continue
        prepared.append(svc)

    if not prepared:
        display.failure_panel("No runnable services found", ["Check subdirectory structure"])
        return RunResult(
            success=False, repo_path=repo_path,
            stack=StackInfo(), commands=CommandSet(),
            duration_s=time.monotonic() - start_time,
            error_summary="No runnable services found",
        )

    _install_cap = max(3, options.max_retries)
    fix_count = 0
    multi_ai_invocation_count: dict[str, int] = {}

    # ── Branch cache (same dep fingerprint as single-service runs) ────────────
    _repo_key = branch_cache.normalize_repo_key(source, repo_path)
    _current_branch = branch_cache.get_current_branch(repo_path)
    _dep_hash, _dep_files = branch_cache.compute_dep_hash(repo_path)
    _venv_dir_name = branch_cache.branch_venv_name(_current_branch)
    _root_stack = stack_detect.detect_without_docker(repo_path) if options.deploy_mode == "dev" else stack_detect.detect(repo_path)
    if not _root_stack.is_known():
        _root_stack = prepared[0].stack
    _cached_branch = memory.get_branch_state(_repo_key, _current_branch)
    _branch_cache_hit = (
        _cached_branch is not None
        and _cached_branch["dep_hash"] == _dep_hash
        and _cached_branch["install_success"]
        and branch_cache.is_env_valid(
            repo_path,
            _root_stack.runtime,
            _cached_branch.get("env_dir", ""),
        )
    )
    _build_cache_hit = _branch_cache_hit and bool(_cached_branch and _cached_branch.get("build_success"))

    if _branch_cache_hit:
        display.branch_cache_hit(_current_branch, _dep_hash[:8], _cached_branch["installed_when"])
    else:
        if _cached_branch:
            display.branch_cache_miss(_current_branch, reason="deps changed")
        else:
            display.branch_cache_miss(_current_branch, reason="no cache yet")

    _multi_meta = {"mode": "multi-service", "services": [s.spec.name for s in prepared]}
    _env_dir_str = str(venv_mgr.venv_path(repo_path, _venv_dir_name))
    install_was_saved = False

    # ── Workspace root install (npm/pnpm) — one shot for the whole tree ─────────
    # Without this, every `npm install` under packages/* re-runs the root
    # prepare script (e.g. husky) and fails the same way on each service.
    workspace_monorepo = cmd_detect.is_npm_workspace_root(repo_path)
    root_install_ok = not workspace_monorepo

    if workspace_monorepo:
        display.rule()
        ws_stack = (
            stack_detect.detect_without_docker(repo_path)
            if options.deploy_mode == "dev"
            else stack_detect.detect(repo_path)
        )
        if _branch_cache_hit:
            display.step("Workspace root — [bold]skipping install[/bold] (branch cache hit)")
            root_install_ok = True
            for svc in prepared:
                if svc.stack.runtime.lower() in ("node", "npm") and svc.spec.path.resolve() != repo_path.resolve():
                    svc.commands.install = None
        else:
            display.step("Workspace root detected — installing dependencies once at repo root…")
            ws_env = env_manager.resolve_env(
                repo_path,
                extra_env_file=options.env_file,
                auto_approve=options.auto_approve,
                mode=options.mode,
            )
            ws_env = {**ws_env, **venv_mgr.setup(repo_path, ws_stack)}
            root_install = cmd_detect.node_install_command(repo_path)
            root_install_ok = False
            for _root_attempt in range(_install_cap):
                display.step(f"[bold]{root_install}[/bold] @ {repo_path.name}/")
                r_result = run_command(root_install, cwd=repo_path, env=ws_env, stream=True, debug=debug)
                if r_result.succeeded:
                    root_install_ok = True
                    break
                r_signals = detect_errors(r_result.all_lines)
                r_errors = classify_all(r_signals, ws_stack.runtime)
                r_fix = pick_and_validate_fix(
                    r_errors, ws_stack, repo_path, options.mode,
                    options.auto_approve, has_ai,
                    r_result.full_output,
                    set(),
                    ai_invocation_count=multi_ai_invocation_count,
                )
                if r_fix.attempted and r_fix.action:
                    display.info(f"Applying fix: {r_fix.action.description}")
                    ok = apply_fix_commands(r_fix.action, repo_path, ws_env, debug)
                    if ok:
                        fix_count += 1
                        if r_fix.action.next_step == "run":
                            root_install_ok = True
                            break
                        continue
                display.warning("Workspace root install failed — continuing with per-service installs")
                break
            if root_install_ok:
                for svc in prepared:
                    if svc.stack.runtime.lower() in ("node", "npm") and svc.spec.path.resolve() != repo_path.resolve():
                        svc.commands.install = None

    # ── Install deps for each service sequentially ────────────────────────────
    _per_service_install_warned = False
    if _branch_cache_hit:
        display.step(
            f"Skipping per-service installs — branch [bold]{_current_branch}[/bold] "
            f"[dim]({_dep_hash[:8]})[/dim]"
        )
    else:
        display.rule()
        display.step("Installing dependencies for all services…")
        for svc in prepared:
            if svc.commands.install:
                _svc_install_cmd = svc.commands.install
                for _install_attempt in range(_install_cap):
                    display.step(
                        f"[bold {svc.spec.log_color}][{svc.spec.name}][/bold {svc.spec.log_color}] "
                        f"{_svc_install_cmd}"
                    )
                    result = run_command(_svc_install_cmd, cwd=svc.spec.path, env=svc.env, stream=True, debug=debug)
                    if result.succeeded:
                        break
                    # Try a targeted fix (e.g. npm lifecycle failure → --ignore-scripts)
                    _svc_signals = detect_errors(result.all_lines)
                    _svc_errors = classify_all(_svc_signals, svc.stack.runtime)
                    _svc_fix = pick_and_validate_fix(
                        _svc_errors, svc.stack, svc.spec.path, options.mode,
                        options.auto_approve, has_ai,
                        result.full_output,
                        set(),
                        ai_invocation_count=multi_ai_invocation_count,
                    )
                    if _svc_fix.attempted and _svc_fix.action:
                        display.info(
                            f"[{svc.spec.name}] Applying fix: {_svc_fix.action.description}"
                        )
                        ok = apply_fix_commands(_svc_fix.action, svc.spec.path, svc.env, debug)
                        if ok:
                            fix_count += 1
                            if _svc_fix.action.next_step == "run":
                                break
                            continue
                        break
                    else:
                        display.warning(
                            f"[{svc.spec.name}] Install failed (exit {result.exit_code}) — will attempt to run anyway"
                        )
                        _per_service_install_warned = True
                        break

        if root_install_ok and not _per_service_install_warned:
            memory.save_branch_state(
                repo_key=_repo_key,
                branch=_current_branch,
                dep_hash=_dep_hash,
                env_dir=_env_dir_str,
                stack_json=json.dumps(_root_stack.as_display_dict()),
                commands_json=json.dumps(_multi_meta),
                dep_files=json.dumps(_dep_files),
                install_success=True,
                build_success=False,
            )
            install_was_saved = True

    # ── Build steps ───────────────────────────────────────────────────────────
    _build_phase_ok = True
    if _build_cache_hit:
        display.step(
            f"Skipping builds — branch [bold]{_current_branch}[/bold] "
            f"build cached [dim]({_dep_hash[:8]})[/dim]"
        )
    else:
        for svc in prepared:
            if not svc.commands.build:
                continue
            _svc_build_cmd = svc.commands.build
            for _build_attempt in range(_install_cap):
                display.step(
                    f"[bold {svc.spec.log_color}][{svc.spec.name}][/bold {svc.spec.log_color}] "
                    f"{_svc_build_cmd}"
                )
                result = run_command(_svc_build_cmd, cwd=svc.spec.path, env=svc.env, stream=True, debug=debug)
                if result.succeeded:
                    break

                _svc_signals = detect_errors(result.all_lines)
                _svc_errors = classify_all(_svc_signals, svc.stack.runtime)
                _svc_fix = pick_and_validate_fix(
                    _svc_errors, svc.stack, svc.spec.path, options.mode,
                    options.auto_approve, has_ai,
                    result.full_output,
                    set(),
                    ai_invocation_count=multi_ai_invocation_count,
                )
                if _svc_fix.attempted and _svc_fix.action:
                    _apply_fix_env(_svc_fix.action, svc.env, options.mode, options.auto_approve)
                    display.info(
                        f"[{svc.spec.name}] Applying fix: {_svc_fix.action.description}"
                    )
                    ok = apply_fix_commands(_svc_fix.action, svc.spec.path, svc.env, debug)
                    if ok:
                        fix_count += 1
                        continue
                    break

                display.warning(f"[{svc.spec.name}] Build failed — will attempt to run anyway")
                _build_phase_ok = False
                break

        if _build_phase_ok and (_branch_cache_hit or install_was_saved):
            memory.save_branch_state(
                repo_key=_repo_key,
                branch=_current_branch,
                dep_hash=_dep_hash,
                env_dir=_env_dir_str,
                stack_json=json.dumps(_root_stack.as_display_dict()),
                commands_json=json.dumps(_multi_meta),
                dep_files=json.dumps(_dep_files),
                install_success=True,
                build_success=True,
            )

    # ── Launch all services concurrently ──────────────────────────────────────
    display.rule()
    display.step(f"Starting {len(prepared)} services…")
    display.rule()

    from repofix.core.docker_compose_bind_fix import ensure_docker_compose_bind_files

    for svc in prepared:
        rc = svc.commands.run or ""
        if svc.stack.is_docker() or ("docker" in rc.lower() and "compose" in rc.lower()):
            ensure_docker_compose_bind_files(
                svc.spec.path,
                run_command=rc,
                stack_is_docker=svc.stack.is_docker(),
            )

    procs: list[tuple[_PreparedService, object]] = []
    _start_cap = max(3, options.max_retries)

    for svc in prepared:
        color = svc.spec.log_color
        name  = svc.spec.name

        def _make_on_line(svc_name: str, svc_color: str):
            def _on_line(src: str, line: str) -> None:
                display.log_line_labeled(line, svc_name, svc_color, src)
            return _on_line

        proc = run_long_lived(
            svc.commands.run,
            svc.spec.path,
            env=svc.env,
            debug=debug,
            on_line=_make_on_line(name, color),
        )
        procs.append((svc, proc))
        display.info(
            f"[bold {color}]{name}[/bold {color}] started "
            f"(PID {proc.pid}) → [link=http://localhost:{svc.port}]http://localhost:{svc.port}[/link]"
        )

    # ── Wait, then heal crashed dev servers (missing deps, wrong entry, …) ───
    for svc, proc in procs:
        _wait_for_exit_or_ready(proc, svc.stack, timeout=35.0)

    svc_fix_fp: dict[str, set[str]] = {s.spec.name: set() for s, _ in procs}
    svc_force_ai: dict[str, set[str]] = {s.spec.name: set() for s, _ in procs}
    svc_ai_inv: dict[str, dict[str, int]] = {s.spec.name: {} for s, _ in procs}
    svc_last_ok_fix: dict[str, tuple[str, str] | None] = {
        s.spec.name: None for s, _ in procs
    }
    for _recovery in range(_start_cap):
        relaunch_idx = [i for i, (_, p) in enumerate(procs) if not p.is_running()]
        if not relaunch_idx:
            break
        progressed = False
        for i in relaunch_idx:
            svc, dead = procs[i]
            name = svc.spec.name
            changed = False
            signals = detect_errors(dead.all_lines)
            errors = classify_all(signals, svc.stack.runtime)
            if any(e.error_type == "wrong_entry_point" for e in errors):
                bad_path = next(
                    (e.extracted.get("bad_path", "") for e in errors if e.error_type == "wrong_entry_point"),
                    "",
                )
                alt = find_node_entry(svc.spec.path)
                if alt and alt.strip() != (svc.commands.run or "").strip():
                    display.info(
                        f"[{name}] Entry missing ({bad_path}) — switching to: {alt}"
                    )
                    svc.commands.run = alt
                    changed = True
                    fix_count += 1
            last_ok = svc_last_ok_fix[name]
            svc_last_ok_fix[name] = None
            note_failed_rule_memory_fix(
                last_ok, errors, svc_force_ai[name], svc_ai_inv[name]
            )
            fix_result = pick_and_validate_fix(
                errors, svc.stack, svc.spec.path, options.mode,
                options.auto_approve, has_ai,
                dead.full_output, svc_fix_fp[name],
                svc_force_ai[name],
                svc_ai_inv[name],
            )
            if fix_result.attempted and fix_result.action:
                if fix_result.error:
                    svc_fix_fp[name].add(fix_result.error.fingerprint())
                _apply_fix_env(fix_result.action, svc.env, options.mode, options.auto_approve)
                display.info(f"[{name}] Applying startup fix: {fix_result.action.description}")
                ok_fix = apply_fix_commands(fix_result.action, svc.spec.path, svc.env, debug)
                if ok_fix:
                    fix_count += 1
                    if fix_result.error:
                        svc_last_ok_fix[name] = (
                            fix_result.error.fingerprint(),
                            fix_result.action.source,
                        )
                        svc_force_ai[name].discard(fix_result.error.fingerprint())
                elif fix_result.error and fix_result.action.source == "ai":
                    update_force_ai_after_ai_fix_commands(
                        ok=ok_fix,
                        fingerprint=fix_result.error.fingerprint(),
                        force_ai_fingerprints=svc_force_ai[name],
                        ai_invocation_count=svc_ai_inv[name],
                    )
                changed = changed or ok_fix
            if not changed:
                continue
            try:
                dead.terminate()
            except Exception:
                pass
            color = svc.spec.log_color

            def _make_on_line2(svc_name: str, svc_color: str):
                def _on_line(src: str, line: str) -> None:
                    display.log_line_labeled(line, svc_name, svc_color, src)
                return _on_line

            proc = run_long_lived(
                svc.commands.run,
                svc.spec.path,
                env=svc.env,
                debug=debug,
                on_line=_make_on_line2(name, color),
            )
            procs[i] = (svc, proc)
            progressed = True
            display.info(
                f"[bold {color}]{name}[/bold {color}] restarted "
                f"(PID {proc.pid}) → http://localhost:{svc.port}"
            )
        if not progressed:
            break
        for svc, proc in procs:
            _wait_for_exit_or_ready(proc, svc.stack, timeout=35.0)

    # ── Check health ──────────────────────────────────────────────────────────
    healthy = [(s, p) for s, p in procs if _multi_service_proc_healthy(p)]
    failed = [(s, p) for s, p in procs if not _multi_service_proc_healthy(p)]
    still_running = [(s, p) for s, p in procs if p.is_running()]

    duration = time.monotonic() - start_time

    if not healthy:
        for svc, _ in failed:
            display.error(f"[{svc.spec.name}] failed on startup")
        display.failure_panel("All services failed to start", ["Check logs above for errors"])
        memory.record_run(source, "multi", False, duration, fix_count)
        return RunResult(
            success=False, repo_path=repo_path,
            stack=StackInfo(), commands=CommandSet(),
            duration_s=duration, fix_count=fix_count,
            error_summary="All services failed",
        )

    if failed:
        for svc, _ in failed:
            display.warning(
                f"[{svc.spec.name}] exited with an error — continuing with remaining services"
            )

    # ── Outcome panel ─────────────────────────────────────────────────────────
    display.rule()
    urls_summary: dict[str, str] = {}
    for svc, proc in healthy:
        if proc.is_running():
            urls_summary[svc.spec.name] = f"http://localhost:{svc.port}"
        else:
            urls_summary[svc.spec.name] = f"http://localhost:{svc.port} (completed)"

    running_only_urls = {
        s.spec.name: f"http://localhost:{s.port}"
        for s, p in healthy if p.is_running()
    }

    primary_url = next(
        (f"http://localhost:{s.port}" for s, p in still_running if s.spec.role == "frontend"),
        next((f"http://localhost:{s.port}" for s, p in still_running), None),
    )
    if primary_url is None and healthy:
        primary_url = f"http://localhost:{healthy[0][0].port}"

    failed_names = [s.spec.name for s, _ in failed]
    summary_extra = {
        "Duration": f"{duration:.1f}s",
        "Services": f"{len(healthy)}/{len(prepared)} ok ({len(still_running)} running)",
        "Fixes applied": str(fix_count),
    }

    all_ok = not failed
    if all_ok:
        display.success_panel(
            primary_url,
            {
                **{
                    f"[bold {s.spec.log_color}]{s.spec.name}[/bold {s.spec.log_color}]": urls_summary[s.spec.name]
                    for s, _ in healthy
                },
                **summary_extra,
            },
        )
        memory.record_run(source, "multi", True, duration, fix_count)
    else:
        partial_summary = dict(summary_extra)
        completed_only = [s.spec.name for s, p in healthy if not p.is_running()]
        if completed_only:
            partial_summary["Completed (exit 0)"] = ", ".join(completed_only)
        display.partial_services_panel(
            primary_url,
            running_only_urls,
            failed_names,
            partial_summary,
        )
        memory.record_run(source, "multi", False, duration, fix_count)

    for _, proc in still_running:
        try:
            proc.terminate()
        except Exception:
            pass

    return RunResult(
        success=all_ok,
        repo_path=repo_path,
        stack=StackInfo(language="multi-service"),
        commands=CommandSet(),
        duration_s=duration,
        fix_count=fix_count,
        app_url=primary_url,
        error_summary=None if all_ok else (
            f"Only {len(healthy)}/{len(prepared)} services ok "
            f"(failed: {', '.join(failed_names)})"
        ),
    )
