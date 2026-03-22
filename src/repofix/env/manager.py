"""Environment variable management — resolve, prompt, and inject env vars."""
from __future__ import annotations
from typing import Optional
import os
from pathlib import Path
from repofix.detection.environment import parse_env_example, scan_code_for_env_vars
from repofix.output import display

def resolve_env(repo_path: Path, extra_env_file: Optional[Path]=None, auto_approve: bool=False, mode: str='auto') -> dict[str, str]:
    """
    Build the environment dict to inject when running the app.

    Steps:
      1. Load .env.example defaults
      2. Load .env if present (overrides example)
      3. Load user-supplied --env-file (overrides everything)
      4. Scan code for referenced vars not yet defined
      5. Prompt for missing vars (in assist mode) or warn (in auto mode)
    """
    env: dict[str, str] = {}
    example_vars = parse_env_example(repo_path)
    env.update({k: v for (k, v) in example_vars.items() if v})
    repo_env_file = repo_path / '.env'
    if repo_env_file.exists():
        env.update(_load_dotenv(repo_env_file))
    if extra_env_file and extra_env_file.exists():
        env.update(_load_dotenv(extra_env_file))
    all_required = set(example_vars.keys())
    missing = {k for k in all_required if not env.get(k)}
    if missing:
        if mode in ('assist',) or not auto_approve:
            display.warning(f"Missing environment variables: {', '.join(sorted(missing))}")
            filled: list[str] = []
            for var in sorted(missing):
                default = example_vars.get(var, '')
                value = display.prompt_value(var, default)
                if value:
                    env[var] = value
                    filled.append(var)
            skipped = missing - set(filled)
            if skipped:
                if len(skipped) == len(missing):
                    display.warning(f'All {len(skipped)} required environment variables were left blank. The app may fail to start or show a blank/loading screen. Re-run with [bold]--env-file <path>[/bold] to supply them.')
                else:
                    display.warning(f"{len(skipped)} variable(s) left blank ({', '.join(sorted(skipped))}). The app may behave incorrectly without them.")
        else:
            for var in sorted(missing):
                display.warning(f'Env var [bold]{var}[/bold] not set — leaving empty')
    return env

def _load_dotenv(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for raw_line in path.read_text(errors='replace').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                (key, _, value) = line.partition('=')
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    result[key] = value
    except Exception:
        pass
    return result

def write_env_file(repo_path: Path, env: dict[str, str]) -> Path:
    """Write resolved env vars to a temporary .env file in the repo."""
    env_file = repo_path / '.env'
    lines = [f'{k}={v}' for (k, v) in env.items()]
    env_file.write_text('\n'.join(lines) + '\n')
    return env_file
