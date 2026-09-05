"""Tests for `fpl allocate` command."""

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli import main
from fpl_cli.models.player import PlayerPosition
from fpl_cli.services.squad_allocator import ScoredPlayer, SquadResult
from tests.conftest import make_player


def _make_squad_result(*, status="optimal", budget=100.0, formation=(4, 4, 2)):
    """Build a SquadResult with 15 players."""
    positions = (
        [("GK", PlayerPosition.GOALKEEPER)] * 2
        + [("DEF", PlayerPosition.DEFENDER)] * 5
        + [("MID", PlayerPosition.MIDFIELDER)] * 5
        + [("FWD", PlayerPosition.FORWARD)] * 3
    )
    players = []
    for i, (pos_name, pos_enum) in enumerate(positions):
        p = make_player(
            id=i + 1, web_name=f"Player{i+1}",
            team_id=(i % 5) + 1, position=pos_enum,
            now_cost=60 + i * 2,
        )
        players.append(ScoredPlayer(
            player=p, raw_quality=15.0 - i * 0.5,
            position=pos_name,
        ))

    starter_ids = {sp.player.id for sp in players[:11]}
    budget_used = sum(sp.player.price for sp in players)

    return SquadResult(
        selected_players=players,
        starter_ids=starter_ids,
        budget_used=round(budget_used, 1),
        budget_remaining=round(budget - budget_used, 1),
        objective_value=150.0,
        status=status,
        formation=formation,
        captain_schedule={0: 1, 1: 1, 2: 3},
    )


def _make_scoring_data(next_gw_id=20):
    sd = MagicMock()
    sd.next_gw_id = next_gw_id
    sd.team_map = {
        i: MagicMock(short_name=f"T{i:02d}")
        for i in range(1, 21)
    }
    return sd


def _run_allocate(
    squad_result, args=None, scoring_data=None, *, return_mocks=False, during_scoring=None,
):
    """Run fpl allocate with mocked service layer.

    When *return_mocks* is True, returns ``(result, mocks_dict)`` so callers
    can inspect call arguments on the solve_squad mock etc.

    *during_scoring* runs inside the stubbed `prepare_scoring_data`, where the
    real one does its Understat enrichment. A test that needs a side effect of
    that enrichment has to produce it there rather than before `invoke`: the
    CLI group resets the join-drop record on entry, so anything staged outside
    the run is gone by the time the command reads it.
    """
    if scoring_data is None:
        scoring_data = _make_scoring_data()

    scored_players = squad_result.selected_players if squad_result.status == "optimal" else []
    coefficients = {sp.player.id: [sp.raw_quality] for sp in scored_players}

    mock_fpl = MagicMock()
    mock_fpl.__aenter__ = AsyncMock(return_value=mock_fpl)
    mock_fpl.__aexit__ = AsyncMock(return_value=False)

    runner = CliRunner()
    settings = {"fpl": {"format": "classic"}, "custom_analysis": True}
    with ExitStack() as stack:
        stack.enter_context(patch("fpl_cli.cli.load_settings", return_value=settings))
        stack.enter_context(patch("fpl_cli.cli._context.load_settings", return_value=settings))
        stack.enter_context(patch("fpl_cli.api.fpl.FPLClient", return_value=mock_fpl))
        async def _prepare_scoring_data(*_args, **_kwargs):
            if during_scoring is not None:
                during_scoring()
            return scoring_data

        stack.enter_context(patch(
            "fpl_cli.services.scoring.prepare_scoring_data",
            new=AsyncMock(side_effect=_prepare_scoring_data),
        ))
        mock_score = stack.enter_context(patch(
            "fpl_cli.services.squad_allocator.score_all_players",
            return_value=scored_players,
        ))
        mock_score_sgw = stack.enter_context(patch(
            "fpl_cli.services.squad_allocator.score_all_players_sgw",
            return_value=scored_players,
        ))
        mock_coefficients = stack.enter_context(patch(
            "fpl_cli.services.squad_allocator.compute_fixture_coefficients",
            return_value=coefficients,
        ))
        mock_solve = stack.enter_context(patch(
            "fpl_cli.services.squad_allocator.solve_squad",
            return_value=squad_result,
        ))

        cli_result = runner.invoke(main, ["allocate"] + (args or []))
        if return_mocks:
            return cli_result, {
                "solve_squad": mock_solve,
                "score_all_players": mock_score,
                "score_all_players_sgw": mock_score_sgw,
                "compute_fixture_coefficients": mock_coefficients,
            }
        return cli_result


class TestAllocateCommand:
    def test_json_output_valid(self):
        """--format json returns valid JSON with 15 players."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["command"] == "allocate"
        assert len(data["data"]) == 15

    def test_json_contains_expected_fields(self):
        """JSON output contains expected player fields."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json"])
        data = json.loads(result.output)
        player = data["data"][0]
        for field in ("id", "web_name", "team", "position", "price", "effective_price", "quality_score", "raw_quality", "role", "captain_gws"):
            assert field in player, f"Missing field: {field}"

    def test_json_metadata(self):
        """JSON metadata includes horizon, budget, formation, solver_status."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json"])
        data = json.loads(result.output)
        meta = data["metadata"]
        for field in ("budget", "budget_used", "budget_remaining", "horizon", "formation", "solver_status"):
            assert field in meta, f"Missing metadata: {field}"

    def test_budget_metadata_consistent(self):
        """budget_used + budget_remaining ~= budget."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json"])
        data = json.loads(result.output)
        meta = data["metadata"]
        assert abs(meta["budget_used"] + meta["budget_remaining"] - meta["budget"]) < 0.2

    def test_custom_budget(self):
        """--budget 95.0 passes budget to metadata."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json", "--budget", "95.0"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["metadata"]["budget"] == 95.0

    def test_custom_horizon(self):
        """--horizon 8 passes through to metadata."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json", "--horizon", "8"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["metadata"]["horizon"] == 8

    def test_infeasible_json_error(self):
        """Infeasible result returns non-zero exit code."""
        infeasible = SquadResult(
            selected_players=[], starter_ids=set(),
            budget_used=0.0, budget_remaining=50.0,
            objective_value=0.0, status="infeasible",
            formation=(0, 0, 0), captain_schedule={},
        )
        result = _run_allocate(infeasible, args=["--format", "json"])
        assert result.exit_code == 1

    def test_infeasible_table_error(self):
        """Infeasible result in table mode exits 1, reporting on stderr (#162)."""
        infeasible = SquadResult(
            selected_players=[], starter_ids=set(),
            budget_used=0.0, budget_remaining=50.0,
            objective_value=0.0, status="infeasible",
            formation=(0, 0, 0), captain_schedule={},
        )
        result = _run_allocate(infeasible)
        assert result.exit_code == 1
        assert "Infeasible" in result.stderr
        assert result.stdout == ""

    def test_table_output_renders(self):
        """Table output renders without error."""
        result = _run_allocate(_make_squad_result())
        assert result.exit_code == 0
        assert "Player1" in result.output

    def test_quality_score_is_normalised_int(self):
        """quality_score in JSON is int 0-100."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json"])
        data = json.loads(result.output)
        for p in data["data"]:
            assert isinstance(p["quality_score"], int)
            assert 0 <= p["quality_score"] <= 100

    def test_raw_quality_is_float(self):
        """raw_quality in JSON is a float."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json"])
        data = json.loads(result.output)
        for p in data["data"]:
            assert isinstance(p["raw_quality"], float)

    def test_early_season_notice_in_metadata_before_the_cutoff(self):
        """At horizon >= 2 the solver ranks on the prior-blended quality score,
        and the command says so in metadata.warnings, keyed on whether the
        priors actually loaded (PR #208 review).
        """
        blended = _make_scoring_data(next_gw_id=3)
        blended.player_priors = {}
        result = _run_allocate(_make_squad_result(), ["--format", "json"], scoring_data=blended)
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.output)["metadata"]["warnings"]
        assert [w["code"] for w in warnings] == ["early_season_prior_informed"]

        degraded = _make_scoring_data(next_gw_id=3)
        degraded.player_priors = None
        result = _run_allocate(_make_squad_result(), ["--format", "json"], scoring_data=degraded)
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.output)["metadata"]["warnings"]
        assert [w["code"] for w in warnings] == ["early_season_small_sample"]

    def test_understat_join_warning_reaches_metadata_at_every_horizon(self):
        """A club Understat carries no rows for costs the solver that club's
        whole squad its quality score, so the notice cannot be tied to the
        early-season branch the way the prior notice is (#229).

        Recorded the way the scoring pipeline records it — the real gate over a
        payload that names another club — from inside the stubbed
        `prepare_scoring_data`, which is where the enrichment this harness
        skips would otherwise run.
        """
        from fpl_cli.api.understat import match_fpl_to_understat

        def _enrich():
            match_fpl_to_understat(
                "Some Player", "Coventry City",
                [{"name": "Saka", "team": "Arsenal", "position": "M S", "minutes": 900}],
            )

        mid_season = _make_scoring_data(next_gw_id=20)
        mid_season.player_priors = {}
        result = _run_allocate(
            _make_squad_result(), ["--horizon", "1", "--format", "json"],
            scoring_data=mid_season, during_scoring=_enrich,
        )
        assert result.exit_code == 0, result.output
        warnings = json.loads(result.output)["metadata"]["warnings"]
        assert [w["code"] for w in warnings] == ["understat_team_unmatched"]
        assert "Coventry City" in warnings[0]["message"]

    def test_no_understat_join_warning_when_every_club_resolved(self):
        mid_season = _make_scoring_data(next_gw_id=20)
        mid_season.player_priors = {}
        result = _run_allocate(
            _make_squad_result(), ["--format", "json"], scoring_data=mid_season,
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["metadata"]["warnings"] == []

    def test_no_early_season_notice_mid_season_or_at_horizon_1(self):
        mid_season = _make_scoring_data(next_gw_id=20)
        mid_season.player_priors = {}
        result = _run_allocate(_make_squad_result(), ["--format", "json"], scoring_data=mid_season)
        assert json.loads(result.output)["metadata"]["warnings"] == []

        early = _make_scoring_data(next_gw_id=3)
        early.player_priors = {}
        result = _run_allocate(
            _make_squad_result(), ["--horizon", "1", "--format", "json"], scoring_data=early,
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["metadata"]["warnings"] == []

    def test_table_mode_prints_the_early_season_notice(self):
        early = _make_scoring_data(next_gw_id=3)
        early.player_priors = {}
        result = _run_allocate(_make_squad_result(), [], scoring_data=early)
        assert result.exit_code == 0, result.output
        assert "Early-season notice" in result.output

    def test_bench_discount_passes_through(self):
        """--bench-discount builds uniform dict and passes to solve_squad."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-discount", "0.01"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        bd = mocks["solve_squad"].call_args.kwargs["bench_discount"]
        assert bd == {"GK": 0.01, "DEF": 0.01, "MID": 0.01, "FWD": 0.01}

    def test_bench_discount_default_is_none(self):
        """No --bench-discount flag passes None to solve_squad."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        bd = mocks["solve_squad"].call_args.kwargs.get("bench_discount")
        assert bd is None

    def test_bench_discount_in_json_metadata(self):
        """--bench-discount value appears in JSON metadata."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-discount", "0.01"],
        )
        data = json.loads(result.output)
        assert data["metadata"]["bench_discount"] == {"GK": 0.01, "DEF": 0.01, "MID": 0.01, "FWD": 0.01}

    def test_bench_discount_null_in_metadata_when_default(self):
        """bench_discount is null in metadata when not specified."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json"])
        data = json.loads(result.output)
        assert data["metadata"]["bench_discount"] is None

    def test_bench_discount_zero_accepted(self):
        """--bench-discount 0.0 is a valid value and reaches metadata."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-discount", "0.0"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["metadata"]["bench_discount"] == {"GK": 0.0, "DEF": 0.0, "MID": 0.0, "FWD": 0.0}

    def test_bench_discount_over_max_rejected(self):
        """--bench-discount > 1.0 is rejected by FloatRange."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-discount", "1.5"],
        )
        assert result.exit_code != 0

    def test_bench_boost_gw_passes_through(self):
        """--bench-boost-gw computes index and passes to solve_squad."""
        # start_gw=20 (from _make_scoring_data), horizon=6 (default)
        # bench_boost_gw=23 -> bench_boost_gw_idx = 23 - 20 = 3
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-boost-gw", "23"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        bb_idx = mocks["solve_squad"].call_args.kwargs.get("bench_boost_gw_idx")
        assert bb_idx == 3

    def test_bench_boost_gw_default_is_none(self):
        """No --bench-boost-gw passes bench_boost_gw_idx=None to solve_squad."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        bb_idx = mocks["solve_squad"].call_args.kwargs.get("bench_boost_gw_idx")
        assert bb_idx is None

    def test_bench_boost_gw_in_json_metadata(self):
        """--bench-boost-gw appears in JSON metadata."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-boost-gw", "22"],
        )
        data = json.loads(result.output)
        assert data["metadata"]["bench_boost_gw"] == 22

    def test_bench_boost_gw_null_when_default(self):
        """bench_boost_gw is null in metadata when not specified."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json"])
        data = json.loads(result.output)
        assert data["metadata"]["bench_boost_gw"] is None

    def test_bench_boost_gw_with_bench_discount(self):
        """--bench-discount and --bench-boost-gw compose correctly."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-discount", "0.01", "--bench-boost-gw", "21"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        bd = mocks["solve_squad"].call_args.kwargs["bench_discount"]
        bb_idx = mocks["solve_squad"].call_args.kwargs["bench_boost_gw_idx"]
        assert bd == {"GK": 0.01, "DEF": 0.01, "MID": 0.01, "FWD": 0.01}
        assert bb_idx == 1  # 21 - 20

    def test_bench_boost_gw_outside_horizon_json_error(self):
        """--bench-boost-gw outside horizon returns error in JSON mode."""
        # start_gw=20, horizon=6 -> valid range [20, 26)
        result = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-boost-gw", "30"],
        )
        assert result.exit_code == 1

    def test_bench_boost_gw_outside_horizon_table_error(self):
        """--bench-boost-gw outside horizon returns error in table mode."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--bench-boost-gw", "30"],
        )
        assert result.exit_code == 1

    def test_bench_boost_gw_at_start_gw(self):
        """--bench-boost-gw at start_gw (lower bound) is valid."""
        # start_gw=20, so bench_boost_gw=20 -> bb_gw_idx=0
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-boost-gw", "20"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        assert mocks["solve_squad"].call_args.kwargs["bench_boost_gw_idx"] == 0

    def test_bench_boost_gw_at_end_of_horizon(self):
        """--bench-boost-gw at last valid GW (upper bound inclusive) is valid."""
        # start_gw=20, horizon=6 -> effective_end_gw=26, last valid=25
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-boost-gw", "25"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        assert mocks["solve_squad"].call_args.kwargs["bench_boost_gw_idx"] == 5

    def test_bench_boost_gw_before_start_gw_rejected(self):
        """--bench-boost-gw before start_gw is rejected."""
        # start_gw=20, so bench_boost_gw=19 is outside [20, 26)
        result = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-boost-gw", "19"],
        )
        assert result.exit_code == 1

    def test_bench_boost_gw_in_table_output(self):
        """Table output includes BB GW indicator."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--bench-boost-gw", "22"],
        )
        assert result.exit_code == 0
        assert "BB: GW22" in result.output

    def test_bench_boost_gw_zero_rejected(self):
        """--bench-boost-gw 0 is rejected by IntRange(min=1)."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--bench-boost-gw", "0"],
        )
        assert result.exit_code != 0

    def test_horizon1_calls_sgw_scoring(self):
        """--horizon 1 calls score_all_players_sgw, not score_all_players."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--horizon", "1"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        mocks["score_all_players_sgw"].assert_called_once()
        mocks["score_all_players"].assert_not_called()

    def test_horizon1_skips_fixture_coefficients(self):
        """--horizon 1 does NOT call compute_fixture_coefficients."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--horizon", "1"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        mocks["compute_fixture_coefficients"].assert_not_called()

    def test_horizon_default_calls_multi_gw_scoring(self):
        """--horizon 6 (default) calls score_all_players and compute_fixture_coefficients."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        mocks["score_all_players"].assert_called_once()
        mocks["compute_fixture_coefficients"].assert_called_once()
        mocks["score_all_players_sgw"].assert_not_called()

    def test_horizon1_coefficients_are_single_element_lists(self):
        """--horizon 1 produces single-element coefficient lists from raw_quality."""
        sr = _make_squad_result()
        cli_result, mocks = _run_allocate(
            sr, args=["--format", "json", "--horizon", "1"], return_mocks=True,
        )
        assert cli_result.exit_code == 0
        solve_call = mocks["solve_squad"].call_args
        coefficients = solve_call.args[1]
        for coeff_list in coefficients.values():
            assert isinstance(coeff_list, list)
            assert len(coeff_list) == 1

    def test_horizon1_uses_starting_xi_ceiling(self):
        """--horizon 1 JSON output normalises quality using STARTING_XI_CEILING into single_gw_score."""
        from fpl_cli.services.scoring import STARTING_XI_CEILING, normalise_score

        sr = _make_squad_result()
        result = _run_allocate(sr, args=["--format", "json", "--horizon", "1"])
        data = json.loads(result.output)
        first = data["data"][0]
        expected = normalise_score(sr.selected_players[0].raw_quality, STARTING_XI_CEILING)
        assert first["single_gw_score"] == expected
        assert "quality_score" not in first

    def test_horizon_default_uses_per_player_value_ceiling(self):
        """--horizon 6 (default) JSON output normalises each player against a
        position-specific VALUE family ceiling via ``pick_display_ceiling``.
        Matches ``fpl player`` / ``fpl stats --value`` display.

        Asserts across GK, DEF, MID, and FWD so a regression restoring a
        single cross-position ceiling (or silently dropping DEF dispatch)
        fails loudly. The central fix of this PR is DEF_VALUE_CEILING
        replacing MID-anchored VALUE_CEILING * 0.85 — exercising only
        ``selected_players[0]`` (always a GK) would miss that path.
        """
        from fpl_cli.services.scoring import normalise_score, pick_display_ceiling

        sr = _make_squad_result()
        result = _run_allocate(sr, args=["--format", "json"])
        data = json.loads(result.output)

        rows_by_id = {row["id"]: row for row in data["data"]}

        # One assertion per position: GK, DEF, MID, FWD. _make_squad_result
        # places them in insertion order 2 GK / 5 DEF / 5 MID / 3 FWD.
        samples = {
            "GK": sr.selected_players[0],   # GK1
            "DEF": sr.selected_players[2],  # DEF1
            "MID": sr.selected_players[7],  # MID1
            "FWD": sr.selected_players[12], # FWD1
        }
        for pos, sp in samples.items():
            row = rows_by_id[sp.player.id]
            expected = normalise_score(
                sp.raw_quality,
                pick_display_ceiling(sp.position, horizon=6),
            )
            assert row["quality_score"] == expected, (
                f"{pos} quality_score {row['quality_score']} != expected {expected} "
                f"(raw={sp.raw_quality}, position={sp.position})"
            )
            assert "single_gw_score" not in row, f"{pos} unexpectedly contains single_gw_score at horizon >= 2"

    def test_horizon1_renames_field_to_single_gw_score(self):
        """--horizon 1 JSON uses single_gw_score; quality_score absent; raw_quality present."""
        sr = _make_squad_result()
        result = _run_allocate(sr, args=["--format", "json", "--horizon", "1"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["metadata"]["horizon"] == 1
        for p in data["data"]:
            assert "single_gw_score" in p
            assert "quality_score" not in p
            assert "raw_quality" in p
            assert isinstance(p["single_gw_score"], int)
            assert 0 <= p["single_gw_score"] <= 100

    def test_horizon2_retains_quality_score(self):
        """--horizon 2 JSON uses quality_score; single_gw_score absent."""
        sr = _make_squad_result()
        result = _run_allocate(sr, args=["--format", "json", "--horizon", "2"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        for p in data["data"]:
            assert "quality_score" in p
            assert "single_gw_score" not in p

    def test_horizon1_suspended_gw1_coefficient_zero(self):
        """--horizon 1 with suspended GW1 player produces coefficient 0.0."""
        sr = _make_squad_result()
        # Make first player suspended_gw1
        suspended = ScoredPlayer(
            player=sr.selected_players[0].player,
            raw_quality=10.0,
            position="GK",
            suspended_gw1=True,
        )
        sgw_players = [suspended] + list(sr.selected_players[1:])

        scoring_data = _make_scoring_data()
        mock_fpl = MagicMock()
        mock_fpl.__aenter__ = AsyncMock(return_value=mock_fpl)
        mock_fpl.__aexit__ = AsyncMock(return_value=False)

        runner = CliRunner()
        settings = {"fpl": {"format": "classic"}, "custom_analysis": True}
        with ExitStack() as stack:
            stack.enter_context(patch("fpl_cli.cli.load_settings", return_value=settings))
            stack.enter_context(patch("fpl_cli.cli._context.load_settings", return_value=settings))
            stack.enter_context(patch("fpl_cli.api.fpl.FPLClient", return_value=mock_fpl))
            stack.enter_context(patch(
                "fpl_cli.services.scoring.prepare_scoring_data",
                new=AsyncMock(return_value=scoring_data),
            ))
            stack.enter_context(patch(
                "fpl_cli.services.squad_allocator.score_all_players_sgw",
                return_value=sgw_players,
            ))
            mock_solve = stack.enter_context(patch(
                "fpl_cli.services.squad_allocator.solve_squad",
                return_value=sr,
            ))

            cli_result = runner.invoke(main, ["allocate", "--format", "json", "--horizon", "1"])

        assert cli_result.exit_code == 0
        coefficients = mock_solve.call_args.args[1]
        suspended_id = suspended.player.id
        assert coefficients[suspended_id] == [0.0]
        # Non-suspended players should have non-zero coefficients
        non_suspended_id = sr.selected_players[1].player.id
        assert coefficients[non_suspended_id][0] > 0

    def test_horizon1_with_bench_discount(self):
        """--horizon 1 --bench-discount 0.01 composes correctly (Free Hit path)."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--horizon", "1", "--bench-discount", "0.01"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        bd = mocks["solve_squad"].call_args.kwargs["bench_discount"]
        assert bd == {"GK": 0.01, "DEF": 0.01, "MID": 0.01, "FWD": 0.01}
        mocks["score_all_players_sgw"].assert_called_once()
        mocks["compute_fixture_coefficients"].assert_not_called()

    def test_horizon1_table_output_renders(self):
        """--horizon 1 table output renders without error (uses STARTING_XI_CEILING)."""
        result = _run_allocate(_make_squad_result(), args=["--horizon", "1"])
        assert result.exit_code == 0
        assert "Player1" in result.output

    def test_free_transfers_passes_through(self):
        """--free-transfers 3 passes free_transfers=3 to solve_squad."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--free-transfers", "3"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        ft = mocks["solve_squad"].call_args.kwargs.get("free_transfers")
        assert ft == 3

    def test_free_transfers_default_is_one(self):
        """No --free-transfers flag defaults to free_transfers=1."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        ft = mocks["solve_squad"].call_args.kwargs.get("free_transfers")
        assert ft == 1

    def test_free_transfers_in_json_metadata(self):
        """--free-transfers value appears in JSON metadata."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--free-transfers", "3"],
        )
        data = json.loads(result.output)
        assert data["metadata"]["free_transfers"] == 3

    def test_free_transfers_zero_passes_through(self):
        """--free-transfers 0 passes free_transfers=0 (flat weighting)."""
        cli_result, mocks = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--free-transfers", "0"],
            return_mocks=True,
        )
        assert cli_result.exit_code == 0
        ft = mocks["solve_squad"].call_args.kwargs.get("free_transfers")
        assert ft == 0

    def test_free_transfers_over_max_rejected(self):
        """--free-transfers 6 is rejected by IntRange(max=5)."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--free-transfers", "6"],
        )
        assert result.exit_code != 0

    def test_free_transfers_negative_rejected(self):
        """--free-transfers -1 is rejected by IntRange(min=0)."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--format", "json", "--free-transfers", "-1"],
        )
        assert result.exit_code != 0

    def test_free_transfers_in_table_summary_non_default(self):
        """Table output includes FTs indicator when non-default."""
        result = _run_allocate(
            _make_squad_result(),
            args=["--free-transfers", "3"],
        )
        assert result.exit_code == 0
        assert "FTs: 3" in result.output

    def test_free_transfers_not_in_table_summary_when_default(self):
        """Table output omits FTs indicator when default (1)."""
        result = _run_allocate(_make_squad_result())
        assert result.exit_code == 0
        assert "FTs:" not in result.output

    def test_effective_price_equals_price_without_overrides(self):
        """Without sell-prices, effective_price == price for all players."""
        result = _run_allocate(_make_squad_result(), args=["--format", "json"])
        data = json.loads(result.output)
        for p in data["data"]:
            assert p["effective_price"] == p["price"]

    def test_effective_price_reflects_sell_price_for_owned(self):
        """With sell-price overrides, owned player's effective_price = sell price."""
        sr = _make_squad_result()
        owned_player = sr.selected_players[0]
        market_price = owned_player.player.price
        sell_price = market_price - 0.4
        saving = round(market_price - sell_price, 1)

        sr_with_owned = SquadResult(
            selected_players=sr.selected_players,
            starter_ids=sr.starter_ids,
            budget_used=round(sr.budget_used - saving, 1),
            budget_remaining=round(sr.budget_remaining + saving, 1),
            objective_value=sr.objective_value,
            status=sr.status,
            formation=sr.formation,
            captain_schedule=sr.captain_schedule,
            owned_ids=frozenset({owned_player.player.id}),
            player_savings={owned_player.player.id: saving},
        )

        result = _run_allocate(sr_with_owned, args=["--format", "json"])
        data = json.loads(result.output)

        for p in data["data"]:
            if p["id"] == owned_player.player.id:
                assert p["effective_price"] == sell_price
                assert p["owned"] is True
                assert p["saving"] == saving
            else:
                assert p["effective_price"] == p["price"]

    def test_effective_price_sum_equals_budget_used(self):
        """Sum of effective_price across all players equals metadata.budget_used."""
        sr = _make_squad_result()
        # Give two players sell-price savings
        p0, p1 = sr.selected_players[0], sr.selected_players[2]
        savings = {p0.player.id: 0.3, p1.player.id: 0.6}
        total_saving = sum(savings.values())

        sr_with_owned = SquadResult(
            selected_players=sr.selected_players,
            starter_ids=sr.starter_ids,
            budget_used=round(sr.budget_used - total_saving, 1),
            budget_remaining=round(sr.budget_remaining + total_saving, 1),
            objective_value=sr.objective_value,
            status=sr.status,
            formation=sr.formation,
            captain_schedule=sr.captain_schedule,
            owned_ids=frozenset(savings.keys()),
            player_savings=savings,
        )

        result = _run_allocate(sr_with_owned, args=["--format", "json"])
        data = json.loads(result.output)

        effective_total = sum(p["effective_price"] for p in data["data"])
        assert abs(effective_total - data["metadata"]["budget_used"]) < 0.2
