"""Local LLM inference using Qwen2.5-Coder-3B-Instruct (GGUF, Q4_K_M quantized).

On first use this module:
  1. Installs huggingface_hub (pure Python, fast).
  2. Installs llama-cpp-python with --prefer-binary so pip picks a pre-built
     wheel from PyPI (Linux glibc x86_64/ARM64, macOS, Windows) and avoids
     C++ compilation on supported platforms.
  3. Downloads the GGUF model weights (~2 GB) from Hugging Face into
     ~/.repofix/models/ and reuses them on every subsequent run.

No API key required.  Works on Linux, macOS, and Windows.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from repofix import config as cfg
from repofix.detection.commands import CommandSet
from repofix.detection.stack import StackInfo
from repofix.fixing.classifier import ClassifiedError
from repofix.fixing.llm_json import extract_json_object, normalize_fix_action_dict
from repofix.fixing.rules import FixAction
from repofix.output import display

# ── Model constants ───────────────────────────────────────────────────────────

_HF_REPO_ID   = "Qwen/Qwen2.5-Coder-3B-Instruct-GGUF"
_HF_FILENAME  = "qwen2.5-coder-3b-instruct-q4_k_m.gguf"
_MODEL_SIZE_GB = 2.0

# Module-level cache — model is loaded once per process
_llm_instance = None


# ── Path helpers ──────────────────────────────────────────────────────────────

def models_dir() -> Path:
    d = cfg.CONFIG_DIR / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path() -> Path:
    return models_dir() / _HF_FILENAME


# ── Runtime dependency install ────────────────────────────────────────────────

def _pip_install(*packages: str) -> None:
    """Install packages quietly, preferring pre-built binary wheels."""
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--prefer-binary", "--quiet", *packages],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
        raise RuntimeError(f"pip install failed: {stderr.strip()}") from exc


def _install_dependencies() -> None:
    """
    Install huggingface_hub and llama-cpp-python if not already present.

    llama-cpp-python is installed with --prefer-binary from standard PyPI,
    which provides glibc wheels for Linux x86_64/ARM64, macOS (Intel + Apple
    Silicon), and Windows.  Source compilation only happens on unsupported
    platforms as a last resort.
    """
    try:
        import huggingface_hub  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        display.step("Installing [bold]huggingface_hub[/bold]…")
        _pip_install("huggingface_hub>=0.21")
        display.success("huggingface_hub installed.")

    if not is_available():
        display.step(
            "Installing [bold]llama-cpp-python[/bold] "
            "(pre-built wheel — no compilation on supported platforms)…"
        )
        _pip_install("llama-cpp-python>=0.2.90")
        display.success("llama-cpp-python installed.")


# ── Availability checks ───────────────────────────────────────────────────────

def is_available() -> bool:
    """Return True if llama-cpp-python is importable."""
    try:
        import llama_cpp  # type: ignore[import-untyped]  # noqa: F401
        return True
    except ImportError:
        return False


def is_downloaded() -> bool:
    """Return True if the GGUF model file is already on disk."""
    return model_path().exists()


# ── Model download ────────────────────────────────────────────────────────────

def ensure_model() -> Path:
    """Download the model weights if not already present; return the file path."""
    dest = model_path()
    if dest.exists():
        return dest

    display.ai_action(
        f"Downloading [bold]Qwen2.5-Coder-3B-Instruct Q4_K_M[/bold] "
        f"(~{_MODEL_SIZE_GB:.0f} GB) to {models_dir()} …"
    )
    display.muted("One-time download — reused on all future runs.")

    try:
        from huggingface_hub import hf_hub_download  # type: ignore[import-untyped]

        cached = Path(hf_hub_download(repo_id=_HF_REPO_ID, filename=_HF_FILENAME))
        # Copy out of the HF cache so the model survives cache purges.
        shutil.copy2(cached, dest)
        display.success(f"Model ready: {dest}")
        return dest

    except Exception as exc:
        display.warning(f"Model download failed: {exc}")
        raise


# ── Full setup ────────────────────────────────────────────────────────────────

def ensure_ready() -> Path:
    """
    One-call setup:
      1. Install llama-cpp-python + huggingface_hub (if needed).
      2. Download the GGUF model weights (if needed).
    Returns the path to the model file.
    """
    _install_dependencies()
    return ensure_model()


# ── Llama instance ────────────────────────────────────────────────────────────

def _get_llm():
    """Lazy-load and cache the Llama model instance."""
    global _llm_instance
    if _llm_instance is not None:
        return _llm_instance

    model_file = ensure_model()
    display.ai_action("Loading Qwen2.5-Coder-3B-Instruct …")

    from llama_cpp import Llama  # type: ignore[import-untyped]

    _llm_instance = Llama(
        model_path=str(model_file),
        n_ctx=4096,
        n_threads=None,   # auto-detect CPU count
        n_gpu_layers=-1,  # use GPU if available, else CPU-only
        verbose=False,
    )
    return _llm_instance


def _generate(prompt: str, max_tokens: int = 512) -> str:
    """Run inference and return the model's reply."""
    llm = _get_llm()
    response = llm.create_chat_completion(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert DevOps engineer. "
                    "Always respond with a single valid JSON object only — "
                    "no markdown fences, no explanation."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
        temperature=0.1,
    )
    return response["choices"][0]["message"]["content"].strip()


# ── Public API (mirrors ai_fixer.py) ─────────────────────────────────────────

def fix_error(
    error: ClassifiedError,
    stack: StackInfo,
    repo_path: Path,
    recent_logs: str = "",
) -> FixAction | None:
    """Ask the local LLM to suggest a fix for an unknown startup error."""
    error_context = "\n".join(error.signal.context_lines) if error.signal.context_lines else ""
    context_section = ""
    if error_context:
        context_section += f"\n## Error Context (lines around the failure)\n```\n{error_context}\n```"
    if recent_logs:
        context_section += f"\n## Recent Logs (last output before failure)\n```\n{recent_logs[-2000:]}\n```"
    if not context_section:
        context_section = "\n## Context\nN/A"

    prompt = textwrap.dedent(f"""
        ## Project Info
        - Language: {stack.language}
        - Framework: {stack.framework}
        - Runtime: {stack.runtime}
        - Project path: {repo_path}

        ## Error
        Type: {error.error_type}
        Message: {error.description}
        {context_section}

        ## Task
        Suggest ONE specific fix to resolve this error. Respond with a JSON object ONLY:

        {{
          "description": "Brief description of the fix",
          "commands": ["command1", "command2"],
          "env_updates": {{"VAR_NAME": "value"}},
          "port_override": null,
          "next_step": "rerun"
        }}

        Rules:
        - "next_step" must be one of: "rerun", "rebuild", "reinstall", or null
        - "port_override" should be an integer port number or null
        - "env_updates" should only include vars that are definitely needed
        - Commands must be safe (no sudo, no rm -rf, no curl | sh)
        - If you cannot suggest a safe fix, respond with: {{"description": "unknown", "commands": [], "env_updates": {{}}, "port_override": null, "next_step": null}}
    """).strip()

    display.ai_action(f"Asking local LLM to analyse: {error.description[:80]}…")
    try:
        raw = _generate(prompt)
        data = extract_json_object(raw)
        norm = normalize_fix_action_dict(data)
        if not norm:
            return None
        return FixAction(
            description=norm["description"],
            commands=norm["commands"],
            env_updates=norm["env_updates"],
            port_override=norm["port_override"],
            next_step=norm["next_step"],
            source="ai",
        )
    except Exception as exc:
        display.warning(f"Local LLM fix failed: {exc}")
        return None


def detect_stack_from_readme(readme: str) -> StackInfo:
    """Ask the local LLM to infer the stack from a README."""
    prompt = textwrap.dedent(f"""
        Analyse the following README and determine the project stack.

        README:
        ```
        {readme[:6000]}
        ```

        Respond with a JSON object ONLY:
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

    try:
        raw = _generate(prompt)
        data = extract_json_object(raw)
        if data:
            return StackInfo(
                language=data.get("language", "unknown"),
                framework=data.get("framework", "unknown"),
                project_type=data.get("project_type", "unknown"),
                runtime=data.get("runtime", "unknown"),
                detection_source="readme_ai",
            )
    except Exception as exc:
        display.warning(f"Local LLM stack detection failed: {exc}")

    return StackInfo()


def extract_commands_from_readme(readme: str) -> CommandSet:
    """Ask the local LLM to extract install/build/run commands from a README."""
    prompt = textwrap.dedent(f"""
        Read this README and extract the commands to set up and run the project.

        README:
        ```
        {readme[:6000]}
        ```

        Respond with a JSON object ONLY:
        {{
          "install": "npm install",
          "build": null,
          "run": "npm run dev"
        }}

        - Use null if a step is not mentioned or not needed.
        - Only include commands that are explicitly shown in the README.
        - Do not invent commands.
    """).strip()

    try:
        raw = _generate(prompt)
        data = extract_json_object(raw)
        if data:
            return CommandSet(
                install=data.get("install"),
                build=data.get("build"),
                run=data.get("run"),
                source="readme_ai",
            )
    except Exception as exc:
        display.warning(f"Local LLM command extraction failed: {exc}")

    return CommandSet(source="readme_ai")
