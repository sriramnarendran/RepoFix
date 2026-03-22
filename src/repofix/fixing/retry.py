"""Fix-retry loop — the self-healing engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from repofix.detection.stack import StackInfo
from repofix.fixing import ai_fixer
from repofix.fixing.classifier import ClassifiedError, classify_all
from repofix.fixing.detector import ErrorSignal, detect_errors
from repofix.fixing.rules import FixAction, apply_rule
from repofix.fixing.safety import UnsafeCommandError, is_safe
from repofix.memory import store as memory
from repofix.output import display

MAX_AI_FOLLOWUPS = 5
"""Max LLM fix attempts per error fingerprint in one run (invoke → apply → still failing)."""


def _can_invoke_ai(fp: str, ai_invocation_count: dict[str, int]) -> bool:
    return ai_invocation_count.get(fp, 0) < MAX_AI_FOLLOWUPS


def _record_ai_invocation(fp: str, ai_invocation_count: dict[str, int]) -> None:
    ai_invocation_count[fp] = ai_invocation_count.get(fp, 0) + 1


def update_force_ai_after_ai_fix_commands(
    *,
    ok: bool,
    fingerprint: str,
    force_ai_fingerprints: set[str],
    ai_invocation_count: dict[str, int],
) -> None:
    """After running shell commands for an AI-sourced fix, set up the next pick."""
    if ok:
        force_ai_fingerprints.discard(fingerprint)
        return
    if _can_invoke_ai(fingerprint, ai_invocation_count):
        force_ai_fingerprints.add(fingerprint)
    else:
        force_ai_fingerprints.discard(fingerprint)


def note_failed_rule_memory_fix(
    last_successful_fix: tuple[str, str] | None,
    errors: list[ClassifiedError],
    force_ai_fingerprints: set[str],
    ai_invocation_count: dict[str, int] | None = None,
) -> None:
    """
    After a fix command succeeded but the pipeline still fails with the same
    fingerprint, escalate: rule/memory → AI; AI → another LLM attempt (until cap).
    """
    if not last_successful_fix:
        return
    fp_last, src_last = last_successful_fix
    counts = ai_invocation_count if ai_invocation_count is not None else {}
    for e in errors:
        if e.fingerprint() == fp_last:
            if src_last in ("rule", "memory"):
                force_ai_fingerprints.add(fp_last)
                display.ai_action(
                    "Rule-based fix did not resolve the failure — escalating to AI…"
                )
            elif src_last == "ai":
                if _can_invoke_ai(fp_last, counts):
                    force_ai_fingerprints.add(fp_last)
                    display.ai_action(
                        "AI fix did not resolve the failure — asking the model again…"
                    )
            break


@dataclass
class FixCycleResult:
    attempted: bool = False
    succeeded: bool = False
    action: FixAction | None = None
    error: ClassifiedError | None = None
    skip_reason: str = ""


@dataclass
class RetryState:
    attempt: int = 0
    max_retries: int = 5
    applied_fingerprints: set[str] = field(default_factory=set)
    """Fingerprints for which a fix was already attempted (avoids repeating the same path)."""
    force_ai_fingerprints: set[str] = field(default_factory=set)
    """When rule/memory fix ran successfully but the same failure recurs, try AI for these fingerprints."""
    ai_invocation_count: dict[str, int] = field(default_factory=dict)
    """Per fingerprint: how many LLM fix invocations used this run (capped at MAX_AI_FOLLOWUPS)."""
    last_successful_fix: tuple[str, str] | None = None
    """``(error_fingerprint, fix_source)`` after a fix command succeeded — used to escalate to AI."""
    fix_log: list[dict] = field(default_factory=list)

    def exhausted(self) -> bool:
        return self.attempt >= self.max_retries

    def increment(self) -> None:
        self.attempt += 1

    def prepare_escalation(self, errors: list[ClassifiedError]) -> None:
        """If the pipeline still fails after a successful fix, escalate to AI / next LLM attempt."""
        if not self.last_successful_fix:
            return
        last = self.last_successful_fix
        self.last_successful_fix = None
        note_failed_rule_memory_fix(last, errors, self.force_ai_fingerprints, self.ai_invocation_count)


def pick_and_validate_fix(
    errors: list[ClassifiedError],
    stack: StackInfo,
    repo_path: Path,
    mode: str,
    auto_approve: bool,
    has_ai: bool,
    recent_logs: str,
    applied_fingerprints: set[str],
    force_ai_fingerprints: set[str] | None = None,
    ai_invocation_count: dict[str, int] | None = None,
) -> FixCycleResult:
    """
    For a list of classified errors, find the best fix action and validate it.
    Returns a FixCycleResult describing what was found.
    """
    force_ai_fps = force_ai_fingerprints if force_ai_fingerprints is not None else set()
    counts = {} if ai_invocation_count is None else ai_invocation_count

    for error in errors:
        fingerprint = error.fingerprint()
        in_force_ai = fingerprint in force_ai_fps
        if fingerprint in applied_fingerprints and not in_force_ai:
            continue

        action: FixAction | None = None
        source_label = ""

        if in_force_ai:
            # Rule/memory (or prior AI) did not resolve — try LLM again up to cap.
            if not has_ai:
                force_ai_fps.discard(fingerprint)
                return FixCycleResult(
                    attempted=False,
                    skip_reason=(
                        "Escalated to AI but no AI backend is configured (local model or a cloud API key)"
                    ),
                    error=error,
                )
            if not _can_invoke_ai(fingerprint, counts):
                force_ai_fps.discard(fingerprint)
                return FixCycleResult(
                    attempted=False,
                    skip_reason=(
                        f"AI follow-up limit reached ({MAX_AI_FOLLOWUPS} LLM attempts per error)"
                    ),
                    error=error,
                )
            _record_ai_invocation(fingerprint, counts)
            action = ai_fixer.fix_error(error, stack, repo_path, recent_logs)
            if action:
                source_label = "ai"
            if not action:
                force_ai_fps.discard(fingerprint)
                return FixCycleResult(
                    attempted=False,
                    skip_reason="Escalated to AI but no suggestion available",
                    error=error,
                )
        else:
            # 1. Check memory store
            cached = memory.lookup_fix(error)
            if cached:
                action = cached
                source_label = "memory"

            # 2. Rule-based
            if not action:
                action = apply_rule(error, stack, repo_path)
                if action:
                    source_label = "rule"

            # 3. AI fallback
            if not action and has_ai and _can_invoke_ai(fingerprint, counts):
                _record_ai_invocation(fingerprint, counts)
                action = ai_fixer.fix_error(error, stack, repo_path, recent_logs)
                if action:
                    source_label = "ai"

            if not action:
                return FixCycleResult(
                    attempted=False,
                    skip_reason=f"No fix found for {error.error_type}",
                    error=error,
                )

        # Safety gate
        for cmd in action.commands:
            safe, reason = is_safe(cmd)
            if not safe:
                display.warning(f"Skipping unsafe command '{cmd}': {reason}")
                action.commands = [c for c in action.commands if c != cmd]

        if action.is_empty() and action.port_override is None and not action.env_updates:
            if in_force_ai:
                force_ai_fps.discard(fingerprint)
            return FixCycleResult(
                attempted=False,
                skip_reason="Fix action was empty after safety filtering",
                error=error,
            )

        # Approval gate
        if mode == "assist" and not auto_approve:
            display.fix_panel(0, error.error_type, action.description, source_label)
            confirmed = display.prompt_confirm("Apply this fix?")
            if not confirmed:
                return FixCycleResult(
                    attempted=False,
                    skip_reason="User declined fix",
                    error=error,
                )

        return FixCycleResult(attempted=True, action=action, error=error)

    return FixCycleResult(attempted=False, skip_reason="All errors already attempted")


def collect_pending_fixes(
    errors: list[ClassifiedError],
    stack: StackInfo,
    repo_path: Path,
    has_ai: bool,
    recent_logs: str,
    applied_fingerprints: set[str],
    force_ai_fingerprints: set[str] | None = None,
    ai_invocation_count: dict[str, int] | None = None,
) -> list[tuple[ClassifiedError, FixAction]]:
    """
    Collect ALL applicable fix actions for a list of errors in a single pass.

    Unlike pick_and_validate_fix (which processes one error at a time inside the
    retry loop), this function gathers every fixable error into a single batch
    so they can all be applied at once as a last-resort sweep.

    Returns a list of (ClassifiedError, FixAction) pairs — in priority order
    (memory → rule → AI) — with safety filtering already applied.  Errors that
    already have applied fingerprints or have no available fix are skipped.
    """
    results: list[tuple[ClassifiedError, FixAction]] = []
    seen_fingerprints: set[str] = set(applied_fingerprints)
    force_ai_fps = force_ai_fingerprints if force_ai_fingerprints is not None else set()
    counts = {} if ai_invocation_count is None else ai_invocation_count

    for error in errors:
        fingerprint = error.fingerprint()
        in_force_ai = fingerprint in force_ai_fps
        if fingerprint in seen_fingerprints and not in_force_ai:
            continue

        action: FixAction | None = None

        if in_force_ai:
            if has_ai and _can_invoke_ai(fingerprint, counts):
                _record_ai_invocation(fingerprint, counts)
                action = ai_fixer.fix_error(error, stack, repo_path, recent_logs)
            if not action:
                force_ai_fps.discard(fingerprint)
                continue
        else:
            cached = memory.lookup_fix(error)
            if cached:
                action = cached
            if not action:
                action = apply_rule(error, stack, repo_path)
            if not action and has_ai and _can_invoke_ai(fingerprint, counts):
                _record_ai_invocation(fingerprint, counts)
                action = ai_fixer.fix_error(error, stack, repo_path, recent_logs)

        if not action:
            continue

        # Safety-filter individual commands (same as pick_and_validate_fix)
        safe_cmds = []
        for cmd in action.commands:
            safe, reason = is_safe(cmd)
            if safe:
                safe_cmds.append(cmd)
            else:
                display.warning(f"Skipping unsafe command '{cmd}': {reason}")
        action.commands = safe_cmds

        if action.is_empty() and not action.env_updates and action.port_override is None:
            continue

        results.append((error, action))
        seen_fingerprints.add(fingerprint)

    return results


def apply_fix_commands(
    action: FixAction,
    repo_path: Path,
    env: dict[str, str],
    debug: bool,
) -> bool:
    """
    Execute the shell commands in a FixAction.
    Returns True if all commands succeeded.
    """
    from repofix.core.executor import ExecutionError, run_command

    if action.run_fn is not None:
        display.step(f"Applying fix: [bold]{action.description}[/bold]")
        try:
            if not action.run_fn():
                return False
        except OSError as exc:
            display.error(f"Fix failed: {exc}")
            return False

    for cmd in action.commands:
        display.step(f"Running fix: [bold]{cmd}[/bold]")
        try:
            result = run_command(cmd, cwd=repo_path, env=env, stream=True, debug=debug)
            if not result.succeeded:
                display.error(f"Fix command failed (exit {result.exit_code}): {cmd}")
                return False
        except ExecutionError as exc:
            display.error(f"Fix command error: {exc}")
            return False

    return True


def build_suggestions(errors: list[ClassifiedError], stack: StackInfo) -> list[str]:
    suggestions: list[str] = []
    for error in errors:
        if error.error_type == "missing_dependency":
            pkg = error.extracted.get("package")
            if pkg:
                suggestions.append(f"Manually install missing package: {pkg}")
        elif error.error_type == "port_conflict":
            port = error.extracted.get("port")
            suggestions.append(f"Free up port {port} or use --port to specify another")
        elif error.error_type == "missing_env_var":
            var = error.extracted.get("var_name")
            suggestions.append(f"Set environment variable {var} (check .env.example)")
        elif error.error_type == "version_mismatch":
            required = error.extracted.get("required", "")
            runtime = stack.runtime.lower()
            if runtime == "python" and required:
                major_minor = ".".join(required.split(".")[:2])
                suggestions.append(
                    f"Install Python {major_minor}: uv python install {major_minor}  or  pyenv install {major_minor} && pyenv local {major_minor}"
                )
            elif runtime in ("node", "npm") and required:
                suggestions.append(f"Switch Node.js to {required}: nvm install {required} && nvm use {required}")
            else:
                suggestions.append(f"Check required {stack.runtime} version and switch with nvm/pyenv")
        elif error.error_type == "build_failure":
            suggestions.append("Run the build command manually with verbose output to see details")
        elif error.error_type == "ssl_error":
            suggestions.append("Run: pip install --upgrade certifi  (Python) or set NODE_TLS_REJECT_UNAUTHORIZED=0 (Node)")
        elif error.error_type == "node_openssl_legacy":
            suggestions.append(
                "Set NODE_OPTIONS=--openssl-legacy-provider or upgrade webpack/react-scripts (Node 17+ / OpenSSL 3)"
            )
        elif error.error_type == "git_remote_auth":
            suggestions.append("Verify git remote URL, GitHub access, SSH keys, or use a PAT for HTTPS")
        elif error.error_type == "pip_resolution":
            suggestions.append("Upgrade pip/setuptools; relax conflicting pins in requirements or pyproject.toml")
        elif error.error_type == "corepack_required":
            suggestions.append("Run: corepack enable  then retry install (honors package.json packageManager)")
        elif error.error_type == "package_manager_wrong":
            suggestions.append("Use yarn/pnpm/bun per the lockfile (yarn install / pnpm install), not the wrong CLI")
        elif error.error_type == "engines_strict":
            suggestions.append("Match Node in engines, or npm/pnpm config to ignore engine-strict / YARN_IGNORE_ENGINES=1")
        elif error.error_type == "glibc_toolchain":
            suggestions.append(
                "GLIBC/manylinux: use a newer Linux image, conda, or build from source — apt rarely upgrades glibc safely"
            )
        elif error.error_type == "gpu_cuda_runtime":
            suggestions.append(
                "CUDA/GPU: install NVIDIA drivers or use CPU-only PyTorch/JAX builds; Warp requires a working CUDA stack"
            )
        elif error.error_type == "git_lfs_error":
            suggestions.append("Run: sudo apt install git-lfs && git lfs install && git lfs pull")
        elif error.error_type == "playwright_browsers":
            suggestions.append("Run: npx playwright install  (from the repo root)")
        elif error.error_type == "memory_limit":
            suggestions.append("Set NODE_OPTIONS=--max-old-space-size=4096 or JAVA_TOOL_OPTIONS=-Xmx2g before running")
        elif error.error_type == "disk_space":
            suggestions.append("Run: sudo sysctl fs.inotify.max_user_watches=524288  (Linux inotify limit)")
            suggestions.append("Check actual disk space with: df -h")
        elif error.error_type == "network_error":
            conn_port = error.extracted.get("conn_port")
            host = error.extracted.get("host", "")
            suggestions.append(f"Cannot reach {host}:{conn_port} — ensure the required service is running")
        elif error.error_type == "database_error":
            db = error.extracted.get("db_type", "database")
            suggestions.append(f"Start your {db} service: sudo systemctl start {db}")
            suggestions.append("Verify DATABASE_URL / connection string in .env file")
        elif error.error_type == "peer_dependency":
            suggestions.append("Run: npm install --legacy-peer-deps  to bypass peer conflicts")
        elif error.error_type == "bundler_version":
            suggestions.append("Run: gem install bundler && bundle update --bundler")
        elif error.error_type == "system_dependency":
            lib = error.extracted.get("lib", "")
            suggestions.append(f"Install missing system library: sudo apt-get install -y {lib or 'build-essential libssl-dev'}")
        elif error.error_type == "compiler_error":
            suggestions.append("Install build tools: sudo apt-get install -y build-essential gcc g++")
        elif error.error_type == "lock_file_conflict":
            suggestions.append("Delete the lock file (yarn.lock / package-lock.json) and reinstall")
        elif error.error_type == "metadata_generation":
            suggestions.append("Run: pip install --upgrade pip setuptools wheel")
        elif error.error_type == "node_gyp":
            suggestions.append("Install: sudo apt-get install -y python3 make g++  then npm install --ignore-scripts")
        elif error.error_type == "java_version":
            suggestions.append("Install JDK 17+: sudo apt-get install -y default-jdk")
        elif error.error_type == "gradle_error":
            suggestions.append("Add org.gradle.jvmargs=-Xmx2g to gradle.properties")
        elif error.error_type == "docker_error":
            suggestions.append("Ensure Docker Desktop / daemon is running: sudo systemctl start docker")
        elif error.error_type == "git_submodule":
            suggestions.append("Run: git submodule update --init --recursive")
        elif error.error_type == "rust_linker":
            suggestions.append("Install OpenSSL dev libs: sudo apt-get install -y pkg-config libssl-dev")
        elif error.error_type == "ruby_gem_error":
            gem = error.extracted.get("gem", "")
            suggestions.append(f"Install native deps for {gem or 'gems'}: sudo apt-get install -y build-essential ruby-dev")
        elif error.error_type == "permission_error":
            suggestions.append(
                "Fix file permissions or ownership on the repo (chmod/chown) or run from a writable directory"
            )
        elif error.error_type == "missing_config":
            suggestions.append(
                "Copy an example config (.env.example → .env) or add the missing config file the app expects"
            )
        elif error.error_type == "npm_lifecycle_failure":
            suggestions.append(
                "Install the tool referenced in the script (e.g. husky), or use npm install --ignore-scripts if hooks are optional"
            )
        elif error.error_type == "go_mod_bad_version":
            suggestions.append(
                "Use a valid go directive in go.mod (e.g. go 1.22 not 1.22.0) or install a matching Go toolchain"
            )
        elif error.error_type == "bind_mount_is_directory":
            cpath = error.extracted.get("container_path", "")
            suggestions.append(
                "Docker created a directory where a config file was expected on the host bind path — "
                f"repofix retries after fixing; if it persists, check docker-compose volumes for {cpath or 'that path'}."
            )
        elif error.error_type == "wrong_entry_point":
            bad = error.extracted.get("bad_path", "index.js")
            suggestions.append(
                f"Entry file '{bad}' not found — check the package.json 'main' field "
                "or use --command to specify the correct entry file"
            )
        elif error.error_type == "missing_tool":
            tool = error.extracted.get("tool_name", "")
            if tool:
                suggestions.append(f"Install missing CLI tool '{tool}' (e.g. pip install {tool}  or  sudo apt-get install -y {tool})")
            else:
                suggestions.append("A required CLI tool was not found — check the Makefile/scripts and install the missing tool")
    if not suggestions:
        suggestions.append("Check the full log output above for clues")
        suggestions.append("Try running the app manually in the repo directory")
    return suggestions
