"""Tests for `fpl league` classic-section position/size reporting."""

from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner


class TestLeaguePositionSize:
    """Position/size line must use the entry payload, not the standings page length."""

    def _mock_fpl_client(self, *, rank_count: int | None = None, entry_rank: int | None = None):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_current_gameweek = AsyncMock(return_value={"id": 25, "finished": True})
        client.get_classic_league_standings = AsyncMock(return_value={
            "league": {"name": "Huge League"},
            "standings": {"results": [
                {"entry": 1, "rank": 42, "total": 1200, "event_total": 60, "player_name": "You"},
            ]},
        })
        leagues = {}
        if rank_count is not None or entry_rank is not None:
            leagues = {
                key: value for key, value in
                {"id": 100, "rank_count": rank_count, "entry_rank": entry_rank}.items()
                if value is not None or key == "id"
            }
        client.get_manager_entry = AsyncMock(return_value={
            "leagues": {"classic": [leagues] if leagues else []},
        })
        return client

    def test_position_uses_entry_payload_rank_count_for_large_league(self):
        from fpl_cli.cli.league import league_command

        client = self._mock_fpl_client(rank_count=347, entry_rank=42)
        with (
            patch(
                "fpl_cli.cli.league.load_settings",
                return_value={"fpl": {"classic_entry_id": 1, "classic_league_id": 100}},
            ),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        ):
            runner = CliRunner()
            result = runner.invoke(league_command, [])

        assert result.exit_code == 0, result.output
        assert "Position: 42 of 347" in result.output
        assert "Position: 42 of 1" not in result.output

    def test_position_falls_back_to_standings_length_without_entry_payload(self):
        from fpl_cli.cli.league import league_command

        client = self._mock_fpl_client()
        with (
            patch(
                "fpl_cli.cli.league.load_settings",
                return_value={"fpl": {"classic_entry_id": 1, "classic_league_id": 100}},
            ),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
        ):
            runner = CliRunner()
            result = runner.invoke(league_command, [])

        assert result.exit_code == 0, result.output
        assert "Position: 42 of 1" in result.output
