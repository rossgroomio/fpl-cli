"""Tests for `fpl league`: classic position/size reporting and the GW performer lists."""

from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli._context import CLIContext, Format


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


def _run_league(client, entry_id=1, *, use_net_points=False):
    from fpl_cli.cli.league import league_command

    with (
        patch(
            "fpl_cli.cli.league.get_settings",
            return_value={
                "fpl": {"classic_entry_id": entry_id, "classic_league_id": 100},
                "use_net_points": use_net_points,
            },
        ),
        patch("fpl_cli.api.fpl.FPLClient", return_value=client),
    ):
        return CliRunner().invoke(league_command, [])


def _section(output, header):
    """The rows printed under a `### <header>` heading, stripped, up to the next blank line."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"### {header}"))
    rows = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        rows.append(line.strip())
    return rows


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


def _performer_client(rows, *, has_next=False):
    """A classic league whose GW scores and hits are `rows` of (name, gross, hit).

    Totals descend with the row order so the standings table is untied; only
    the GW scores under test carry ties. Entry ids are 1-based row positions.
    """
    client = _mock_fpl_client([
        {"entry": i + 1, "rank": i + 1, "total": 500 - i, "event_total": gross, "player_name": name}
        for i, (name, gross, _) in enumerate(rows)
    ])
    client.get_classic_league_standings.return_value["standings"]["has_next"] = has_next
    client.get_manager_picks = AsyncMock(
        side_effect=lambda entry, gw: {"entry_history": {"event_transfers_cost": rows[entry - 1][2]}},
    )
    return client


# The issue's eight managers (#381): two level at third, a hit-taker at the
# bottom, and the user top of the gameweek, well clear of the bottom five.
_ISSUE_ROWS = [
    ("Me", 63, 0), ("Bob Baker", 60, 0), ("Cam Cole", 58, 0), ("Dan Drew", 58, 0),
    ("Fay Ford", 45, 0), ("Gus Grant", 40, 0), ("Hal Hart", 36, 0), ("Eve Evans", 33, 4),
]


class TestLeagueClassicPerformers:
    """#381: `fpl league` lists GW performers the way `fpl review` does."""

    def test_managers_level_at_the_cut_are_both_listed_as_a_shared_place(self):
        result = _run_league(_performer_client(_ISSUE_ROWS), use_net_points=True)

        assert result.exit_code == 0, result.output
        assert _section(result.output, "Best GW Performers") == [
            "1. You - 63 pts",
            "2. Bob Baker - 60 pts",
            "3=. Cam Cole - 58 pts",
            "3=. Dan Drew - 58 pts",
        ]

    def test_worst_list_keeps_a_tie_at_fifth_and_words_a_hit_as_review_does(self):
        result = _run_league(_performer_client(_ISSUE_ROWS), use_net_points=True)

        assert result.exit_code == 0, result.output
        worst = _section(result.output, "Worst GW Performers")
        assert worst[:6] == [
            "1. Eve Evans - 29 net pts (33 gross, -4 hit)",
            "2. Hal Hart - 36 pts",
            "3. Gus Grant - 40 pts",
            "4. Fay Ford - 45 pts",
            "5=. Cam Cole - 58 pts",
            "5=. Dan Drew - 58 pts",
        ]
        assert "gross, -4 hit = 29 net" not in result.output

    def test_user_above_the_bottom_five_gets_their_gw_rank_not_a_row(self):
        result = _run_league(_performer_client(_ISSUE_ROWS), use_net_points=True)

        assert result.exit_code == 0, result.output
        worst = _section(result.output, "Worst GW Performers")
        assert worst[-1] == "Your GW rank: 1 of 8 - 63 pts"
        assert not any("You -" in row for row in worst)

    def test_user_in_the_bottom_five_is_listed_there_without_a_rank_line(self):
        rows = [("Ann Ames", 70, 0), ("Bob Baker", 60, 0), ("Me", 30, 0)]

        result = _run_league(_performer_client(rows), entry_id=3)

        assert result.exit_code == 0, result.output
        assert _section(result.output, "Worst GW Performers")[0] == "1. You - 30 pts"
        assert "Your GW rank" not in result.output

    def test_users_own_hit_reads_the_same_in_their_gw_rank_line(self):
        rows = [("Me", 70, 8)] + [(f"Manager{i}", 40 - i, 0) for i in range(2, 8)]

        result = _run_league(_performer_client(rows), use_net_points=True)

        assert result.exit_code == 0, result.output
        assert "1. You - 62 net pts (70 gross, -8 hit)" in _section(result.output, "Best GW Performers")
        assert _section(result.output, "Worst GW Performers")[-1] == (
            "Your GW rank: 1 of 7 - 62 net pts (70 gross, -8 hit)"
        )

    def test_rank_line_names_no_field_size_when_the_page_is_not_the_whole_league(self):
        # A league past one 50-entry page ranks the GW within the page only,
        # so "of 8" would read as the whole league.
        result = _run_league(_performer_client(_ISSUE_ROWS, has_next=True), use_net_points=True)

        assert result.exit_code == 0, result.output
        assert _section(result.output, "Worst GW Performers")[-1] == "Your GW rank: 1 - 63 pts"


def _run_draft_league(event_totals, *, draft_entry_id=1001):
    """`fpl league` in draft format over managers scoring `event_totals` this GW.

    Manager i (1-based) has league entry i, entry id 1000 + i and the name
    "First{i} Last{i}"; totals descend with the order so the table is untied.
    """
    from fpl_cli.cli.league import league_command

    fpl_client = _mock_fpl_client([])
    draft_client = MagicMock()
    draft_client.__aenter__ = AsyncMock(return_value=draft_client)
    draft_client.__aexit__ = AsyncMock(return_value=False)
    draft_client.get_league_details = AsyncMock(return_value={
        "league": {"name": "Draft League"},
        "standings": [
            {"league_entry": i, "rank": i, "total": 900 - i, "event_total": pts}
            for i, pts in enumerate(event_totals, 1)
        ],
        "league_entries": [
            {"id": i, "entry_id": 1000 + i, "player_first_name": f"First{i}", "player_last_name": f"Last{i}"}
            for i in range(1, len(event_totals) + 1)
        ],
    })
    with (
        patch(
            "fpl_cli.cli.league.get_settings",
            return_value={"fpl": {"draft_league_id": 200, "draft_entry_id": draft_entry_id}},
        ),
        patch("fpl_cli.api.fpl.FPLClient", return_value=fpl_client),
        patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft_client),
    ):
        return CliRunner().invoke(league_command, [], obj=CLIContext(format=Format.DRAFT, settings={}))


class TestLeagueDraftPerformers:
    """#381: the draft lists share a place on a tie and keep the tie past the cut."""

    def test_managers_level_on_gw_points_share_a_place_in_best(self):
        result = _run_draft_league([70, 55, 55, 40, 30, 20])

        assert result.exit_code == 0, result.output
        assert _section(result.output, "Best GW Performers") == [
            "1. You - 70 pts",
            "2=. First2 Last2 - 55 pts",
            "2=. First3 Last3 - 55 pts",
        ]

    def test_a_tie_at_the_third_place_cut_keeps_both_managers_in_worst(self):
        result = _run_draft_league([70, 55, 41, 40, 40, 20])

        assert result.exit_code == 0, result.output
        assert _section(result.output, "Worst GW Performers") == [
            "1. First6 Last6 - 20 pts",
            "2=. First4 Last4 - 40 pts",
            "2=. First5 Last5 - 40 pts",
        ]
