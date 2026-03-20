"""Second-step npm global prompt when prefix is not writable."""

from __future__ import annotations

from repofix.output.display import prompt_npm_global_prefix_unwritable


def test_prompt_npm_global_prefix_unwritable_auto_approve_defaults_isolated() -> None:
    assert prompt_npm_global_prefix_unwritable(auto_approve=True) == "isolated"
