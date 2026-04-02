"""Tests for review-related CLI helpers."""

from unittest.mock import AsyncMock

import pytest

from fpl_cli.cli._helpers import _gw_position_with_half, _live_player_stats
from fpl_cli.cli._review_classic import _format_review_classic_player
from fpl_cli.cli._review_draft import _format_review_draft_player
from fpl_cli.cli._review_summarisation import _names_match, _normalise_name, _review_compare_recs, _review_llm_summarise
from fpl_cli.cli.preview import _preview_build_fixture_map
from fpl_cli.cli.review import _review_resolve_gw

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classic_player(
    name="Salah",
    team="LIV",
    position="MID",
    display_points=6,
    contributed=True,
    is_captain=False,
    is_triple_captain=False,
    auto_sub_in=False,
    auto_sub_out=False,
    red_cards=0,
    bgw=False,
    dgw=False,
):
    return {
        "name": name,
        "team": team,
        "position": position,
        "display_points": display_points,
        "contributed": contributed,
        "is_captain": is_captain,
        "is_triple_captain": is_triple_captain,
        "auto_sub_in": auto_sub_in,
        "auto_sub_out": auto_sub_out,
        "red_cards": red_cards,
        "bgw": bgw,
        "dgw": dgw,
    }


def _draft_player(
    name="Salah",
    team="LIV",
    position="MID",
    points=6,
    contributed=True,
    auto_sub_in=False,
    auto_sub_out=False,
    red_cards=0,
    bgw=False,
    dgw=False,
):
    return {
        "name": name,
        "team": team,
        "position": position,
        "points": points,
        "contributed": contributed,
        "auto_sub_in": auto_sub_in,
        "auto_sub_out": auto_sub_out,
        "red_cards": red_cards,
        "bgw": bgw,
        "dgw": dgw,
    }


def _make_gw(id_=1, finished=False):
    return {"id": id_, "finished": finished}


def _make_client(gameweeks=None, current_gw=None):
    client = AsyncMock()
    client.get_gameweeks = AsyncMock(return_value=gameweeks or [])
    client.get_current_gameweek = AsyncMock(return_value=current_gw)
    return client


# ---------------------------------------------------------------------------
# TestFormatReviewClassicPlayer
# ---------------------------------------------------------------------------

class TestFormatReviewClassicPlayer:

    def test_auto_sub_in(self):
        p = _classic_player(display_points=8, auto_sub_in=True, contributed=True)
        line = _format_review_classic_player(p)
        assert "[AUTO-SUB IN]" in line
        assert "8 [AUTO-SUB IN]" in line

    def test_auto_sub_out_uses_actual_pts(self):
        # Use non-zero points to prove the function doesn't hardcode "(0)"
        p = _classic_player(display_points=3, auto_sub_out=True, contributed=False)
        line = _format_review_classic_player(p)
        assert "(3) [DIDN'T PLAY - auto-subbed out]" in line

    def test_bench_high_pts_unused_warning(self):
        p = _classic_player(display_points=9, contributed=False)
        line = _format_review_classic_player(p)
        assert "[BENCH - 9 pts unused!]" in line

    def test_bench_low_pts_no_warning(self):
        p = _classic_player(display_points=5, contributed=False)
        line = _format_review_classic_player(p)
        assert "[BENCH]" in line
        assert "unused" not in line

    def test_bench_exactly_six_pts_triggers_warning(self):
        p = _classic_player(display_points=6, contributed=False)
        line = _format_review_classic_player(p)
        assert "[BENCH - 6 pts unused!]" in line

    def test_normal_starter_plain_pts(self):
        p = _classic_player(display_points=10, contributed=True)
        line = _format_review_classic_player(p)
        assert "10 pts" in line
        assert "[" not in line

    def test_triple_captain_suffix(self):
        p = _classic_player(display_points=12, is_triple_captain=True, is_captain=True)
        line = _format_review_classic_player(p)
        assert "(TC)" in line
        assert "(C)" not in line

    def test_captain_not_tc(self):
        p = _classic_player(display_points=12, is_captain=True, is_triple_captain=False)
        line = _format_review_classic_player(p)
        assert "(C)" in line
        assert "(TC)" not in line

    def test_red_card_marker(self):
        p = _classic_player(display_points=2, red_cards=1)
        line = _format_review_classic_player(p)
        assert "🟥" in line

    def test_no_red_card_no_marker(self):
        p = _classic_player(display_points=6, red_cards=0)
        line = _format_review_classic_player(p)
        assert "🟥" not in line

    def test_line_format_structure(self):
        p = _classic_player(name="Haaland", team="MCI", position="FWD", display_points=14)
        line = _format_review_classic_player(p)
        assert line.startswith("- Haaland (MCI, FWD):")

    def test_bgw_starter_auto_subbed_out(self):
        p = _classic_player(display_points=0, auto_sub_out=True, contributed=False, bgw=True)
        line = _format_review_classic_player(p)
        assert "(0) [DIDN'T PLAY - BGW]" in line
        assert "auto-subbed out" not in line

    def test_bgw_starter_no_sub_available(self):
        p = _classic_player(display_points=0, contributed=True, bgw=True)
        line = _format_review_classic_player(p)
        assert "(0) [BGW]" in line

    def test_bgw_bench_player(self):
        p = _classic_player(display_points=0, contributed=False, bgw=True)
        line = _format_review_classic_player(p)
        assert "(0) [BGW]" in line
        assert "[BENCH]" not in line

    def test_bgw_captain_still_shows_badge(self):
        p = _classic_player(display_points=0, auto_sub_out=True, contributed=False, bgw=True, is_captain=True)
        line = _format_review_classic_player(p)
        assert "[DIDN'T PLAY - BGW]" in line
        assert "(C)" in line

    def test_dgw_starter(self):
        p = _classic_player(display_points=14, dgw=True)
        line = _format_review_classic_player(p)
        assert "14 [DGW]" in line

    def test_dgw_auto_sub_in(self):
        p = _classic_player(display_points=6, auto_sub_in=True, contributed=True, dgw=True)
        line = _format_review_classic_player(p)
        assert "[AUTO-SUB IN] [DGW]" in line

    def test_dgw_bench_unused(self):
        p = _classic_player(display_points=8, contributed=False, dgw=True)
        line = _format_review_classic_player(p)
        assert "[BENCH - 8 pts unused!] [DGW]" in line

    def test_bgw_and_dgw_simultaneously_bgw_wins(self):
        # Should be impossible in practice, but BGW takes precedence
        p = _classic_player(display_points=0, contributed=False, bgw=True, dgw=True)
        line = _format_review_classic_player(p)
        assert "[BGW]" in line
        # DGW suffix still appended (harmless - both flags can't be true in real data)
        assert "[DGW]" in line

    def test_non_bgw_dgw_unchanged(self):
        p = _classic_player(display_points=6)
        line = _format_review_classic_player(p)
        assert "[BGW" not in line
        assert "[DGW]" not in line


# ---------------------------------------------------------------------------
# TestFormatReviewDraftPlayer
# ---------------------------------------------------------------------------

class TestFormatReviewDraftPlayer:

    def test_uses_points_not_display_points(self):
        # Draft player dict has 'points', not 'display_points'
        p = _draft_player(points=7)
        line = _format_review_draft_player(p)
        assert "7 pts" in line

    def test_auto_sub_in(self):
        p = _draft_player(points=5, auto_sub_in=True, contributed=True)
        line = _format_review_draft_player(p)
        assert "[AUTO-SUB IN]" in line

    def test_auto_sub_out_uses_actual_pts(self):
        p = _draft_player(points=2, auto_sub_out=True, contributed=False)
        line = _format_review_draft_player(p)
        assert "(2) [DIDN'T PLAY - auto-subbed out]" in line

    def test_bench_high_pts_unused_warning(self):
        p = _draft_player(points=8, contributed=False)
        line = _format_review_draft_player(p)
        assert "[BENCH - 8 pts unused!]" in line

    def test_bench_low_pts_no_warning(self):
        p = _draft_player(points=3, contributed=False)
        line = _format_review_draft_player(p)
        assert "[BENCH]" in line
        assert "unused" not in line

    def test_normal_starter_plain_pts(self):
        p = _draft_player(points=9, contributed=True)
        line = _format_review_draft_player(p)
        assert "9 pts" in line
        assert "[" not in line

    def test_no_captain_markers(self):
        # Draft formatter never adds (C) or (TC)
        p = _draft_player(points=12)
        p["is_captain"] = True
        p["is_triple_captain"] = True
        line = _format_review_draft_player(p)
        assert "(C)" not in line
        assert "(TC)" not in line

    def test_red_card_marker(self):
        p = _draft_player(points=1, red_cards=1)
        line = _format_review_draft_player(p)
        assert "🟥" in line

    def test_no_red_card_no_marker(self):
        p = _draft_player(points=6, red_cards=0)
        line = _format_review_draft_player(p)
        assert "🟥" not in line

    def test_bgw_starter_auto_subbed_out(self):
        p = _draft_player(points=0, auto_sub_out=True, contributed=False, bgw=True)
        line = _format_review_draft_player(p)
        assert "(0) [DIDN'T PLAY - BGW]" in line

    def test_bgw_bench_player(self):
        p = _draft_player(points=0, contributed=False, bgw=True)
        line = _format_review_draft_player(p)
        assert "(0) [BGW]" in line

    def test_dgw_starter(self):
        p = _draft_player(points=14, dgw=True)
        line = _format_review_draft_player(p)
        assert "14 [DGW]" in line


# ---------------------------------------------------------------------------
# TestReviewResolveGw
# ---------------------------------------------------------------------------

class TestReviewResolveGw:

    async def test_explicit_gw_not_found_returns_none(self):
        client = _make_client(
            gameweeks=[_make_gw(id_=5, finished=True)],
            current_gw=_make_gw(id_=5, finished=True),
        )
        result = await _review_resolve_gw(client, gameweek=99)
        assert result is None

    async def test_explicit_gw_not_finished_returns_none(self):
        client = _make_client(
            gameweeks=[_make_gw(id_=10, finished=False)],
            current_gw=_make_gw(id_=10, finished=False),
        )
        result = await _review_resolve_gw(client, gameweek=10)
        assert result is None

    async def test_explicit_gw_finished_returns_result(self):
        gw_data = _make_gw(id_=8, finished=True)
        client = _make_client(
            gameweeks=[gw_data],
            current_gw=_make_gw(id_=9, finished=False),
        )
        result = await _review_resolve_gw(client, gameweek=8)
        assert result is not None
        assert result["gw"] == 8
        assert result["gw_data"] == gw_data
        assert result["api_current_gw_id"] == 9

    async def test_no_current_gw_returns_none(self):
        client = _make_client(gameweeks=[], current_gw=None)
        result = await _review_resolve_gw(client, gameweek=None)
        assert result is None

    async def test_current_gw_id_1_in_progress_returns_none(self):
        # id=1, not finished → id-1 = 0 < 1, no completed GW yet
        client = _make_client(
            gameweeks=[_make_gw(id_=1, finished=False)],
            current_gw=_make_gw(id_=1, finished=False),
        )
        result = await _review_resolve_gw(client, gameweek=None)
        assert result is None

    async def test_current_gw_in_progress_derived_gw_not_finished_returns_none(self):
        # current GW=5 in progress → try GW 4; GW 4 is not finished
        client = _make_client(
            gameweeks=[_make_gw(id_=4, finished=False), _make_gw(id_=5, finished=False)],
            current_gw=_make_gw(id_=5, finished=False),
        )
        result = await _review_resolve_gw(client, gameweek=None)
        assert result is None

    async def test_current_gw_finished_returns_result(self):
        gw_data = _make_gw(id_=7, finished=True)
        client = _make_client(
            gameweeks=[gw_data],
            current_gw=gw_data,
        )
        result = await _review_resolve_gw(client, gameweek=None)
        assert result is not None
        assert result["gw"] == 7
        assert result["gw_data"] == gw_data
        assert result["api_current_gw_id"] == 7

    async def test_current_gw_in_progress_derived_gw_finished_returns_result(self):
        prev_gw = _make_gw(id_=6, finished=True)
        curr_gw = _make_gw(id_=7, finished=False)
        client = _make_client(
            gameweeks=[prev_gw, curr_gw],
            current_gw=curr_gw,
        )
        result = await _review_resolve_gw(client, gameweek=None)
        assert result is not None
        assert result["gw"] == 6
        assert result["gw_data"] == prev_gw
        assert result["api_current_gw_id"] == 7


# ---------------------------------------------------------------------------
# TestPreviewBuildFixtureMap
# ---------------------------------------------------------------------------

class TestPreviewBuildFixtureMap:

    def test_single_fixture(self):
        fixtures = [{"home_team": "ARS", "away_team": "LIV", "home_fdr": 3, "away_fdr": 4, "kickoff": "Sat 12:30"}]
        result = _preview_build_fixture_map(fixtures)
        assert result["ARS"] == "LIV"
        assert result["LIV"] == "ars"

    def test_dgw_team_comma_joined(self):
        fixtures = [
            {"home_team": "ARS", "away_team": "LIV", "home_fdr": 3, "away_fdr": 4, "kickoff": "Sat 12:30"},
            {"home_team": "MCI", "away_team": "ARS", "home_fdr": 2, "away_fdr": 5, "kickoff": "Tue 19:45"},
        ]
        result = _preview_build_fixture_map(fixtures)
        assert result["ARS"] == "LIV, mci"

    def test_empty_list_returns_empty_dict(self):
        assert _preview_build_fixture_map([]) == {}

    def test_away_team_gets_home_label(self):
        fixtures = [{"home_team": "TOT", "away_team": "CHE", "home_fdr": 3, "away_fdr": 3, "kickoff": "Sun 16:30"}]
        result = _preview_build_fixture_map(fixtures)
        assert result["CHE"] == "tot"
        assert result["TOT"] == "CHE"


# ---------------------------------------------------------------------------
# TestReviewLlmSummariseGuards
# ---------------------------------------------------------------------------

_LLM_SUMMARISE_BASE_KWARGS = dict(
    gw=1,
    gw_data={},
    collected_data={},
    classic_team=None,
    classic_transfers_data=None,
    classic_league_data=None,
    draft_result=None,
    global_data=None,
    player_map={},
    teams={},
    settings={},
    debug=False,
)


class TestReviewLlmSummariseGuards:

    async def test_raises_if_research_provider_none_and_not_dry_run(self):
        with pytest.raises(ValueError, match="research_provider"):
            await _review_llm_summarise(
                **_LLM_SUMMARISE_BASE_KWARGS,
                dry_run=False,
                research_provider=None,
                synthesis_provider=object(),
            )

    async def test_raises_if_synthesis_provider_none_and_not_dry_run(self):
        with pytest.raises(ValueError, match="synthesis_provider"):
            await _review_llm_summarise(
                **_LLM_SUMMARISE_BASE_KWARGS,
                dry_run=False,
                research_provider=object(),
                synthesis_provider=None,
            )


# ---------------------------------------------------------------------------
# _gw_position_with_half
# ---------------------------------------------------------------------------

class TestGwPositionWithHalf:

    def test_top_half(self):
        assert _gw_position_with_half(3, 11) == "3 [TOP HALF]"

    def test_exact_middle_odd_league(self):
        assert _gw_position_with_half(6, 11) == "6 [EXACT MIDDLE]"

    def test_bottom_half_includes_worst_rank(self):
        # 8th of 11 → 4th worst
        assert _gw_position_with_half(8, 11) == "8 [BOTTOM HALF, 4 worst]"

    def test_bottom_half_tied_includes_worst_rank_with_equals(self):
        # "8=" of 11 → 4th= worst
        assert _gw_position_with_half("8=", 11) == "8= [BOTTOM HALF, 4= worst]"

    def test_last_place(self):
        # 11th of 11 → 1st worst
        assert _gw_position_with_half(11, 11) == "11 [BOTTOM HALF, 1 worst]"

    def test_invalid_position_returns_as_string(self):
        assert _gw_position_with_half("unknown", 11) == "unknown"


# ---------------------------------------------------------------------------
# _review_compare_recs
# ---------------------------------------------------------------------------

def _make_recs(
    captain="Salah",
    vice="Rice",
    transfers=None,
    roll=False,
    waivers=None,
):
    return {
        "gameweek": 30,
        "classic": {
            "captain": captain,
            "vice_captain": vice,
            "transfers": transfers or [],
            "roll_transfer": roll,
        },
        "draft": {
            "waivers": waivers or [],
        },
    }


def _make_collected(
    team_points=None,
    classic_transfers=None,
    draft_transactions=None,
):
    return {
        "team_points": team_points or [],
        "classic_transfers": classic_transfers or [],
        "draft_transactions": draft_transactions or [],
    }


class TestReviewCompareRecsCaptain:

    def test_captain_followed(self):
        recs = _make_recs(captain="Salah")
        collected = _make_collected(team_points=[
            _classic_player(name="Salah", display_points=12, is_captain=True),
            _classic_player(name="Rice", display_points=6),
        ])
        result = _review_compare_recs(recs, collected, {}, {})
        assert result["classic"]["captain_followed"] is True
        assert result["classic"]["captain_pts_delta"] == 0

    def test_captain_diverged(self):
        recs = _make_recs(captain="Salah")
        collected = _make_collected(team_points=[
            _classic_player(name="Salah", display_points=3),
            _classic_player(name="Haaland", display_points=15, is_captain=True),
        ])
        result = _review_compare_recs(recs, collected, {}, {})
        assert result["classic"]["captain_followed"] is False
        assert result["classic"]["rec_captain"] == "Salah"
        assert result["classic"]["actual_captain"] == "Haaland"
        assert result["classic"]["rec_captain_pts"] == 3
        assert result["classic"]["actual_captain_pts"] == 15
        assert result["classic"]["captain_pts_delta"] == 12  # 15 - 3


class TestReviewCompareRecsTransfers:

    def test_transfer_followed(self):
        recs = _make_recs(transfers=[{"in": "Iwobi", "out": "Miley"}])
        collected = _make_collected(classic_transfers=[{
            "player_in": "Iwobi",
            "player_in_team": "FUL",
            "player_in_points": 8,
            "player_out": "Miley",
            "player_out_team": "NEW",
            "player_out_points": 0,
            "net": 8,
            "verdict": "✓ Hit",
        }])
        result = _review_compare_recs(recs, collected, {}, {})
        transfers = result["classic"]["transfers"]
        assert len(transfers) == 1
        assert transfers[0]["followed"] is True

    def test_roll_aligned(self):
        recs = _make_recs(roll=True)
        collected = _make_collected(classic_transfers=[])
        result = _review_compare_recs(recs, collected, {}, {})
        assert result["classic"]["rec_roll"] is True
        assert result["classic"]["actual_roll"] is True

    def test_rec_roll_but_transferred(self):
        recs = _make_recs(roll=True)
        collected = _make_collected(classic_transfers=[{
            "player_in": "Iwobi",
            "player_in_team": "FUL",
            "player_in_points": 5,
            "player_out": "Miley",
            "player_out_team": "NEW",
            "player_out_points": 0,
            "net": 5,
            "verdict": "✓ Hit",
        }])
        result = _review_compare_recs(recs, collected, {}, {})
        assert result["classic"]["rec_roll"] is True
        assert result["classic"]["actual_roll"] is False

    def test_unadvised_transfer_flagged(self):
        recs = _make_recs(transfers=[{"in": "Iwobi", "out": "Miley"}])
        collected = _make_collected(classic_transfers=[
            {
                "player_in": "Iwobi",
                "player_in_team": "FUL",
                "player_in_points": 8,
                "player_out": "Miley",
                "player_out_team": "NEW",
                "player_out_points": 0,
                "net": 8,
                "verdict": "✓ Hit",
            },
            {
                "player_in": "Palmer",
                "player_in_team": "CHE",
                "player_in_points": 2,
                "player_out": "Rogers",
                "player_out_team": "AVL",
                "player_out_points": 6,
                "net": -4,
                "verdict": "✗ Miss",
            },
        ])
        result = _review_compare_recs(recs, collected, {}, {})
        assert len(result["classic"]["unadvised_transfers"]) == 1
        assert result["classic"]["unadvised_transfers"][0]["actual_in"] == "Palmer"


class TestReviewCompareRecsWaivers:

    def test_waiver_followed(self):
        recs = _make_recs(waivers=[{"priority": 1, "in": "Nyoni", "out": "Wirtz"}])
        collected = _make_collected(draft_transactions=[{
            "player_in": "Nyoni",
            "player_in_team": "LIV",
            "player_in_points": 4,
            "player_out": "Wirtz",
            "player_out_team": "LIV",
            "player_out_points": 0,
            "net": 4,
            "verdict": "✓ Hit",
        }])
        result = _review_compare_recs(recs, collected, {}, {})
        waivers = result["draft"]["waivers"]
        assert len(waivers) == 1
        assert waivers[0]["followed"] is True

    def test_waiver_diverged_different_replacement(self):
        recs = _make_recs(waivers=[{"priority": 1, "in": "Nyoni", "out": "Wirtz"}])
        collected = _make_collected(draft_transactions=[{
            "player_in": "Gordon",
            "player_in_team": "NEW",
            "player_in_points": 7,
            "player_out": "Wirtz",
            "player_out_team": "LIV",
            "player_out_points": 0,
            "net": 7,
            "verdict": "✓ Hit",
        }])
        result = _review_compare_recs(recs, collected, {}, {})
        waivers = result["draft"]["waivers"]
        assert len(waivers) == 1
        assert waivers[0]["followed"] is False
        assert waivers[0]["different_replacement"] is True
        assert waivers[0]["actual_in"] == "Gordon"

    def test_waiver_not_executed(self):
        recs = _make_recs(waivers=[{"priority": 1, "in": "Nyoni", "out": "Wirtz"}])
        collected = _make_collected(draft_transactions=[])
        result = _review_compare_recs(recs, collected, {}, {})
        waivers = result["draft"]["waivers"]
        assert len(waivers) == 1
        assert waivers[0]["followed"] is False
        assert waivers[0].get("not_executed") is True


class TestReviewCompareRecsNoFile:

    def test_no_recs_returns_none(self):
        """parse_recommendations returns None when file missing -
        _review_compare_recs should never be called in that case,
        but verify the parser gracefully handles it."""
        from pathlib import Path

        from fpl_cli.parsers.recommendations import parse_recommendations
        assert parse_recommendations(Path("/nonexistent/gw30-recommendations.md")) is None


class TestLivePlayerStats:

    def test_returns_stats_for_known_player(self):
        live_stats = {10: {"total_points": 8, "minutes": 90, "red_cards": 0}}
        pts, mins, reds = _live_player_stats(live_stats, 10)
        assert pts == 8
        assert mins == 90
        assert reds == 0

    def test_returns_zeros_for_unknown_player(self):
        pts, mins, reds = _live_player_stats({}, 999)
        assert pts == 0
        assert mins == 0
        assert reds == 0

    def test_returns_zeros_for_none_player_id(self):
        live_stats = {10: {"total_points": 5, "minutes": 45, "red_cards": 0}}
        pts, mins, reds = _live_player_stats(live_stats, None)
        assert pts == 0
        assert mins == 0
        assert reds == 0

    def test_partial_stats_fills_defaults(self):
        live_stats = {10: {"total_points": 3}}
        pts, mins, reds = _live_player_stats(live_stats, 10)
        assert pts == 3
        assert mins == 0
        assert reds == 0


class TestNamesMatchDiacritics:
    """Verify _names_match handles accented names across sources."""

    def test_accented_vs_ascii(self):
        assert _names_match("Gyökeres", "Gyokeres")

    def test_ascii_vs_accented(self):
        assert _names_match("Raul", "Raúl")

    def test_both_accented(self):
        assert _names_match("Müller", "Müller")

    def test_with_parenthetical(self):
        assert _names_match("Gyökeres (SPU)", "Gyokeres")

    def test_with_initial_and_diacritics(self):
        assert _names_match("L. Díaz", "Diaz")

    def test_mismatch_still_fails(self):
        assert not _names_match("Gyökeres", "Haaland")


class TestNormaliseNameDiacritics:
    """Verify _normalise_name strips diacritics in its pipeline."""

    def test_strips_diacritics_and_lowercases(self):
        assert _normalise_name("Gyökeres") == "gyokeres"

    def test_strips_parenthetical_after_diacritics(self):
        assert _normalise_name("Raúl (FUL)") == "raul"

    def test_strips_initial_after_diacritics(self):
        assert _normalise_name("L. Díaz") == "diaz"
