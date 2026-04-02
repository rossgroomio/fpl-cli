"""Tests for ReportAgent."""

from pathlib import Path

import pytest

from fpl_cli.agents.base import AgentStatus
from fpl_cli.agents.orchestration.report import ReportAgent


# ---------------------------------------------------------------------------
# Minimal data helpers
# ---------------------------------------------------------------------------

def _preview_data() -> dict:
    return {
        "gw_fixtures": [
            {
                "home_team": "LIV",
                "home_fdr": 2,
                "away_fdr": 4,
                "away_team": "ARS",
                "kickoff": "Sat 15:00",
            }
        ],
        "deadline": "Fri 18:30",
        "my_squad": [
            {
                "name": "Salah",
                "team": "LIV",
                "fixture": "ARS",
                "position": "MID",
                "form": 7.5,
                "ownership": 45.2,
                "status": "Available",
            }
        ],
        "draft_squad": [
            {
                "name": "Saka",
                "team": "ARS",
                "fixture": "liv",
                "position": "MID",
                "form": 6.2,
                "status": "✓",
            }
        ],
        "prices": {
            "risers_this_gw": [
                {
                    "name": "Palmer",
                    "team": "CHE",
                    "current_price": 5.6,
                    "change_this_gw": 0.1,
                }
            ]
        },
    }


def _review_data() -> dict:
    return {
        "points": {
            "total": 72,
            "rank": 50000,
            "overall_rank": 120000,
            "average": 55,
            "highest": 143,
        },
        "team_points": [
            {
                "name": "Salah",
                "team": "LIV",
                "position": "MID",
                "display_points": 14,
                "is_captain": True,
                "is_triple_captain": False,
                "is_vice_active": False,
                "contributed": True,
                "auto_sub_in": False,
                "auto_sub_out": False,
                "red_cards": 0,
            },
            {
                "name": "Saka",
                "team": "ARS",
                "position": "MID",
                "display_points": 6,
                "is_captain": False,
                "is_triple_captain": False,
                "is_vice_active": False,
                "contributed": True,
                "auto_sub_in": False,
                "auto_sub_out": False,
                "red_cards": 0,
            },
        ],
        "classic_transfers": [
            {
                "player_in": "Salah",
                "player_in_team": "LIV",
                "player_in_points": 14,
                "player_out": "Saka",
                "player_out_team": "ARS",
                "player_out_points": 6,
                "net": 8,
                "verdict": "Hit",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Group 1: run() contract
# ---------------------------------------------------------------------------

class TestRunContract:
    async def test_no_context_returns_failed(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run(None)
        assert result.status == AgentStatus.FAILED
        assert "No context" in result.message

    async def test_missing_gameweek_returns_failed(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run({"report_type": "preview", "data": {}})
        assert result.status == AgentStatus.FAILED
        assert "gameweek" in result.message.lower()

    async def test_unknown_report_type_returns_failed(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run({"report_type": "summary", "gameweek": 29, "data": {}})
        assert result.status == AgentStatus.FAILED
        assert "Unknown report type" in result.message

    async def test_valid_preview_writes_file(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run({
            "report_type": "preview",
            "gameweek": 29,
            "data": _preview_data(),
        })
        assert result.status == AgentStatus.SUCCESS
        report_path = Path(result.data["report_path"])
        assert report_path.exists()
        assert report_path.name == "gw29-preview.md"

    async def test_valid_review_writes_file(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run({
            "report_type": "review",
            "gameweek": 29,
            "data": _review_data(),
        })
        assert result.status == AgentStatus.SUCCESS
        report_path = Path(result.data["report_path"])
        assert report_path.exists()
        assert report_path.name == "gw29-review.md"


# ---------------------------------------------------------------------------
# Group 2: Template rendering (Jinja2 path)
# ---------------------------------------------------------------------------

class TestTemplateRendering:
    def setup_method(self):
        self.agent = ReportAgent()

    # Preview

    def test_preview_contains_deadline(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "Fri 18:30" in output

    def test_preview_player_name_in_my_squad(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "Salah" in output

    def test_preview_fixture_team_in_table(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "LIV" in output

    def test_preview_price_riser_name_and_price(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "Palmer" in output
        assert "5.6" in output

    # Review

    def test_review_total_points_in_summary(self):
        output = self.agent._generate_review_report(29, _review_data())
        assert "72" in output

    def test_review_captain_marker(self):
        output = self.agent._generate_review_report(29, _review_data())
        assert "Salah (C)" in output

    def test_review_no_captain_marker_for_non_captain(self):
        output = self.agent._generate_review_report(29, _review_data())
        assert "Saka (C)" not in output

    def test_review_triple_captain_marker(self):
        data = _review_data()
        data["team_points"][0]["is_triple_captain"] = True
        output = self.agent._generate_review_report(29, data)
        assert "Salah (TC)" in output

    def test_review_auto_sub_in_marker(self):
        data = _review_data()
        data["team_points"][1]["auto_sub_in"] = True
        data["team_points"][1]["contributed"] = False
        output = self.agent._generate_review_report(29, data)
        assert "[SUB IN]" in output

    def test_review_auto_sub_out_marker(self):
        data = _review_data()
        data["team_points"][1]["auto_sub_out"] = True
        data["team_points"][1]["contributed"] = False
        output = self.agent._generate_review_report(29, data)
        assert "[DIDN'T PLAY]" in output

    def test_review_unused_bench_marker(self):
        data = _review_data()
        data["team_points"][1]["contributed"] = False
        data["team_points"][1]["display_points"] = 8  # >= 6, triggers UNUSED!
        output = self.agent._generate_review_report(29, data)
        assert "[UNUSED!]" in output

    def test_review_red_card_emoji(self):
        data = _review_data()
        data["team_points"][1]["red_cards"] = 1
        output = self.agent._generate_review_report(29, data)
        assert "🟥" in output

    def test_review_no_red_cards_omits_column(self):
        data = _review_data()
        # Default data has red_cards=0 for all players
        output = self.agent._generate_review_report(29, data)
        # Table header should not have the red card column
        for line in output.splitlines():
            if line.startswith("| Player"):
                assert "🟥" not in line
                break

    def test_review_transfer_row_rendered(self):
        output = self.agent._generate_review_report(29, _review_data())
        assert "Salah" in output
        assert "Saka" in output
        assert "Hit" in output


# ---------------------------------------------------------------------------
# Group 3: Inline fallback path
# ---------------------------------------------------------------------------

class TestInlineFallback:
    def setup_method(self):
        self.agent = ReportAgent()

    # Preview inline

    def test_preview_inline_player_name(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "Salah" in output

    def test_preview_inline_price_riser(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "Palmer" in output

    def test_preview_inline_classic_section(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "# Classic" in output

    def test_preview_inline_no_further_reading(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "# Further Reading" not in output

    # Review inline

    def test_review_inline_player_name(self):
        output = self.agent._generate_review_inline(29, _review_data())
        assert "Salah" in output

    def test_review_inline_points_value(self):
        output = self.agent._generate_review_inline(29, _review_data())
        assert "72" in output

    def test_review_inline_classic_section(self):
        output = self.agent._generate_review_inline(29, _review_data())
        assert "# Classic" in output

    def test_review_inline_draft_section(self):
        output = self.agent._generate_review_inline(29, _review_data())
        assert "# Draft" in output

    def test_review_inline_captain_marker(self):
        output = self.agent._generate_review_inline(29, _review_data())
        assert "Salah (C)" in output


# ---------------------------------------------------------------------------
# Group 4: Fixture column
# ---------------------------------------------------------------------------

class TestFixtureColumn:
    def setup_method(self):
        self.agent = ReportAgent()

    def test_fixture_in_template_preview(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "ARS" in output

    def test_fixture_in_template_draft(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "liv" in output

    def test_fixture_in_inline_preview(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "ARS" in output

    def test_fixture_in_inline_draft(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "liv" in output

    def test_fixture_header_in_template(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "| Fixture |" in output

    def test_fixture_header_in_inline(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "| Fixture |" in output
