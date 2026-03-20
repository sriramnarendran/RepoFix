"""Tests for npm global-install detection used in CLI hints."""

from __future__ import annotations

import pytest

from repofix.output.display import command_uses_npm_global_install


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("npm install -g openclaw@latest", True),
        ("npm i -g foo", True),
        ("npm install --global bar", True),
        ("pnpm add -g baz", True),
        ("echo npm", False),
        ("npm install foo", False),
        ("", False),
    ],
)
def test_command_uses_npm_global_install(cmd: str, expected: bool) -> None:
    assert command_uses_npm_global_install(cmd) is expected
