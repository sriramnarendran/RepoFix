"""Install shell tab completion without touching login shells where possible.

Fish loads completions from ~/.config/fish/completions/ automatically.
Bash (with bash-completion) loads ~/.local/share/bash-completion/completions/
without editing ~/.bashrc on most Linux distributions.

Zsh and PowerShell still need `repofix completion install` (they require fpath /
profile changes). Opt out entirely with REPOFIX_NO_AUTO_COMPLETION=1.
"""

from __future__ import annotations

import os
from pathlib import Path

_PROG = "repofix"
_COMPLETE_VAR = "_REPOFIX_COMPLETE"


def maybe_install_shell_completion() -> None:
    if os.environ.get("REPOFIX_NO_AUTO_COMPLETION", "").strip():
        return
    if os.environ.get("CI", "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        import shellingham
        from typer._completion_shared import get_completion_script
    except Exception:
        return
    try:
        detected = shellingham.detect_shell()
    except Exception:
        return
    if not detected:
        return
    shell_name = detected[0].lower()
    home = Path.home()
    try:
        if shell_name == "fish":
            _write_if_changed(
                home / ".config/fish/completions/repofix.fish",
                get_completion_script(prog_name=_PROG, complete_var=_COMPLETE_VAR, shell="fish"),
            )
        elif shell_name == "bash":
            _write_if_changed(
                home / ".local/share/bash-completion/completions/repofix",
                get_completion_script(prog_name=_PROG, complete_var=_COMPLETE_VAR, shell="bash"),
            )
    except OSError:
        return


def _write_if_changed(path: Path, content: str) -> None:
    text = content.rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text:
        return
    path.write_text(text, encoding="utf-8")
