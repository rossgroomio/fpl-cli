"""Tests for .agents/skills/gw-prep/scripts/normalise_entities.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent / ".agents/skills/gw-prep/scripts/normalise_entities.py"
)


def _load_script() -> ModuleType:
    """Load normalise_entities.py as a module (it's not a package).

    Its only import beyond the stdlib is `fpl_cli.utils.markdown`, resolved
    through the installed fpl-cli package, so no sys.path manipulation is
    needed to load it standalone.
    """
    spec = importlib.util.spec_from_file_location("normalise_entities", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_script()
_run = _mod._run


def _normalise(path: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    _run(str(path))
    return json.loads(capsys.readouterr().out)


# -- Rewriting -----------------------------------------------------------------

# The section as it arrived in the run that prompted issue #185: a blockquote
# marker and a comparison operator escaped, everything non-ASCII intact.
ESCAPED_SECTION = """## Classic

&gt; **Data confidence caveat:** two of the three have no minutes yet.

Hinshelwood (Δoutlook +80) &gt; Lewis-Potter (+72) &gt; Tavernier (+69)

| Out | In | Cost |
| --- | --- | --- |
| Gyökeres | Haaland | £0.5m |
"""


def test_rewrites_the_file_in_place(tmp_path, capsys):
    report = tmp_path / "gw2-recommendations.md"
    report.write_text(ESCAPED_SECTION, encoding="utf-8")

    result = _normalise(report, capsys)

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["unescaped"] == 9  # three "&gt;" -> ">"
    assert result["residual"] == []

    written = report.read_text(encoding="utf-8")
    assert written.startswith("## Classic\n\n> **Data confidence caveat:**")
    assert "&gt;" not in written
    # Characters that survived the escaping must survive the repair.
    assert "Δoutlook" in written
    assert "Gyökeres | Haaland | £0.5m" in written


def test_leaves_a_clean_file_untouched(tmp_path, capsys):
    report = tmp_path / "gw2-recommendations.md"
    clean = "## Classic\n\n> **Caveat:** Brighton & Hove Albion at 90&\n"
    report.write_text(clean, encoding="utf-8")
    before = report.stat().st_mtime_ns

    result = _normalise(report, capsys)

    assert result == {"ok": True, "changed": False, "unescaped": 0, "residual": []}
    assert report.read_text(encoding="utf-8") == clean
    assert report.stat().st_mtime_ns == before, "clean file must not be rewritten"


def test_is_idempotent(tmp_path, capsys):
    report = tmp_path / "gw2-recommendations.md"
    report.write_text(ESCAPED_SECTION, encoding="utf-8")

    _normalise(report, capsys)
    once = report.read_text(encoding="utf-8")
    second = _normalise(report, capsys)

    assert second["changed"] is False
    assert report.read_text(encoding="utf-8") == once


# -- Residual reporting --------------------------------------------------------


def test_reports_entities_it_does_not_decode(tmp_path, capsys):
    """`ok: false` is the warn signal: the file was still rewritten as far as
    it could be, and the caller proceeds rather than blocking."""
    report = tmp_path / "gw2-recommendations.md"
    report.write_text("&gt; quote\nspacing&nbsp;here\n", encoding="utf-8")

    result = _normalise(report, capsys)

    assert result["ok"] is False
    assert result["changed"] is True
    assert result["residual"] == [{"line": 2, "entity": "&nbsp;"}]
    assert report.read_text(encoding="utf-8").startswith("> quote")


def test_reports_residuals_without_rewriting_when_nothing_is_decodable(
    tmp_path, capsys
):
    report = tmp_path / "gw2-recommendations.md"
    report.write_text("delta &#916; here\n", encoding="utf-8")

    result = _normalise(report, capsys)

    assert result["ok"] is False
    assert result["changed"] is False
    assert result["residual"] == [{"line": 1, "entity": "&#916;"}]


# -- Failure modes -------------------------------------------------------------


def test_missing_file_exits_1_with_a_json_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(str(tmp_path / "absent.md"))
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] is True
    assert "absent.md" in payload["messages"][0]


def test_runs_as_a_subprocess(tmp_path):
    """The orchestrator invokes it as a script, so the CLI surface is pinned
    too -- an argparse or import regression must not hide behind the
    in-process tests above."""
    report = tmp_path / "gw2-recommendations.md"
    report.write_text("&gt; quote\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--file", str(report)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["changed"] is True
    assert report.read_text(encoding="utf-8") == "> quote\n"
