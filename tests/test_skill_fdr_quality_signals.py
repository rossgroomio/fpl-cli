"""gw-prep must read the rating-quality signals `fpl fdr` hands it.

`fpl fdr` reports the ways fixture difficulty can be wrong -- ratings from
last season, no ratings at all, ratings still naming relegated clubs, ratings
that separate no two teams, pre-season estimates -- as `data.ratings_warning`
on the agent payload. None of them shows up in the numbers: the pFDR table
renders normally either way, so a consumer that drops the field accepts
whatever rating quality it happened to get and never says so (issue #135).

The trap is that `--blanks` is a different code path. It bypasses the agent
for a schedule-only payload with no `fdr_by_team`, no ATK/DEF split and no
`ratings_warning`, so a skill issuing only that command reads the field as
absent rather than clean -- indistinguishable from healthy ratings. This
module pins both halves: that gw-prep still issues the agent call, and that
the agent call still emits the fields gw-prep is told to read.
"""

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from fpl_cli.cli.fdr import fdr_command

REPO_ROOT = Path(__file__).parent.parent
GW_PREP = REPO_ROOT / ".agents/skills/gw-prep"
SKILL = GW_PREP / "SKILL.md"
OUTPUT_TEMPLATE = GW_PREP / "references/output-template.md"

# The three fields the skill is told to read off the agent payload, plus the
# blanks payload's own warning channel.
QUALITY_FIELDS = (
    "data.ratings_warning",
    "data.predictions_stale",
    "data.prediction_warnings",
)

# `fpl fdr ... --format json` without `--blanks` anywhere on the line. The
# blanks path returns a different payload and is not a substitute.
AGENT_FDR_CALL = re.compile(r"^fpl fdr(?!.*--blanks).*--format json\s*$", re.MULTILINE)
BLANKS_FDR_CALL = re.compile(r"^fpl fdr .*--blanks.*--format json\s*$", re.MULTILINE)


@pytest.fixture
def skill_text():
    return SKILL.read_text(encoding="utf-8")


def test_the_call_patterns_tell_the_two_payloads_apart():
    # Guard the guard: a pattern that matched `--blanks` would wave through
    # exactly the bug this module exists to catch.
    assert AGENT_FDR_CALL.search("fpl fdr --my-squad --format json")
    assert AGENT_FDR_CALL.search("fpl fdr --format json")
    assert not AGENT_FDR_CALL.search("fpl fdr --blanks --format json")
    assert BLANKS_FDR_CALL.search("fpl fdr --blanks --format json")


def test_phase_b1_issues_the_agent_call(skill_text):
    """The schedule-only call cannot carry `ratings_warning`, so it is not enough."""
    assert AGENT_FDR_CALL.search(skill_text), (
        "gw-prep B1 must run `fpl fdr ... --format json` without `--blanks`;"
        " the blanks path returns no fixture-difficulty analysis and no"
        " ratings_warning"
    )


def test_phase_b1_still_fetches_the_blank_double_schedule(skill_text):
    """The agent payload has no predicted BGW/DGW -- both calls are load-bearing."""
    assert BLANKS_FDR_CALL.search(skill_text)


@pytest.mark.parametrize("field", QUALITY_FIELDS)
def test_skill_names_each_quality_field(field, skill_text):
    assert field in skill_text, f"gw-prep never tells the orchestrator to read {field}"


def test_skill_names_the_ratings_remedy(skill_text):
    """The warning text is quoted verbatim because it carries the fix."""
    assert "fpl ratings update" in skill_text


def test_caveat_reaches_every_phase_c_prompt(skill_text):
    """One unthreaded prompt is a sub-agent reasoning on ratings it can't see."""
    pfdr_lines = skill_text.count("> - pFDR: ")
    caveat_lines = skill_text.count("{data_caveat}")
    assert pfdr_lines > 0
    assert caveat_lines == pfdr_lines


def test_output_template_carries_the_data_quality_section():
    """Terminal scrollback is not a record; the report is what gets re-read."""
    assert "Data quality" in OUTPUT_TEMPLATE.read_text(encoding="utf-8")


def _agent_json(agent_data: dict, args: list[str]) -> dict:
    """Invoke the agent path of `fpl fdr --format json` over a stubbed agent."""
    agent = MagicMock()
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock(return_value=False)
    agent.run = AsyncMock(return_value=MagicMock(success=True, data=agent_data))

    with (
        patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=True),
        patch("fpl_cli.cli.fdr.load_settings", return_value={"fpl": {}}),
        patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=agent),
    ):
        result = CliRunner().invoke(fdr_command, args)

    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_agent_envelope_carries_the_fields_the_skill_reads():
    """The other half of the contract: renaming these silently blinds gw-prep."""
    parsed = _agent_json(
        {
            "current_gameweek": 25,
            "easy_fixture_runs": {"overall": []},
            "ratings_warning": "⚠️ Team ratings are from 2025-26, not 2026-27",
            "predictions_stale": True,
            "prediction_warnings": ["fixture_predictions.yaml is malformed - using shipped copy"],
        },
        ["--format", "json"],
    )

    for field in QUALITY_FIELDS:
        assert field.removeprefix("data.") in parsed["data"], f"{field} missing from envelope"
    assert parsed["data"]["ratings_warning"].startswith("⚠️")


def test_agent_envelope_reports_clean_ratings_as_null_not_absent():
    """`None` is the clean signal. An absent key would read the same as the
    blanks payload, which never had the field to begin with."""
    parsed = _agent_json(
        {
            "current_gameweek": 25,
            "easy_fixture_runs": {"overall": []},
            "ratings_warning": None,
        },
        ["--format", "json"],
    )

    assert parsed["data"]["ratings_warning"] is None
