"""Tests for orchestration agents (ReportAgent)."""

from pathlib import Path

import pytest

from fpl_cli.agents.base import AgentStatus
from fpl_cli.agents.orchestration.report import ReportAgent

# ==============================================================================
# REPORT AGENT TESTS
# ==============================================================================

class TestReportAgentInit:
    """Tests for ReportAgent initialization."""

    def test_agent_initialization(self):
        """Test default initialization."""
        agent = ReportAgent()
        assert agent.name == "ReportAgent"
        assert agent.output_dir == Path(".")

    def test_agent_initialization_with_output_dir(self, tmp_path):
        """Test initialization with custom output dir."""
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        assert agent.output_dir == tmp_path


class TestReportAgentRun:
    """Tests for ReportAgent run method."""

    @pytest.mark.asyncio
    async def test_run_missing_context(self):
        """Test run fails without context."""
        agent = ReportAgent()
        result = await agent.run()

        assert result.status == AgentStatus.FAILED
        assert "No context provided" in result.message

    @pytest.mark.asyncio
    async def test_run_missing_gameweek(self):
        """Test run fails without gameweek."""
        agent = ReportAgent()
        result = await agent.run(context={"report_type": "preview"})

        assert result.status == AgentStatus.FAILED
        assert "No gameweek specified" in result.message

    @pytest.mark.asyncio
    async def test_run_unknown_report_type(self):
        """Test run fails with unknown report type."""
        agent = ReportAgent()
        result = await agent.run(context={"report_type": "unknown", "gameweek": 25})

        assert result.status == AgentStatus.FAILED
        assert "Unknown report type" in result.message

    @pytest.mark.asyncio
    async def test_run_preview_success(self, tmp_path):
        """Test successful preview report generation."""
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        context = {
            "report_type": "preview",
            "gameweek": 25,
            "data": {"deadline": "2024-02-10 11:00"},
        }

        result = await agent.run(context=context)

        assert result.status == AgentStatus.SUCCESS
        assert "report_path" in result.data
        assert "gw25-preview.md" in result.data["report_path"]
        assert (tmp_path / "gw25-preview.md").exists()

    @pytest.mark.asyncio
    async def test_run_review_success(self, tmp_path):
        """Test successful review report generation."""
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        context = {
            "report_type": "review",
            "gameweek": 25,
            "data": {"points": {"total": 75}},
        }

        result = await agent.run(context=context)

        assert result.status == AgentStatus.SUCCESS
        assert "report_path" in result.data
        assert "gw25-review.md" in result.data["report_path"]

    @pytest.mark.asyncio
    async def test_run_creates_output_directory(self, tmp_path):
        """Test output directory is created if it doesn't exist."""
        output_dir = tmp_path / "reports" / "nested"
        agent = ReportAgent(config={"output_dir": str(output_dir)})
        context = {
            "report_type": "preview",
            "gameweek": 25,
            "data": {},
        }

        result = await agent.run(context=context)

        assert result.status == AgentStatus.SUCCESS
        assert output_dir.exists()


class TestReportAgentPreview:
    """Tests for preview report generation."""

    def test_generate_preview_inline_deadline(self):
        """Test preview includes deadline."""
        agent = ReportAgent()
        data = {"deadline": "2024-02-10 11:00"}

        content = agent._generate_preview_inline(25, data)

        assert "2024-02-10 11:00" in content

    def test_generate_preview_inline_fixtures(self):
        """Test preview includes fixtures table."""
        agent = ReportAgent()
        data = {
            "gw_fixtures": [
                {"home_team": "Arsenal", "away_team": "Man City", "home_fdr": 4, "away_fdr": 4, "kickoff": "15:00"},
            ]
        }

        content = agent._generate_preview_inline(25, data)

        assert "Arsenal" in content
        assert "Man City" in content
        assert "| Home | FDR |" in content

    def test_generate_preview_inline_my_squad(self):
        """Test preview includes my squad table."""
        agent = ReportAgent()
        data = {
            "my_squad": [
                {"name": "Salah", "team": "LIV", "position": "MID", "form": 7.0, "ownership": "32.1", "status": "✓"},
            ]
        }

        content = agent._generate_preview_inline(25, data)

        assert "My Squad" in content
        assert "Salah" in content

    def test_generate_preview_inline_prices(self):
        """Test preview includes price changes."""
        agent = ReportAgent()
        data = {
            "prices": {
                "risers_this_gw": [
                    {"name": "Player1", "team": "ARS", "current_price": 10.5, "change_this_gw": 0.1},
                ],
                "fallers_this_gw": [
                    {"name": "Player2", "team": "MCI", "current_price": 9.0, "change_this_gw": -0.1},
                ],
            }
        }

        content = agent._generate_preview_inline(25, data)

        assert "Price Rises" in content
        assert "Price Falls" in content


class TestReportAgentReview:
    """Tests for review report generation."""

    def test_generate_review_inline_points(self):
        """Test review includes points summary."""
        agent = ReportAgent()
        data = {
            "points": {
                "total": 75,
                "rank": 100000,
                "overall_rank": 50000,
                "average": 55,
                "highest": 120,
            }
        }

        content = agent._generate_review_inline(25, data)

        assert "75" in content
        assert "Team Summary" in content

    def test_generate_review_inline_team_points(self):
        """Test review includes team points table."""
        agent = ReportAgent()
        data = {
            "team_points": [
                {
                    "name": "Salah", "team": "LIV", "position": "MID",
                    "display_points": 12, "is_captain": True, "contributed": True,
                },
                {"name": "Haaland", "team": "MCI", "position": "FWD", "display_points": 8, "contributed": True},
            ]
        }

        content = agent._generate_review_inline(25, data)

        assert "Team Points" in content
        assert "Salah" in content
        assert "(C)" in content  # Captain marker
