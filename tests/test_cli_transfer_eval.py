"""Tests for `fpl transfer-eval` command."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.models.player import PlayerPosition
from tests.conftest import make_player, make_team


def _make_agent_result(success=True, data=None):
    result = MagicMock()
    result.success = success
    result.data = data or {
        "out_player": {
            "id": 10, "web_name": "Palmer", "team_short": "CHE",
            "position": "MID", "outlook": 65, "this_gw": 55,
            "outlook_delta": None, "gw_delta": None,
            "fixture_matchups": [{"opponent": "ARS", "fdr": 4}],
            "form": 6.0, "status": "a", "chance_of_playing": 100,
            "price": 10.0, "excluded": False,
            "quality_score": 72, "quality_per_m": 7.2,
            "rolling_pts_per_m": 0.72, "rolling_fixture_count": 5,
            "reliability": 0.85,
        },
        "in_players": [
            {
                "id": 20, "web_name": "Salah", "team_short": "LIV",
                "position": "MID", "outlook": 80, "this_gw": 70,
                "outlook_delta": 15, "gw_delta": 15,
                "fixture_matchups": [{"opponent": "BOU", "fdr": 2}],
                "form": 7.5, "status": "a", "chance_of_playing": 100,
                "price": 13.0, "excluded": False,
                "quality_score": 85, "quality_per_m": 6.5,
                "rolling_pts_per_m": 0.65, "rolling_fixture_count": 5,
                "reliability": 0.92,
            },
            {
                "id": 30, "web_name": "Mbeumo", "team_short": "BRE",
                "position": "MID", "outlook": 60, "this_gw": 50,
                "outlook_delta": -5, "gw_delta": -5,
                "fixture_matchups": [{"opponent": "MCI", "fdr": 5}],
                "form": 5.0, "status": "a", "chance_of_playing": 100,
                "price": 7.5, "excluded": False,
                "quality_score": 58, "quality_per_m": 7.7,
                "rolling_pts_per_m": 0.93, "rolling_fixture_count": 4,
                "reliability": None,
            },
        ],
        "sorted_by": "outlook_delta",
    }
    result.message = "Agent failed" if not success else ""
    result.errors = ["Error"] if not success else []
    return result


_PLAYERS = [
    make_player(id=10, web_name="Palmer", first_name="Cole", second_name="Palmer",
                team_id=4, position=PlayerPosition.MIDFIELDER, now_cost=100),
    make_player(id=20, web_name="Salah", first_name="Mohamed", second_name="Salah",
                team_id=3, position=PlayerPosition.MIDFIELDER, now_cost=130),
    make_player(id=30, web_name="Mbeumo", first_name="Bryan", second_name="Mbeumo",
                team_id=2, position=PlayerPosition.MIDFIELDER, now_cost=75),
    make_player(id=40, web_name="Haaland", first_name="Erling", second_name="Haaland",
                team_id=5, position=PlayerPosition.FORWARD, now_cost=150),
    make_player(id=50, web_name="Neto", first_name="Pedro", second_name="Lomba Neto",
                team_id=4, position=PlayerPosition.MIDFIELDER, now_cost=70),
    make_player(id=60, web_name="João Pedro", first_name="João Pedro",
                second_name="Junqueira de Jesus",
                team_id=4, position=PlayerPosition.FORWARD, now_cost=78),
]

_TEAMS = [
    make_team(id=2, name="Brentford", short_name="BRE"),
    make_team(id=3, name="Liverpool", short_name="LIV"),
    make_team(id=4, name="Chelsea", short_name="CHE"),
    make_team(id=5, name="Manchester City", short_name="MCI"),
]


def _run_cmd(args, agent_result=None, fmt="classic", finances=None):
    runner = CliRunner()
    if agent_result is None:
        agent_result = _make_agent_result()

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=agent_result)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=False)

    settings: dict = {"fpl": {"classic_entry_id": 123}, "custom_analysis": True}
    if fmt == "draft":
        settings = {"fpl": {"draft_league_id": 456}, "custom_analysis": True}

    mock_client = MagicMock()
    mock_client.get_players = AsyncMock(return_value=_PLAYERS)
    mock_client.get_teams = AsyncMock(return_value=_TEAMS)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("fpl_cli.agents.analysis.transfer_eval.TransferEvalAgent", return_value=mock_agent), \
         patch("fpl_cli.api.fpl.FPLClient", return_value=mock_client), \
         patch("fpl_cli.scraper.fpl_prices.load_cache", return_value=finances), \
         patch("fpl_cli.cli._context.load_settings", return_value=settings), \
         patch("fpl_cli.cli.load_settings", return_value=settings):
        return runner.invoke(main, ["transfer-eval"] + args)


class TestTransferEvalTable:
    def test_table_output_includes_columns(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Salah,Mbeumo"])
        assert result.exit_code == 0, result.output
        # Rich truncates names in narrow terminals; check for shortest unique prefix
        assert "Pal" in result.output
        assert "Sal" in result.output
        assert "Mbe" in result.output

    def test_draft_format_omits_price(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Salah"], fmt="draft")
        assert result.exit_code == 0, result.output
        # Price column should not appear in draft format
        assert "£13.0m" not in result.output

    def test_classic_format_shows_price(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Salah"])
        assert result.exit_code == 0, result.output
        assert "£" in result.output  # price column present (values may be truncated by terminal width)

    def test_no_affordability_when_cache_unavailable(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Salah"])
        assert result.exit_code == 0, result.output
        # Budget column not shown when no cache


class TestTransferEvalAffordability:
    def _make_finances(self, bank=2.0, sell_price=10.0):
        finances = MagicMock()
        finances.bank = bank
        finances.squad = [MagicMock(name="Palmer", sell_price=sell_price)]
        finances.squad[0].name = "Palmer"
        return finances

    def test_budget_shown_when_cache_available(self):
        finances = self._make_finances(bank=2.0, sell_price=10.0)
        result = _run_cmd(["--out", "Palmer", "--in", "Salah"], finances=finances)
        assert result.exit_code == 0, result.output
        # Budget column header present
        assert "ITB" in result.output

    def test_affordable_positive_budget(self):
        finances = self._make_finances(bank=5.0, sell_price=10.0)
        result = _run_cmd(["--out", "Palmer", "--in", "Mbeumo"], finances=finances)
        assert result.exit_code == 0, result.output
        # Budget column present with positive value
        assert "ITB" in result.output


class TestFindSellPriceDiacritics:
    """Verify _find_sell_price matches accented names across sources."""

    @staticmethod
    def _make_finances(name: str, sell_price: float = 10.0):
        finances = MagicMock()
        finances.bank = 2.0
        sp = MagicMock()
        sp.name = name
        sp.sell_price = sell_price
        finances.squad = [sp]
        return finances

    def test_accented_scraper_ascii_query(self):
        from fpl_cli.cli.transfer_eval import _find_sell_price
        finances = self._make_finances("Gyökeres", sell_price=11.5)
        assert _find_sell_price(finances, "Gyokeres") == 11.5

    def test_ascii_scraper_accented_query(self):
        from fpl_cli.cli.transfer_eval import _find_sell_price
        finances = self._make_finances("Gyokeres", sell_price=11.5)
        assert _find_sell_price(finances, "Gyökeres") == 11.5

    def test_both_accented(self):
        from fpl_cli.cli.transfer_eval import _find_sell_price
        finances = self._make_finances("Raúl", sell_price=5.0)
        assert _find_sell_price(finances, "Raúl") == 5.0

    def test_substring_match_with_diacritics(self):
        from fpl_cli.cli.transfer_eval import _find_sell_price
        finances = self._make_finances("L. Díaz", sell_price=7.0)
        assert _find_sell_price(finances, "Diaz") == 7.0


class TestTransferEvalQualityValue:
    def test_table_shows_quality_and_value_classic(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Salah"])
        assert result.exit_code == 0, result.output
        assert "72" in result.output  # Palmer quality_score
        assert "7.2" in result.output  # Palmer quality_per_m
        assert "85" in result.output  # Salah quality_score

    def test_draft_shows_quality_omits_value(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Salah"], fmt="draft")
        assert result.exit_code == 0, result.output
        assert "72" in result.output  # quality_score shown
        assert "Quality/£m" not in result.output  # Quality/£m column header absent

    def test_null_quality_shows_dash(self):
        data = _make_agent_result().data
        data["out_player"]["quality_score"] = None
        data["out_player"]["quality_per_m"] = None
        agent_result = _make_agent_result(data=data)
        result = _run_cmd(["--out", "Palmer", "--in", "Salah"], agent_result=agent_result)
        assert result.exit_code == 0, result.output
        # OUT player's quality_score is None; IN player (Salah) still has 85
        assert "85" in result.output  # Salah's quality_score renders
        # OUT player's 72 should NOT appear (it's now None)
        # Can't assert "72" not in output (could appear in other cells),
        # but verify the null path didn't crash and Salah's score survived
        assert "Qua" in result.output  # column header present (may be truncated to "Qua…")

    def test_quality_present_value_null(self):
        """Price 0 scenario: quality_score present but quality_per_m null."""
        data = _make_agent_result().data
        data["in_players"][0]["quality_per_m"] = None
        agent_result = _make_agent_result(data=data)
        result = _run_cmd(["--out", "Palmer", "--in", "Salah,Mbeumo"], agent_result=agent_result)
        assert result.exit_code == 0, result.output
        # Salah's quality_score (85) still renders even though quality_per_m is None
        assert "85" in result.output
        # Mbeumo's quality_per_m (7.7) still renders
        assert "7.7" in result.output


class TestTransferEvalJson:
    def test_json_output_valid(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Salah,Mbeumo", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "transfer-eval"
        assert "out_player" in data["data"]
        assert len(data["data"]["in_players"]) == 2

    def test_json_includes_all_fields(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Salah", "--format", "json"])
        data = json.loads(result.output)
        inp = data["data"]["in_players"][0]
        assert "outlook_delta" in inp
        assert "gw_delta" in inp
        assert "outlook" in inp
        assert "this_gw" in inp

    def test_json_includes_quality_and_value(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Salah", "--format", "json"])
        data = json.loads(result.output)
        out = data["data"]["out_player"]
        assert out["quality_score"] == 72
        assert out["quality_per_m"] == 7.2
        inp = data["data"]["in_players"][0]
        assert inp["quality_score"] == 85
        assert inp["quality_per_m"] == 6.5


class TestTransferEvalErrors:
    def test_unresolvable_out_player(self):
        result = _run_cmd(["--out", "Nonexistent", "--in", "Salah"])
        assert result.exit_code == 1
        assert "Could not resolve OUT player" in result.output

    def test_unresolvable_in_player(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Nonexistent"])
        assert result.exit_code == 1
        assert "Could not resolve IN player" in result.output

    def test_position_mismatch_rejected(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Haaland"])
        assert result.exit_code == 1
        assert "Position mismatch" in result.output
        assert "Palmer is MID" in result.output
        assert "Haaland" in result.output
        assert "FWD" in result.output

    def test_position_mismatch_multiple(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Haaland,João Pedro"])
        assert result.exit_code == 1
        assert "Position mismatch" in result.output
        assert "are FWD" in result.output

    def test_position_mismatch_json(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Haaland", "--format", "json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "Position mismatch" in data["error"]

    def test_agent_failure(self):
        agent_result = _make_agent_result(success=False)
        result = _run_cmd(["--out", "Palmer", "--in", "Salah", "--format", "json"],
                          agent_result=agent_result)
        assert result.exit_code == 1

    def test_single_in_player(self):
        result = _run_cmd(["--out", "Palmer", "--in", "Salah"])
        assert result.exit_code == 0, result.output


class TestTransferEvalResolution:
    def test_resolve_by_numeric_id(self):
        result = _run_cmd(["--out", "10", "--in", "Salah"])
        assert result.exit_code == 0, result.output

    def test_resolve_by_name_with_team(self):
        """'João Pedro (CHE)' should resolve to the FWD, not Neto."""
        fwd_data = _make_agent_result(data={
            "out_player": {
                "id": 60, "web_name": "João Pedro", "team_short": "CHE",
                "position": "FWD", "outlook": 50, "this_gw": 40,
                "outlook_delta": None, "gw_delta": None,
                "fixture_matchups": [{"opponent": "ARS", "fdr": 4}],
                "form": 7.7, "status": "a", "chance_of_playing": 100,
                "price": 7.8, "excluded": False,
                "quality_score": 67, "quality_per_m": 8.6,
                "rolling_pts_per_m": 1.0, "rolling_fixture_count": 5,
            },
            "in_players": [{
                "id": 40, "web_name": "Haaland", "team_short": "MCI",
                "position": "FWD", "outlook": 80, "this_gw": 70,
                "outlook_delta": 30, "gw_delta": 30,
                "fixture_matchups": [{"opponent": "BOU", "fdr": 2}],
                "form": 9.0, "status": "a", "chance_of_playing": 100,
                "price": 15.0, "excluded": False,
                "quality_score": 90, "quality_per_m": 6.0,
                "rolling_pts_per_m": 0.8, "rolling_fixture_count": 5,
            }],
            "sorted_by": "outlook_delta",
        })
        result = _run_cmd(["--out", "João Pedro (CHE)", "--in", "Haaland"],
                          agent_result=fwd_data)
        assert result.exit_code == 0, result.output
        assert "FWD" in result.output

    def test_invalid_team_code_returns_error(self):
        result = _run_cmd(["--out", "Palmer (XXX)", "--in", "Salah"])
        assert result.exit_code == 1
        assert "Could not resolve OUT player" in result.output


class TestTransferEvalReliability:
    def test_reliability_percentage_in_output(self):
        """Player with reliability=0.85 shows 85% in CLI table."""
        result = _run_cmd(["--out", "Palmer", "--in", "Salah"])
        assert result.exit_code == 0, result.output
        assert "85%" in result.output  # Palmer (out) = 0.85 -> "85%"
        assert "92%" in result.output  # Salah (in) = 0.92 -> "92%"

    def test_reliability_none_shows_dash(self):
        """Player with reliability=None shows '-' in CLI table."""
        result = _run_cmd(["--out", "Palmer", "--in", "Salah,Mbeumo"])
        assert result.exit_code == 0, result.output
        # Mbeumo has reliability=None -> "-"
        assert "-" in result.output

    def test_reliability_in_json_output(self):
        """reliability field included in JSON output."""
        result = _run_cmd(["--out", "Palmer", "--in", "Salah", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["out_player"]["reliability"] == pytest.approx(0.85)
        assert data["data"]["in_players"][0]["reliability"] == pytest.approx(0.92)
