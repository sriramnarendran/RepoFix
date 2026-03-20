"""Tests for LLM JSON extraction and fix payload normalization."""

from repofix.fixing.llm_json import extract_json_object, normalize_fix_action_dict


def test_extract_plain():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_fenced():
    raw = 'Here:\n```json\n{"x": "y"}\n```\n'
    assert extract_json_object(raw) == {"x": "y"}


def test_extract_nested_braces():
    raw = 'prefix {"commands": ["a"], "meta": {"n": 1}} suffix'
    d = extract_json_object(raw)
    assert d == {"commands": ["a"], "meta": {"n": 1}}


def test_normalize_fix():
    d = normalize_fix_action_dict(
        {
            "description": "  fix ports  ",
            "commands": ["  npm ci  ", 99],
            "env_updates": {"A": 1},
            "port_override": "8080",
            "next_step": "RERUN",
        }
    )
    assert d is not None
    assert d["description"] == "fix ports"
    assert d["commands"] == ["npm ci"]
    assert d["env_updates"] == {"A": "1"}
    assert d["port_override"] == 8080
    assert d["next_step"] == "rerun"


def test_normalize_unknown():
    assert normalize_fix_action_dict({"description": "unknown"}) is None
