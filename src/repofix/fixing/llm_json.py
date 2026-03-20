"""Extract JSON objects from noisy LLM replies (fences, prose, partial wraps)."""

from __future__ import annotations

import json
import re
from typing import Any


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def _strip_outer_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
    text = re.sub(r"\s*```\s*$", "", text).strip()
    return text.rstrip("`").strip()


def _balanced_brace_chunks(s: str) -> list[str]:
    """Return top-level `{...}` substrings (handles nesting)."""
    out: list[str] = []
    depth = 0
    start = -1
    for i, c in enumerate(s):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(s[start : i + 1])
                    start = -1
    return out


def _repair_json_loose(s: str) -> str:
    """Best-effort fixes for common LLM JSON mistakes."""
    s = s.strip()
    # Trailing commas before } or ]
    s = re.sub(r",(\s*[\]}])", r"\1", s)
    return s


def extract_json_object(text: str) -> dict[str, Any] | None:
    """
    Parse the first valid JSON object from model output.

    Tries: fenced blocks, whole text, then each balanced `{...}` region
    (longest first). Applies light repairs on decode failure.
    """
    if not text or not text.strip():
        return None

    raw = text
    candidates: list[str] = []

    for m in _FENCE_RE.finditer(raw):
        candidates.append(m.group(1).strip())

    stripped = _strip_outer_fences(raw)
    candidates.append(stripped)

    for chunk in _balanced_brace_chunks(stripped):
        candidates.append(chunk)

    # Longest-first tends to be the full object vs inner braces
    candidates.sort(key=len, reverse=True)
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        for variant in (cand, _repair_json_loose(cand)):
            try:
                val = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(val, dict):
                return val
    return None


_VALID_NEXT_STEPS = frozenset({"rerun", "rebuild", "reinstall"})


def normalize_fix_action_dict(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Coerce parsed LLM JSON into a shape suitable for FixAction."""
    if not data:
        return None
    desc = data.get("description")
    if desc == "unknown" or not desc:
        return None

    commands_raw = data.get("commands") or []
    if not isinstance(commands_raw, list):
        commands_raw = []
    commands: list[str] = []
    for c in commands_raw:
        if isinstance(c, str) and c.strip():
            commands.append(c.strip())

    env_raw = data.get("env_updates") or {}
    env_updates: dict[str, str] = {}
    if isinstance(env_raw, dict):
        for k, v in env_raw.items():
            if k is None:
                continue
            ks = str(k).strip()
            if ks and v is not None:
                env_updates[ks] = str(v)

    port = data.get("port_override")
    port_override: int | None = None
    if port is not None and port != "":
        try:
            port_override = int(port)
        except (TypeError, ValueError):
            port_override = None

    next_step = data.get("next_step")
    if next_step is not None:
        next_step = str(next_step).lower().strip()
        if next_step in ("none", "null", ""):
            next_step = None
        elif next_step not in _VALID_NEXT_STEPS:
            next_step = None

    return {
        "description": str(desc).strip(),
        "commands": commands,
        "env_updates": env_updates,
        "port_override": port_override,
        "next_step": next_step,
    }
