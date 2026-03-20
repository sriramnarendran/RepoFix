"""AI fallback for unknown errors and README-based detection.

Tries in order:
  1. Local LLM — when `use_local_llm` is true and llama-cpp-python + weights exist.
  2. Cloud APIs — Gemini, OpenAI, or Anthropic per `ai_cloud_provider` / `ai_cloud_fallback`.

Configure keys with `repofix config set-key` (Gemini) or `set-api-key`, and models via
`repofix config set-default --gemini-model …` (etc.).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

from repofix import config as cfg
from repofix.detection.commands import CommandSet
from repofix.detection.stack import StackInfo
from repofix.fixing.classifier import ClassifiedError
from repofix.fixing.llm_cloud import generate_cloud
from repofix.fixing.llm_json import extract_json_object, normalize_fix_action_dict
from repofix.fixing.rules import FixAction
from repofix.output import display

# ── Local LLM helper ──────────────────────────────────────────────────────────


def _local_llm_available() -> bool:
    """Return True if the local-LLM feature is enabled and packages are present."""
    if not cfg.load().use_local_llm:
        return False
    try:
        from repofix.fixing import local_llm  # noqa: F401

        return local_llm.is_available()
    except Exception:
        return False


def ai_fix_available() -> bool:
    """True if any AI backend can run (local model or any configured cloud API)."""
    if _local_llm_available():
        return True
    return cfg.any_cloud_ai_configured()


def _fix_error_prompt(
    error: ClassifiedError,
    stack: StackInfo,
    repo_path: Path,
    recent_logs: str,
) -> str:
    error_context = "\n".join(error.signal.context_lines) if error.signal.context_lines else ""
    context_section = ""
    if error_context:
        context_section += (
            f"\n## Error Context (lines around the failure)\n```\n{error_context}\n```"
        )
    if recent_logs:
        context_section += f"\n## Recent Logs (tail)\n```\n{recent_logs[-4000:]}\n```"
    if not context_section:
        context_section = "\n## Context\nN/A"

    extracted = error.extracted or {}
    extracted_blob = ""
    if extracted:
        try:
            extracted_blob = json.dumps(extracted, indent=2, default=str)
        except Exception:
            extracted_blob = str(extracted)
        extracted_blob = f"\n## Structured hints (from classifier)\n```json\n{extracted_blob}\n```\n"

    return textwrap.dedent(f"""
        You are an expert DevOps engineer helping to automatically fix a project startup error.

        Think about common root causes even when the error looks unfamiliar: missing packages or
        system libraries, wrong language/runtime version, bad PATH, missing env vars or secrets,
        port conflicts, bind/host issues, file permissions, architecture (e.g. ARM vs x86),
        corrupt caches (node_modules, pip, cargo), wrong working directory, Dockerfile or
        compose misconfiguration, proxy/SSL, and stale generated files.

        ## Project Info
        - Language: {stack.language}
        - Framework: {stack.framework}
        - Runtime: {stack.runtime}
        - Project path: {repo_path}

        ## Error
        - Classified type: {error.error_type}
        - Summary: {error.description}
        - Raw signal line: {error.signal.raw_line[:500]}
        {extracted_blob}{context_section}

        ## Task
        Propose ONE minimal, safe fix. Respond with a single JSON object only (no markdown):

        {{
          "description": "Brief description of the fix",
          "commands": ["command1", "command2"],
          "env_updates": {{"VAR_NAME": "value"}},
          "port_override": null,
          "next_step": "rerun"
        }}

        Rules:
        - "next_step" must be one of: "rerun", "rebuild", "reinstall", or null
        - "port_override": integer TCP port or null
        - "env_updates": only vars clearly required by the error
        - Commands: safe, non-destructive — no sudo, no rm -rf /, no curl|sh, no recursive deletes
        - Prefer standard package managers and documented project commands
        - If there is no safe, specific fix, respond exactly with:
          {{"description": "unknown", "commands": [], "env_updates": {{}}, "port_override": null, "next_step": null}}
    """).strip()


def _readme_stack_prompt(readme: str) -> str:
    return textwrap.dedent(f"""
        You are a developer tool. Analyse the following README and determine the project stack.

        README:
        ```
        {readme[:6000]}
        ```

        Respond with one JSON object only (no markdown):
        {{
          "language": "Python",
          "framework": "FastAPI",
          "project_type": "backend",
          "runtime": "python"
        }}

        - "project_type" must be one of: frontend, backend, fullstack, service, unknown
        - "runtime" must be one of: node, python, go, cargo, java, php, ruby, flutter, docker, unknown
        - Use "unknown" if you cannot determine.
    """).strip()


def _readme_commands_prompt(readme: str) -> str:
    return textwrap.dedent(f"""
        You are a developer tool. Read this README and extract the commands to set up and run.

        README:
        ```
        {readme[:6000]}
        ```

        Respond with one JSON object only (no markdown):
        {{
          "install": "npm install",
          "build": null,
          "run": "npm run dev"
        }}

        - Use null if a step is not mentioned or not needed.
        - Only include commands explicitly shown in the README.
        - Do not invent commands.
    """).strip()


def _parse_stack_json(data: dict[str, Any] | None) -> StackInfo | None:
    if not data:
        return None
    return StackInfo(
        language=data.get("language", "unknown"),
        framework=data.get("framework", "unknown"),
        project_type=data.get("project_type", "unknown"),
        runtime=data.get("runtime", "unknown"),
        detection_source="readme_ai",
    )


def _parse_commands_json(data: dict[str, Any] | None) -> CommandSet | None:
    if not data:
        return None
    return CommandSet(
        install=data.get("install"),
        build=data.get("build"),
        run=data.get("run"),
        source="readme_ai",
    )


# ── Fix unknown errors ────────────────────────────────────────────────────────


def fix_error(
    error: ClassifiedError,
    stack: StackInfo,
    repo_path: Path,
    recent_logs: str = "",
) -> FixAction | None:
    """
    Suggest a fix for an error that rule-based strategies couldn't handle.

    Tries local LLM first when enabled; then cloud providers per configuration.
    """
    if _local_llm_available():
        try:
            from repofix.fixing import local_llm

            result = local_llm.fix_error(error, stack, repo_path, recent_logs)
            if result is not None:
                return result
        except Exception as exc:
            display.warning(f"Local LLM failed, trying cloud APIs: {exc}")

    if not cfg.any_cloud_ai_configured():
        return None

    prompt = _fix_error_prompt(error, stack, repo_path, recent_logs)
    display.ai_action(f"Asking cloud AI to analyse: {error.description[:80]}…")
    try:
        raw = generate_cloud(prompt, task_label="fix", max_tokens=2048)
        data = extract_json_object(raw)
        normalized = normalize_fix_action_dict(data)
        if not normalized:
            return None
        return FixAction(
            description=normalized["description"],
            commands=normalized["commands"],
            env_updates=normalized["env_updates"],
            port_override=normalized["port_override"],
            next_step=normalized["next_step"],
            source="ai",
        )
    except Exception as exc:
        display.warning(f"Cloud AI fix failed: {exc}")
        return None


# ── Stack detection from README ───────────────────────────────────────────────


def detect_stack_from_readme(readme: str) -> StackInfo:
    """Infer the project stack from a README file."""
    if _local_llm_available():
        try:
            from repofix.fixing import local_llm

            result = local_llm.detect_stack_from_readme(readme)
            if result.language != "unknown":
                return result
        except Exception as exc:
            display.warning(f"Local LLM stack detection failed, trying cloud: {exc}")

    if not cfg.any_cloud_ai_configured():
        return StackInfo()

    try:
        raw = generate_cloud(_readme_stack_prompt(readme), task_label="readme stack", max_tokens=512)
        data = extract_json_object(raw)
        parsed = _parse_stack_json(data)
        if parsed:
            return parsed
    except Exception as exc:
        display.warning(f"Cloud AI stack detection failed: {exc}")

    return StackInfo()


# ── Command extraction from README ─────────────────────────────────────────────


def extract_commands_from_readme(readme: str) -> CommandSet:
    """Extract install/build/run commands from a README file."""
    if _local_llm_available():
        try:
            from repofix.fixing import local_llm

            result = local_llm.extract_commands_from_readme(readme)
            if result.run or result.install:
                return result
        except Exception as exc:
            display.warning(f"Local LLM command extraction failed, trying cloud: {exc}")

    if not cfg.any_cloud_ai_configured():
        return CommandSet(source="readme_ai")

    try:
        raw = generate_cloud(_readme_commands_prompt(readme), task_label="readme commands", max_tokens=512)
        data = extract_json_object(raw)
        parsed = _parse_commands_json(data)
        if parsed:
            return parsed
    except Exception as exc:
        display.warning(f"Cloud AI command extraction failed: {exc}")

    return CommandSet(source="readme_ai")
