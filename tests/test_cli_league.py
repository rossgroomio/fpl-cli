"""Tests for `fpl league` classic-section position/size reporting."""

from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner


def _standings_page(totals):
    """One page of classic standings, carrying the API's own `rank`.

    That rank is strictly sequential — a points tie is split by fewest
    transfers season-to-date, which no column here shows — so every fixture
    below states it and no assertion trusts it (#344).
    """
    return [
        {
            "entry": i + 1,
            "rank": i + 1,
            "total": total,
            "event_total": 60 - i,
            "player_name": f"Manager{i + 1}",
        }
        for i, total in enumerate(totals)
    ]


def _mock_fpl_client(results, *, rank_count=None, entry_rank=None):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_current_gameweek = AsyncMock(return_value={"id": 25, "finished": True})
    client.get_classic_league_standings = AsyncMock(return_value={
        "league": {"name": "Huge League"},
        "standings": {"results": results},
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


def _run_league(client, entry_id=1):
    from fpl_cli.cli.league import league_command

    with (
        patch(
            "fpl_cli.cli.league.get_settings",
            return_value={"fpl": {"classic_entry_id": entry_id, "classic_league_id": 100}},
        ),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
    ):
        return CliRunner().invoke(league_command, [])


def _table_positions(output):
    """Map manager name -> the Pos cell rendered beside it in the standings table."""
    positions = {}
    for line in output.splitlines():
        if "│" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("│").split("│")]
        if len(cells) == 4 and cells[0] != "Pos":
            positions[cells[1]] = cells[0]
    return positions


class TestLeaguePositionSize:
    """Position/size line must use the entry payload, not the standings page length."""

    def test_position_uses_entry_payload_rank_count_for_large_league(self):
        client = _mock_fpl_client(
            _standings_page([1400, 1300, 1200, 1100]), rank_count=347, entry_rank=42,
        )

        result = _run_league(client)

        assert result.exit_code == 0, result.output
        assert "Position: 1 of 347" in result.output
        assert "Position: 1 of 4" not in result.output

    def test_position_falls_back_to_standings_length_without_entry_payload(self):
        client = _mock_fpl_client(_standings_page([1400, 1300, 1200, 1100]))

        result = _run_league(client)

        assert result.exit_code == 0, result.output
        assert "Position: 1 of 4" in result.output


class TestLeagueClassicTiePositions:
    """#344: the classic table shares a place on a tie, the way the ledger does."""

    def test_tied_managers_share_a_place_and_the_next_total_skips(self):
        # Two managers level on 203 both sit 2nd, so 3rd is consumed by the
        # tie and the next distinct total is 4th. The standings' own rank
        # would have called them 2nd and 3rd.
        client = _mock_fpl_client(_standings_page([205, 203, 203, 202]))

        result = _run_league(client)

        assert result.exit_code == 0, result.output
        positions = _table_positions(result.output)
        assert positions["Manager2"] == "2"
        assert positions["Manager3"] == "2"
        assert positions["Manager4"] == "4"

    def test_untied_table_keeps_the_sequential_numbering(self):
        # Competition ranking only differs where there is a tie.
        client = _mock_fpl_client(_standings_page([205, 204, 203]))

        result = _run_league(client)

        assert result.exit_code == 0, result.output
        assert _table_positions(result.output) == {
            "Manager1": "1", "Manager2": "2", "Manager3": "3",
        }

    def test_users_own_position_line_matches_their_row_in_the_table(self):
        # The reader is the lower half of a tie: the standings call them 3rd,
        # the ledger and `fpl review` call them 2nd. The summary line and the
        # table below it must not disagree.
        client = _mock_fpl_client(_standings_page([205, 203, 203]), rank_count=3)

        result = _run_league(client, entry_id=3)

        assert result.exit_code == 0, result.output
        assert "Position: 2 of 3" in result.output
        assert _table_positions(result.output)["Manager3"] == "2"

    def test_entry_with_no_total_is_left_unplaced_not_ranked_as_zero(self):
        # `derive_point_in_time_positions` asks for members with a known total
        # only. Substituting 0 would tie an entry carrying no total with a
        # manager who really has scored 0, and shift everyone below them.
        page = _standings_page([205, 203])
        del page[1]["total"]

        result = _run_league(_mock_fpl_client(page))

        assert result.exit_code == 0, result.output
        positions = _table_positions(result.output)
        assert positions["Manager1"] == "1"
        assert positions["Manager2"] == "?"

    def test_null_total_on_another_row_still_leaves_the_reader_their_summary(self):
        # The positions are derived before the summary is printed, so a row
        # the helper cannot rank must not cost the reader the block about
        # their own entry.
        page = _standings_page([205, 203, 202])
        page[2]["total"] = None

        result = _run_league(_mock_fpl_client(page, rank_count=3))

        assert result.exit_code == 0, result.output
        assert "Position: 1 of 3" in result.output
