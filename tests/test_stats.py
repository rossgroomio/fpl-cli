"""Tests for StatsAgent Understat enrichment."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from fpl_cli.agents.analysis.stats import StatsAgent


@pytest.fixture
def mock_understat_match():
    """Mock Understat match result for a single player."""
    return {
        "id": 12345,
        "name": "Mohamed Salah",
        "team": "Liverpool",
        "position": "M F",
        "minutes": 1800,
        "npxG": 10.2,
        "npxG_per_90": 0.51,
        "xGChain": 18.5,
        "xGChain_per_90": 0.93,
        "xGBuildup": 5.2,
        "xGBuildup_per_90": 0.26,
        "penalty_xG": 2.3,
        "penalty_xG_per_90": 0.12,
    }


class TestStatsAgentUnderstatEnrichment:
    """Tests for Understat data merging in StatsAgent."""

    def test_merge_understat_data_adds_fields(self, mock_understat_match):
        """Test that Understat metrics are merged into player stats."""
        agent = StatsAgent(config={"gameweeks": 0})

        player_stats = {
            "id": 1,
            "name": "Salah",
            "team": "LIV",
            "position": "MID",
            "minutes": 1800,
        }

        enriched = agent._merge_understat_data(player_stats, mock_understat_match)

        assert enriched["npxG_per_90"] == 0.51
        assert enriched["xGChain_per_90"] == 0.93
        assert enriched["xGBuildup_per_90"] == 0.26
        assert enriched["penalty_xG"] == 2.3
        assert enriched["penalty_xG_per_90"] == 0.12

    def test_merge_understat_data_missing_returns_nones(self):
        """Test graceful fallback when no Understat match."""
        agent = StatsAgent(config={"gameweeks": 0})

        player_stats = {
            "id": 1,
            "name": "Unknown",
            "team": "???",
            "position": "MID",
            "minutes": 900,
        }

        enriched = agent._merge_understat_data(player_stats, None)

        assert enriched["npxG_per_90"] is None
        assert enriched["xGChain_per_90"] is None
        assert enriched["xGBuildup_per_90"] is None
        assert enriched["penalty_xG"] is None
        assert enriched["penalty_xG_per_90"] is None


class TestStatsAgentNpxGScoring:
    """Tests for npxG-aware scoring in StatsAgent."""

    def test_differential_score_differs_with_npxg(self):
        """Score should differ when npxG is available vs when it's None."""
        agent = StatsAgent(config={"gameweeks": 0})

        base = {
            "xGI_per_90": 0.8,
            "form": 5,
            "points_per_game": 5,
            "ownership": 5,
            "GI_minus_xGI": 0,
            "positional_fdr": 3.0,
            "matchup_score": 5.0,
            "minutes": 2400,
            "appearances": 28,
        }

        player_with_npxg = {
            **base,
            "npxG_per_90": 0.2,  # Lower than xGI because penalties stripped
            "xGChain_per_90": 0.3,
        }

        player_without_npxg = {
            **base,
            "npxG_per_90": None,
            "xGChain_per_90": None,
        }

        score_with = agent._calculate_differential_score(player_with_npxg)
        score_without = agent._calculate_differential_score(player_without_npxg)

        # Scores must differ - proves npxG path is active
        assert score_with != score_without
        # Both must be positive
        assert score_with > 0
        assert score_without > 0
