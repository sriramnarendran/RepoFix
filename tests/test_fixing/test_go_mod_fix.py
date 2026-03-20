"""Rule: patch invalid go directive in go.mod."""

from pathlib import Path

from repofix.fixing.classifier import classify_all
from repofix.fixing.detector import ErrorSignal
from repofix.fixing.rules import apply_rule
from repofix.detection.stack import StackInfo


def test_go_mod_bad_version_detect_and_fix(tmp_path: Path) -> None:
    mod_dir = tmp_path / "svc"
    mod_dir.mkdir()
    go_mod = mod_dir / "go.mod"
    go_mod.write_text("module example.com/x\ngo 1.22.0\n", encoding="utf-8")

    line = (
        f"{go_mod}:3: invalid go version '1.22.0': must match format 1.23"
    )
    sig = ErrorSignal(
        raw_line=line,
        source="stderr",
        error_type="go_mod_bad_version",
        context_lines=[line],
    )
    errors = classify_all([sig], runtime="go")
    assert errors[0].error_type == "go_mod_bad_version"
    assert errors[0].extracted.get("wanted_version") == "1.23"

    stack = StackInfo(language="go", runtime="go", framework="unknown")
    action = apply_rule(errors[0], stack, tmp_path)
    assert action is not None and action.run_fn is not None
    assert action.run_fn() is True
    assert "go 1.23" in go_mod.read_text(encoding="utf-8")
    assert "1.22.0" not in go_mod.read_text(encoding="utf-8")
