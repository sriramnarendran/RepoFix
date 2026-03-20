"""User configuration management — stored at ~/.repofix/config.toml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import toml
from pydantic import BaseModel, Field

CONFIG_DIR        = Path.home() / ".repofix"
CONFIG_FILE       = CONFIG_DIR / "config.toml"
MEMORY_DB         = CONFIG_DIR / "memory.db"
RUNS_LOG          = CONFIG_DIR / "runs.log"
LOGS_DIR          = CONFIG_DIR / "logs"
PROCESS_REGISTRY  = CONFIG_DIR / "processes.json"
MODELS_DIR        = CONFIG_DIR / "models"


class RunnerConfig(BaseModel):
    gemini_api_key: str = Field(default="", description="Gemini API key for AI fallback")
    openai_api_key: str = Field(default="", description="OpenAI API key (or compatible endpoint)")
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    openai_base_url: str = Field(
        default="",
        description="OpenAI-compatible API base URL (empty = https://api.openai.com/v1)",
   )
    ai_cloud_provider: str = Field(
        default="auto",
        description="Primary cloud provider: auto | gemini | openai | anthropic",
    )
    ai_cloud_fallback: bool = Field(
        default=True,
        description="If the primary cloud provider fails, try other configured providers",
    )
    gemini_model: str = Field(default="gemini-2.0-flash-lite")
    openai_model: str = Field(default="gpt-4o-mini")
    anthropic_model: str = Field(default="claude-3-5-haiku-20241022")
    ai_cloud_setup_prompted: bool = Field(
        default=False,
        description="Whether the optional cloud API first-run prompt was shown",
    )
    default_mode: str = Field(default="auto", description="auto | assist | debug")
    default_retries: int = Field(default=5, description="Default max fix/retry cycles")
    auto_approve: bool = Field(default=False, description="Skip all confirmation prompts")
    clone_base_dir: str = Field(
        default=str(Path.home() / ".repofix" / "repos"),
        description="Base directory for cloned repos",
    )
    allowed_extra_commands: list[str] = Field(
        default_factory=list,
        description="Additional commands to add to the safety allowlist",
    )
    use_local_llm: bool = Field(
        default=True,
        description="Use local Qwen2.5-Coder-3B model before falling back to Gemini",
    )
    local_llm_prompted: bool = Field(
        default=False,
        description="Whether the one-time local-LLM setup prompt has been shown",
    )

    def has_gemini_key(self) -> bool:
        return bool(self.gemini_api_key.strip())

    def has_openai_key(self) -> bool:
        return bool(self.openai_api_key.strip())

    def has_anthropic_key(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    def any_cloud_ai_configured(self) -> bool:
        return self.has_gemini_key() or self.has_openai_key() or self.has_anthropic_key()


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    clone_dir = Path(load().clone_base_dir)
    clone_dir.mkdir(parents=True, exist_ok=True)


def load() -> RunnerConfig:
    ensure_dirs_only()
    if not CONFIG_FILE.exists():
        return RunnerConfig()
    try:
        data: dict[str, Any] = toml.loads(CONFIG_FILE.read_text())
        return RunnerConfig(**data)
    except Exception:
        return RunnerConfig()


def save(cfg: RunnerConfig) -> None:
    ensure_dirs_only()
    CONFIG_FILE.write_text(toml.dumps(cfg.model_dump()))


def ensure_dirs_only() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def set_gemini_key(key: str) -> None:
    cfg = load()
    cfg.gemini_api_key = key.strip()
    save(cfg)


def get_gemini_key() -> str:
    """Return key from config or GEMINI_API_KEY env var."""
    env_key = os.environ.get("GEMINI_API_KEY", "")
    if env_key:
        return env_key
    return load().gemini_api_key


def set_openai_key(key: str) -> None:
    c = load()
    c.openai_api_key = key.strip()
    save(c)


def get_openai_api_key() -> str:
    env_key = os.environ.get("OPENAI_API_KEY", "")
    if env_key:
        return env_key
    return load().openai_api_key


def get_openai_base_url() -> str:
    env_u = os.environ.get("OPENAI_BASE_URL", "")
    if env_u:
        return env_u.strip()
    return load().openai_base_url


def set_anthropic_key(key: str) -> None:
    c = load()
    c.anthropic_api_key = key.strip()
    save(c)


def get_anthropic_api_key() -> str:
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        return env_key
    return load().anthropic_api_key


def any_cloud_ai_configured() -> bool:
    return bool(
        get_gemini_key().strip()
        or get_openai_api_key().strip()
        or get_anthropic_api_key().strip()
    )
