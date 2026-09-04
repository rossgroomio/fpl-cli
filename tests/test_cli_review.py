"""Tests for review-related CLI helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fpl_cli.cli._helpers import _format_pts_display, _gw_position_with_half, _live_player_stats
from fpl_cli.cli._review_classic import (
    _format_review_classic_player,
    _review_classic_league,
    _review_classic_transfers,
)
from fpl_cli.cli._review_draft import _format_review_draft_player, _review_draft
from fpl_cli.cli._review_summarisation import (
    _classic_fines_league_data,
    _classic_position_fields,
    _format_classic_section,
    _names_match,
    _normalise_name,
    _review_compare_recs,
    _review_llm_summarise,
)
from fpl_cli.cli.preview import _preview_build_fixture_map
from fpl_cli.cli.review import _review_resolve_gw
from tests.conftest import make_draft_player, make_player, make_team

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
# TestFormatPtsDisplay — Rich-table renderer (separate path from LLM prompt)
# ---------------------------------------------------------------------------

class TestFormatPtsDisplay:

    def test_bench_boost_player_gets_bb_suffix(self):
        p = {"display_points": 5, "contributed": True, "is_bench_boost_player": True}
        out = _format_pts_display(p, points_key="display_points")
        assert "[BB]" in out
        assert "(" not in out  # contributor - no brackets around pts

    def test_no_bb_suffix_when_flag_absent(self):
        p = {"display_points": 5, "contributed": True}
        out = _format_pts_display(p, points_key="display_points")
        assert "[BB]" not in out

    def test_no_bb_suffix_when_flag_false(self):
        p = {"display_points": 5, "contributed": True, "is_bench_boost_player": False}
        out = _format_pts_display(p, points_key="display_points")
        assert "[BB]" not in out

    def test_auto_sub_in_takes_precedence_over_bb(self):
        # Defensive: auto_sub + BB shouldn't co-occur in real data, but render path
        # prioritises auto-sub markers. Locks in that precedence.
        p = {"display_points": 6, "auto_sub_in": True, "is_bench_boost_player": True}
        out = _format_pts_display(p, points_key="display_points")
        assert "[SUB IN]" in out
        assert "[BB]" not in out


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


# ---------------------------------------------------------------------------
# GW1 (opening gameweek) behaviour
# ---------------------------------------------------------------------------

class TestReviewResolveGwPreSeason:

    async def test_no_current_gw_and_nothing_finished_reports_season_not_started(self, capsys):
        client = _make_client(
            gameweeks=[
                {"id": 1, "finished": False, "deadline_time": "2026-08-21T17:30:00Z"},
                {"id": 2, "finished": False, "deadline_time": "2026-08-28T17:30:00Z"},
            ],
            current_gw=None,
        )
        result = await _review_resolve_gw(client, gameweek=None)
        assert result is None
        out = capsys.readouterr().err
        assert "Season hasn't started" in out
        assert "Could not determine" not in out

    async def test_no_current_gw_with_finished_gws_keeps_generic_message(self, capsys):
        # Season over / API oddity: something finished, so this is not pre-season.
        client = _make_client(
            gameweeks=[{"id": 38, "finished": True}],
            current_gw=None,
        )
        result = await _review_resolve_gw(client, gameweek=None)
        assert result is None
        assert "Could not determine current gameweek" in capsys.readouterr().err


class TestReviewClassicTransfersGw1:

    async def test_gw1_no_transfers_prints_season_opener_note(self, capsys):
        client = AsyncMock()
        client.get_manager_transfers = AsyncMock(return_value=[])
        result = await _review_classic_transfers(client, 123, 1, {}, {}, {})
        assert result == []
        assert "bought pre-season" in capsys.readouterr().out

    async def test_later_gw_no_transfers_stays_silent(self, capsys):
        client = AsyncMock()
        client.get_manager_transfers = AsyncMock(return_value=[])
        result = await _review_classic_transfers(client, 123, 7, {}, {}, {})
        assert result == []
        assert capsys.readouterr().out == ""


class TestReviewClassicLeaguePendingStandings:

    async def test_empty_standings_flags_pending_and_omits_zero_league(self, capsys):
        client = AsyncMock()
        client.get_classic_league_standings = AsyncMock(return_value={
            "league": {"name": "Office League"},
            "new_entries": {"results": [{"entry": 123}]},
            "standings": {"results": []},
        })
        result = await _review_classic_league(client, 999, 123, 1, 1)
        assert result == {"league_name": "Office League", "standings_pending": True}
        out = capsys.readouterr().out
        assert "standings not published yet" in out
        # No hollow performer tables when there are no entries to rank
        assert "Best GW Performers" not in out
        assert "Worst GW Performers" not in out


class TestReviewClassicLeagueUserNotOnPage:

    async def test_user_absent_from_page_marks_not_found(self):
        # A league with more entries than fit on one standings page: the
        # manager's own entry isn't on the fetched page, but total_entries
        # (len of that page) is still real and nonzero.
        client = AsyncMock()
        client.get_classic_league_standings = AsyncMock(return_value={
            "league": {"name": "Big League"},
            "standings": {"results": [
                {"entry": 1, "rank": 1, "total": 500, "event_total": 60, "player_name": "Someone Else"},
            ]},
        })
        result = await _review_classic_league(client, 999, 123, 5, 5)
        assert result is not None
        assert result["total_entries"] == 1
        assert result["user_found_in_standings"] is False
        assert result["user_gw_rank"] is None
        assert result["user_gw_points"] == 0


class TestClassicPositionFields:

    def test_populated_league_annotates_position(self):
        fields = _classic_position_fields({
            "user_gw_rank": "3", "user_position": 5, "total_entries": 11,
        })
        assert fields["total"] == 11
        assert fields["position"] == 5
        assert "TOP HALF" in fields["gw_position"]

    def test_pending_standings_reports_unknown(self):
        fields = _classic_position_fields({"league_name": "L", "standings_pending": True})
        assert fields == {"gw_position": "unknown", "position": "unknown", "total": "unknown"}

    def test_missing_league_reports_unknown(self):
        assert _classic_position_fields(None)["total"] == "unknown"

    def test_user_absent_from_truncated_standings_reports_unknown(self):
        # A >50-entry league still reports a real, nonzero total_entries even
        # when the manager's own entry wasn't on the fetched page-1 standings.
        # total_entries alone can't tell that apart from a known position.
        fields = _classic_position_fields({
            "user_gw_rank": None, "user_position": "?", "total_entries": 50,
            "user_found_in_standings": False,
        })
        assert fields == {"gw_position": "unknown", "position": "unknown", "total": "unknown"}


class TestClassicFinesLeagueData:

    def test_league_points_preferred_when_present(self):
        league = {"user_gw_points": 44, "worst_performers": []}
        entry = {"points": 56, "transfers_cost": 0}
        assert _classic_fines_league_data(league, entry) is league

    def test_falls_back_when_user_absent_from_truncated_standings(self):
        # A defaulted 0 that nobody actually scored must not out-rank the
        # manager's real entry-history score just because the key is present.
        league = {
            "user_gw_points": 0, "total_entries": 50, "user_found_in_standings": False,
        }
        entry = {"points": 68, "transfers_cost": 0}
        result = _classic_fines_league_data(league, entry)
        assert result is not None
        assert result["user_gw_points"] == 68

    def test_falls_back_to_entry_points_when_standings_pending(self):
        entry = {"points": 56, "transfers_cost": 4}
        result = _classic_fines_league_data({"league_name": "L", "standings_pending": True}, entry)
        assert result is not None
        assert result["user_gw_points"] == 56
        assert result["user_gw_net_points"] == 52
        assert result["league_name"] == "L"

    def test_falls_back_when_league_missing_entirely(self):
        result = _classic_fines_league_data(None, {"points": 31, "transfers_cost": 0})
        assert result == {"user_gw_points": 31, "user_gw_net_points": 31}

    def test_no_entry_summary_leaves_league_data_alone(self):
        assert _classic_fines_league_data(None, None) is None


class TestFormatClassicSectionTransfers:

    def test_gw1_explains_transfers_do_not_exist(self):
        result = _format_classic_section([], [], {}, [], gameweek=1)
        assert "transfers do not exist in GW1" in result["transfers"]
        assert "rolled" in result["transfers"]

    def test_later_gw_keeps_no_transfers_wording(self):
        result = _format_classic_section([], [], {}, [], gameweek=9)
        assert result["transfers"] == "No transfers this week"

    def test_transfers_present_unaffected_by_gameweek(self):
        transfers = [{
            "player_out": "Miley", "player_out_points": 2,
            "player_in": "Iwobi", "player_in_points": 9,
            "net": 7, "verdict": "✓ Hit",
        }]
        result = _format_classic_section([], [], {}, transfers, gameweek=1)
        assert "Iwobi" in result["transfers"]


class TestReviewCompareRecsGw1:

    def test_gw1_does_not_score_absent_transfers_as_a_roll(self):
        recs = _make_recs(roll=True)
        collected = _make_collected(classic_transfers=[])
        result = _review_compare_recs(recs, collected, {}, {}, gameweek=1)
        assert result["classic"]["no_transfers_possible"] is True
        assert result["classic"]["rec_roll"] is False
        assert result["classic"]["actual_roll"] is False

    def test_later_gw_still_scores_a_roll_as_aligned(self):
        recs = _make_recs(roll=True)
        collected = _make_collected(classic_transfers=[])
        result = _review_compare_recs(recs, collected, {}, {}, gameweek=12)
        assert result["classic"]["no_transfers_possible"] is False
        assert result["classic"]["rec_roll"] is True
        assert result["classic"]["actual_roll"] is True

    def test_gameweek_omitted_keeps_previous_behaviour(self):
        recs = _make_recs(roll=True)
        result = _review_compare_recs(recs, _make_collected(), {}, {})
        assert result["classic"]["actual_roll"] is True


# ---------------------------------------------------------------------------
# TestReviewPlayerClubLabel
# ---------------------------------------------------------------------------

class TestReviewPlayerClubLabel:
    """#150: the prompt line is the only place the model learns a player's club,
    so it spells the club out rather than leaving a code to expand."""

    def test_classic_line_uses_full_club_name(self):
        p = _classic_player(name="Gyökeres", team="ARS", position="FWD", display_points=9)
        p["team_name"] = "Arsenal"
        assert _format_review_classic_player(p) == "- Gyökeres (Arsenal, FWD): 9 pts"

    def test_draft_line_uses_full_club_name(self):
        p = _draft_player(name="Eze", team="ARS", position="MID", points=3)
        p["team_name"] = "Arsenal"
        assert _format_review_draft_player(p) == "- Eze (Arsenal, MID): 3 pts"

    def test_falls_back_to_short_code_without_a_full_name(self):
        # Older callers (and any squad row we couldn't resolve a club for) still
        # produce a usable line rather than a KeyError.
        p = _classic_player(name="Salah", team="LIV", position="MID", display_points=6)
        assert _format_review_classic_player(p) == "- Salah (LIV, MID): 6 pts"

    def test_club_label_survives_status_annotations(self):
        p = _classic_player(name="Wissa", team="NEW", position="FWD", display_points=9,
                            contributed=False)
        p["team_name"] = "Newcastle"
        line = _format_review_classic_player(p)
        assert line.startswith("- Wissa (Newcastle, FWD):")
        assert "[BENCH - 9 pts unused!]" in line


# ---------------------------------------------------------------------------
# TestReviewTransferClubLabel
# ---------------------------------------------------------------------------

class TestReviewTransferClubLabel:
    """A player transferred in is by definition someone whose situation just
    changed — the likeliest #150 trigger of all — so the Transfers block names
    them with a club rather than leaving it to the model."""

    @staticmethod
    def _transfer(**overrides):
        move = {
            "player_out": "Watkins", "player_out_team": "AVL",
            "player_out_team_name": "Aston Villa", "player_out_points": 2,
            "player_in": "Gyökeres", "player_in_team": "ARS",
            "player_in_team_name": "Arsenal", "player_in_points": 9,
            "net": 7, "verdict": "✓ Hit", "kind": "w",
        }
        move.update(overrides)
        return move

    def test_classic_transfer_line_names_both_clubs(self):
        from fpl_cli.cli._review_summarisation import _format_classic_section

        text = _format_classic_section([], [], {}, [self._transfer()], gameweek=9)["transfers"]
        assert "Watkins (Aston Villa) (2 pts) → Gyökeres (Arsenal) (9 pts)" in text

    def test_draft_waiver_line_names_both_clubs(self):
        from fpl_cli.cli._review_summarisation import _format_draft_section

        text = _format_draft_section([], [], {}, [self._transfer()])["transactions"]
        assert "Watkins (Aston Villa) (2 pts) → Gyökeres (Arsenal) (9 pts)" in text

    def test_falls_back_to_a_bare_name_when_the_club_did_not_resolve(self):
        from fpl_cli.cli._review_summarisation import _format_classic_section

        move = self._transfer(player_in_team_name=None, player_out_team_name=None)
        text = _format_classic_section([], [], {}, [move], gameweek=9)["transfers"]
        assert "- Watkins (2 pts) → Gyökeres (9 pts)" in text

    def test_draft_free_agent_pickup_keeps_its_placeholder(self):
        from fpl_cli.cli._review_summarisation import _format_draft_section

        move = self._transfer(player_out=None, player_out_team_name=None, player_out_points=0)
        text = _format_draft_section([], [], {}, [move])["transactions"]
        assert "- Free agent (0 pts) → Gyökeres (Arsenal) (9 pts)" in text

    def test_no_placeholder_club_reaches_the_squad_line(self):
        """"Unknown club" beside a name reads like a real club to the model,
        right where the prompt calls that bracket authoritative."""
        p = _classic_player(name="Mystery", team="???", display_points=1)
        p["team_name"] = None
        assert _format_review_classic_player(p) == "- Mystery (???, MID): 1 pts"


class TestReviewDraftPlayerMatching:
    """#168: the draft→main ID map is what pulls a draft player's live stats,
    so a name the main game changed must not quietly zero their gameweek."""

    @staticmethod
    def _draft_client(draft_elements, picks):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value={
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 9, "total": 9, "rank": 1}],
            "league_entries": [
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
            ],
        })
        client.get_bootstrap_static = AsyncMock(return_value={"elements": draft_elements})
        client.get_entry_picks = AsyncMock(return_value={"picks": picks, "subs": []})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        return client

    async def _run(self, draft_elements, picks, players, live_stats):
        client = self._draft_client(draft_elements, picks)
        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            return await _review_draft(
                MagicMock(), 1, 1, gw=15, api_current_gw_id=15,
                players=players, player_map={p.id: p for p in players},
                teams={19: make_team(id=19, short_name="MCI", name="Man City")},
                live_stats=live_stats,
            )

    async def test_renamed_draft_player_takes_the_main_games_points(self):
        """The draft game kept `Savinho` after the main game moved to `Sávio`.
        Both bootstraps still agree on the code, so his 9 points must land."""
        renamed = make_draft_player(id=403, code=510281, web_name="Savinho", team=19, element_type=3)
        main_player = make_player(id=403, code=510281, web_name="Sávio", team_id=19)

        data = await self._run(
            [renamed], [{"element": 403, "position": 1}], [main_player],
            {403: {"total_points": 9, "minutes": 90}},
        )

        squad = data["draft_squad_points_data"]
        assert [p["name"] for p in squad] == ["Savinho"]
        assert squad[0]["points"] == 9
        assert squad[0]["minutes"] == 90

    async def test_a_player_neither_key_resolves_still_reads_zero(self):
        """The fallback must not invent a match: an element the main game has
        no row for keeps the zero it has always had."""
        stranger = make_draft_player(id=901, code=999999, web_name="Mystery", team=19, element_type=3)
        main_player = make_player(id=403, code=510281, web_name="Sávio", team_id=19)

        data = await self._run(
            [stranger], [{"element": 901, "position": 1}], [main_player],
            {403: {"total_points": 9, "minutes": 90}},
        )

        assert data["draft_squad_points_data"][0]["points"] == 0


# ---------------------------------------------------------------------------
# TestReviewStaleClubFlags
# ---------------------------------------------------------------------------

class TestReviewStaleClubFlags:
    """#174: `-g` reviews a finished gameweek, but the club a player is at
    today is the only one the bootstrap knows. Reach back past a transfer and
    that club's fixture list was never his, so which players blanked -- and
    which doubled -- has to be read off the gameweek's own live data instead.

    In every case here GW15 blanked club 3 and gave club 19 a double. `mover`
    is at the blanking club today and played both of the other one's fixtures
    that gameweek; `leaver` is the mirror image.
    """

    TEAMS = {
        3: make_team(id=3, short_name="BLA", name="Blanked FC"),
        19: make_team(id=19, short_name="MCI", name="Man City"),
    }
    # Only `mover` has `explain` entries in GW15's live payload, because only
    # his club of the day took the pitch -- twice, so he doubled as well.
    WITH_FIXTURE = frozenset({401})
    WITH_DOUBLE = frozenset({401})

    @staticmethod
    def _players():
        return [
            make_player(id=401, code=1401, web_name="Mover", team_id=3, selected_by_percent=20.0),
            make_player(id=402, code=1402, web_name="Leaver", team_id=19, selected_by_percent=30.0),
        ]

    LIVE_STATS = {  # noqa: RUF012 — plain test data, not a mutable default
        401: {"total_points": 0, "minutes": 0},
        402: {"total_points": 0, "minutes": 0},
    }

    async def _global_stats(self, **kwargs):
        from fpl_cli.cli._review_analysis import _review_global_stats

        client = MagicMock()
        client.get_dream_team = AsyncMock(return_value={"team": []})
        return await _review_global_stats(
            client, 15, {p.id: p for p in self._players()}, self.TEAMS, self.LIVE_STATS,
            bgw_team_ids=frozenset({3}), **kwargs,
        )

    async def test_blankers_are_read_off_the_gameweek_not_todays_club(self):
        """The real zero is the one whose club played that week, whatever
        shirt he wears now."""
        data = await self._global_stats(players_with_fixture=self.WITH_FIXTURE)
        assert [b["name"] for b in data["blankers"]] == ["Mover"]

    async def test_blankers_fall_back_to_the_club_when_the_gameweek_cannot_answer(self):
        """A gameweek still in play gives no answer, and the club decides as
        it always did."""
        data = await self._global_stats(players_with_fixture=None)
        assert [b["name"] for b in data["blankers"]] == ["Leaver"]

    async def _classic_team(self, **kwargs):
        from fpl_cli.cli._review_classic import _review_classic_team

        client = MagicMock()
        client.get_manager_picks = AsyncMock(return_value={
            "entry_history": {"points": 0},
            "active_chip": None,
            "automatic_subs": [],
            "picks": [
                {"element": 401, "position": 1, "multiplier": 1},
                {"element": 402, "position": 2, "multiplier": 1},
            ],
        })
        return await _review_classic_team(
            client, 1, 15, {p.id: p for p in self._players()}, self.TEAMS,
            {"id": 15}, self.LIVE_STATS,
            bgw_team_ids=frozenset({3}), dgw_team_ids=frozenset({19}), **kwargs,
        )

    async def test_classic_bgw_flag_follows_the_gameweeks_own_record(self):
        """`_format_review_review_player` lets `bgw` override every other
        annotation, so excusing the wrong zero is what reaches the user and
        the prompt."""
        data = await self._classic_team(players_with_fixture=self.WITH_FIXTURE)
        assert {p["name"]: p["bgw"] for p in data["my_picks_data"]} == {
            "Mover": False, "Leaver": True,
        }

    async def test_classic_bgw_flag_falls_back_to_the_club(self):
        data = await self._classic_team(players_with_fixture=None)
        assert {p["name"]: p["bgw"] for p in data["my_picks_data"]} == {
            "Mover": True, "Leaver": False,
        }

    async def test_classic_dgw_flag_follows_the_gameweeks_own_record(self):
        """The twin flag, and wrong in both directions for the same reason:
        `[DGW]` is a suffix on the prompt line claiming he played twice."""
        data = await self._classic_team(
            players_with_fixture=self.WITH_FIXTURE, players_with_double=self.WITH_DOUBLE,
        )
        assert {p["name"]: p["dgw"] for p in data["my_picks_data"]} == {
            "Mover": True, "Leaver": False,
        }

    async def test_classic_dgw_flag_falls_back_to_the_club(self):
        data = await self._classic_team(players_with_fixture=None, players_with_double=None)
        assert {p["name"]: p["dgw"] for p in data["my_picks_data"]} == {
            "Mover": False, "Leaver": True,
        }

    @staticmethod
    def _draft_client(draft_elements, picks):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value={
            "league": {"name": "Draft League"},
            "standings": [{"league_entry": 10, "event_total": 0, "total": 0, "rank": 1}],
            "league_entries": [
                {"id": 10, "entry_id": 1, "player_first_name": "A", "player_last_name": "B"},
            ],
        })
        client.get_bootstrap_static = AsyncMock(return_value={"elements": draft_elements})
        client.get_entry_picks = AsyncMock(return_value={"picks": picks, "subs": []})
        client.get_league_transactions = AsyncMock(return_value={"transactions": []})
        return client

    async def _draft(self, **kwargs):
        draft_elements = [
            make_draft_player(id=401, code=1401, web_name="Mover", team=3, element_type=3),
            make_draft_player(id=402, code=1402, web_name="Leaver", team=19, element_type=3),
            # No code and no name the main game knows, so nothing to look up
            # in the gameweek's live data.
            make_draft_player(id=901, code=999999, web_name="Mystery", team=3, element_type=3),
        ]
        picks = [{"element": e["id"], "position": i} for i, e in enumerate(draft_elements, start=1)]
        client = self._draft_client(draft_elements, picks)
        with patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=client):
            return await _review_draft(
                MagicMock(), 1, 1, gw=15, api_current_gw_id=15,
                players=self._players(), player_map={p.id: p for p in self._players()},
                teams=self.TEAMS, live_stats=self.LIVE_STATS,
                bgw_team_ids=frozenset({3}), dgw_team_ids=frozenset({19}), **kwargs,
            )

    async def test_draft_bgw_flag_follows_the_gameweeks_own_record(self):
        data = await self._draft(players_with_fixture=self.WITH_FIXTURE)
        assert {p["name"]: p["bgw"] for p in data["draft_squad_points_data"]} == {
            "Mover": False, "Leaver": True, "Mystery": True,
        }

    async def test_an_unmatched_draft_player_still_reads_his_club(self):
        """`Mystery` has no main-game id, so the gameweek has nothing to say
        about him and his club answers -- in both directions."""
        data = await self._draft(players_with_fixture=frozenset({401, 402, 901}))
        flags = {p["name"]: p["bgw"] for p in data["draft_squad_points_data"]}
        assert flags["Mystery"] is True  # club 3 blanked; the 901 above is a draft id
        assert flags["Mover"] is False

    async def test_draft_bgw_flag_falls_back_to_the_club(self):
        data = await self._draft(players_with_fixture=None)
        assert {p["name"]: p["bgw"] for p in data["draft_squad_points_data"]} == {
            "Mover": True, "Leaver": False, "Mystery": True,
        }

    async def test_draft_dgw_flag_follows_the_gameweeks_own_record(self):
        data = await self._draft(
            players_with_fixture=self.WITH_FIXTURE, players_with_double=self.WITH_DOUBLE,
        )
        assert {p["name"]: p["dgw"] for p in data["draft_squad_points_data"]} == {
            "Mover": True, "Leaver": False, "Mystery": False,
        }

    async def test_draft_dgw_flag_falls_back_to_the_club(self):
        data = await self._draft(players_with_fixture=None, players_with_double=None)
        assert {p["name"]: p["dgw"] for p in data["draft_squad_points_data"]} == {
            "Mover": False, "Leaver": True, "Mystery": False,
        }


class TestReviewThreadsTheGameweeksFixtureSet:
    """The fix is only worth anything if the command builds the set and hands
    it to all three sites, so pin the wiring rather than trusting the call."""

    def test_every_section_receives_the_resolved_set(self, monkeypatch):
        from click.testing import CliRunner

        from fpl_cli.cli import review as review_module
        from tests.conftest import make_fixture

        # Club 3 blanked GW15 and club 19 doubled, so only club 19's players
        # have `explain` entries -- two of them, which is what the pair of
        # `resolve_players_with_*` calls read.
        live_data = {"elements": [
            {"id": 401, "stats": {"total_points": 0}, "explain": [{"fixture": 7}, {"fixture": 8}]},
            {"id": 402, "stats": {"total_points": 0}, "explain": []},
        ]}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_gameweeks = AsyncMock(return_value=[{"id": 15, "finished": True}])
        client.get_current_gameweek = AsyncMock(return_value={"id": 20, "finished": False})
        client.get_players = AsyncMock(return_value=[
            make_player(id=401, team_id=19), make_player(id=402, team_id=3),
        ])
        client.get_teams = AsyncMock(return_value=[
            make_team(id=3, short_name="BLA", name="Blanked FC"),
            make_team(id=19, short_name="MCI", name="Man City"),
        ])
        client.get_gameweek_live = AsyncMock(return_value=live_data)
        client.get_fixtures = AsyncMock(return_value=[
            make_fixture(id=7, gameweek=15, home_team_id=19, away_team_id=1, finished=True, started=True),
            make_fixture(id=8, gameweek=15, home_team_id=2, away_team_id=19, finished=True, started=True),
        ])
        monkeypatch.setattr("fpl_cli.api.fpl.FPLClient", MagicMock(return_value=client))

        seen: dict[str, object] = {}

        def _spy(key, result):
            async def _call(*args, players_with_fixture=None, players_with_double=None, **kwargs):
                seen[key] = (players_with_fixture, players_with_double)
                return result
            return _call

        monkeypatch.setattr(review_module, "_review_classic_team", _spy("classic", {
            "my_entry_summary": None, "active_chip": None,
            "team_points_data": [], "my_picks_data": [],
        }))
        monkeypatch.setattr(review_module, "_review_global_stats", _spy("global", {}))
        monkeypatch.setattr(review_module, "_review_draft", _spy("draft", {
            "draft_squad_points_data": [], "draft_transactions_data": [],
            "draft_league_data": None, "draft_automatic_subs": [],
            "draft_player_map": {},
        }))
        monkeypatch.setattr(review_module, "_review_fixtures", AsyncMock(return_value=[]))
        monkeypatch.setattr(review_module, "_review_league_table", AsyncMock(return_value=[]))

        result = CliRunner().invoke(review_module.review_command, ["--gameweek", "15"])

        assert result.exit_code == 0, result.output
        # 401's club took the pitch twice that gameweek; 402's not at all.
        # Global stats reads blanks only, so it is handed no double set.
        assert seen == {
            "classic": (frozenset({401}), frozenset({401})),
            "global": (frozenset({401}), None),
            "draft": (frozenset({401}), frozenset({401})),
        }
