"""npm global scope prompt defaults."""

from __future__ import annotations

from repofix.output.display import prompt_npm_global_scope


def test_prompt_npm_global_scope_auto_approve_defaults_isolated() -> None:
    assert prompt_npm_global_scope(auto_approve=True) == "isolated"
