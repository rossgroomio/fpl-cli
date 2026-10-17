"""Signal coverage: a feed that reaches part of the pool is named, not silent (#438)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fpl_cli.models.player import PlayerPosition
from fpl_cli.services.player_prior import CUTOFF_GW, PlayerPrior
from fpl_cli.services.scoring import prepare_scoring_data
from fpl_cli.services.scoring.coverage import (
    ADJUSTED_NPXG_COVERAGE_LOW,
    CONSISTENCY_COVERAGE_LOW,
    FIXTURE_RATINGS_COVERAGE_LOW,
    GW_HISTORY_COVERAGE_LOW,
    HISTORY_PRIOR_COVERAGE_LOW,
    MIN_ELIGIBLE_PLAYERS,
    UNDERSTAT_COVERAGE_FLOOR,
    UNDERSTAT_COVERAGE_LOW,
    UNDERSTAT_EARLY_COVERAGE_FLOOR,
    SignalCoverage,
    adjusted_npxg_coverage,
    consistency_coverage,
    coverage_warnings,
    fixture_ratings_coverage,
    gw_history_coverage,
    history_prior_coverage,
    understat_coverage,
)
from fpl_cli.services.team_ratings import TeamRating, TeamRatingsService
from tests.conftest import make_fixture, make_player, make_team


def _pool(n: int = 30, **kwargs):
    """*n* players with minutes, ids 1..n."""
    return [make_player(id=i, web_name=f"P{i}", team_id=1, minutes=900, **kwargs) for i in range(1, n + 1)]


def _coverage(covered: int, eligible: int, floor: float = 0.8, min_eligible: int = 1) -> SignalCoverage:
    return SignalCoverage(
        code="x_low", label="X", covered=covered, eligible=eligible,
        eligible_label="players", floor=floor, consequence="the rest score without it",
        cause="run `fpl doctor --providers`", min_eligible=min_eligible,
    )


class TestSignalCoverage:
    def test_share_is_full_when_nobody_was_eligible(self):
        assert _coverage(0, 0).share == 1.0
        assert not _coverage(0, 0).low

    def test_under_the_floor_is_low_and_warns_with_the_numbers(self):
        c = _coverage(3, 10)
        assert c.low
        warning = c.warning()
        assert warning is not None
        assert warning["code"] == "x_low"
        assert "3 of 10 players (30%; at least 80% expected)" in warning["message"]
        assert "the rest score without it" in warning["message"]
        assert warning["message"].endswith("run `fpl doctor --providers`.")

    def test_at_the_floor_is_not_low(self):
        assert not _coverage(8, 10).low
        assert _coverage(8, 10).warning() is None

    def test_a_small_sample_gives_no_verdict(self):
        """Twenty players and two misses is a filter, not a feed failure."""
        assert not _coverage(0, MIN_ELIGIBLE_PLAYERS - 1, min_eligible=MIN_ELIGIBLE_PLAYERS).low
        assert _coverage(0, MIN_ELIGIBLE_PLAYERS, min_eligible=MIN_ELIGIBLE_PLAYERS).low

    def test_coverage_warnings_keeps_only_the_low_ones_in_order(self):
        warnings = coverage_warnings([_coverage(0, 10), _coverage(10, 10), _coverage(1, 10)])
        assert [w["code"] for w in warnings] == ["x_low", "x_low"]
        assert "0 of 10" in warnings[0]["message"]
        assert "1 of 10" in warnings[1]["message"]


class TestUnderstatCoverage:
    def test_counts_players_with_minutes_the_lookup_gives_an_npxg_rate(self):
        players = [*_pool(3), make_player(id=4, minutes=0)]
        lookup = {
            1: {"npxG_per_90": 0.3},
            2: {"npxG_per_90": None, "xGChain_per_90": 0.4},  # payload lost npxG
            4: {"npxG_per_90": 0.5},  # no minutes: not eligible, not counted
        }
        c = understat_coverage(players, lookup, next_gw_id=10)
        assert (c.covered, c.eligible) == (1, 3)
        assert c.code == UNDERSTAT_COVERAGE_LOW

    def test_floor_is_lower_until_the_clubs_have_settled(self):
        assert understat_coverage([], {}, next_gw_id=3).floor == UNDERSTAT_EARLY_COVERAGE_FLOOR
        assert understat_coverage([], {}, next_gw_id=4).floor == UNDERSTAT_COVERAGE_FLOOR

    def test_a_failed_fetch_reads_as_nobody_reached(self):
        c = understat_coverage(_pool(), None, next_gw_id=10)
        assert c.covered == 0
        assert c.low


class TestMatchDataCoverage:
    def test_adjusted_npxg_is_judged_over_players_with_enough_starts(self):
        players = [make_player(id=i, minutes=900, starts=s) for i, s in enumerate((4, 4, 3, 7), 1)]
        c = adjusted_npxg_coverage(players, {1: 0.4, 3: 0.2})
        assert (c.covered, c.eligible) == (1, 3)
        assert c.code == ADJUSTED_NPXG_COVERAGE_LOW
        assert "4+ starts" in c.eligible_label

    def test_consistency_is_judged_over_players_with_six_starts(self):
        players = [make_player(id=i, minutes=900, starts=s) for i, s in enumerate((6, 5, 9), 1)]
        c = consistency_coverage(players, {1: object()})
        assert (c.covered, c.eligible) == (1, 2)
        assert c.code == CONSISTENCY_COVERAGE_LOW

    def test_an_empty_lookup_is_nobody_reached(self):
        players = [make_player(id=i, minutes=900, starts=6) for i in range(1, 31)]
        assert adjusted_npxg_coverage(players, None).low
        assert consistency_coverage(players, {}).low

    def test_nobody_eligible_before_anyone_has_the_starts(self):
        # GW2: no verdict rather than a false alarm.
        c = adjusted_npxg_coverage([make_player(id=1, starts=1)], None)
        assert c.eligible == 0 and not c.low


class TestHistoryPriorCoverage:
    def test_counts_history_sourced_priors_over_the_pool(self):
        players = _pool(4)
        priors = {
            1: PlayerPrior(0.7, 0.5, "history"),
            2: PlayerPrior(0.2, 0.3, "price"),
            3: PlayerPrior(0.8, 0.5, "history"),
        }
        c = history_prior_coverage(players, priors, next_gw_id=2)
        assert (c.covered, c.eligible) == (2, 4)
        assert c.code == HISTORY_PRIOR_COVERAGE_LOW

    def test_nobody_eligible_from_the_cutoff(self):
        c = history_prior_coverage(_pool(), None, next_gw_id=CUTOFF_GW)
        assert c.eligible == 0 and not c.low

    def test_no_priors_before_the_cutoff_is_nobody_reached(self):
        c = history_prior_coverage(_pool(), None, next_gw_id=5)
        assert c.covered == 0 and c.low
        assert f"until GW{CUTOFF_GW}" in c.consequence


class TestGwHistoryCoverage:
    def test_an_empty_history_counts_as_missing(self):
        players = [*_pool(3), make_player(id=4, minutes=0)]
        histories = {1: [{"round": 1}], 2: [], 4: [{"round": 1}]}
        c = gw_history_coverage(players, histories)
        assert (c.covered, c.eligible) == (1, 3)
        assert c.code == GW_HISTORY_COVERAGE_LOW

    def test_one_failure_in_a_full_pool_stays_under_the_radar(self):
        players = _pool(40)
        histories = {p.id: [{"round": 1}] for p in players if p.id != 40}
        assert not gw_history_coverage(players, histories).low

    def test_a_run_of_failures_is_low(self):
        players = _pool(40)
        histories = {p.id: [{"round": 1}] for p in players if p.id <= 30}
        c = gw_history_coverage(players, histories)
        assert c.low
        assert "element-summary" in c.cause


def _ratings_service(rated: dict[str, TeamRating] | None, staleness: str | None = None):
    svc = TeamRatingsService.__new__(TeamRatingsService)
    svc._ratings = dict(rated or {})
    svc._loaded = True
    svc._metadata = None
    svc.get_staleness_warning = MagicMock(return_value=staleness)  # type: ignore[method-assign]
    return svc


class TestFixtureRatingsCoverage:
    TEAMS = {1: make_team(id=1, short_name="ARS"), 2: make_team(id=2, short_name="SHU"), 3: make_team(id=3, short_name="MCI")}
    RATING = TeamRating(atk_home=3, atk_away=3, def_home=3, def_away=3)

    def test_a_fixture_with_an_unrated_side_is_low(self):
        fixtures = [
            make_fixture(id=1, gameweek=5, home_team_id=1, away_team_id=2),
            make_fixture(id=2, gameweek=5, home_team_id=1, away_team_id=3),
        ]
        svc = _ratings_service({"ARS": self.RATING, "MCI": self.RATING}, staleness="⚠️ three clubs unrated - run `fpl ratings update`.")
        c = fixture_ratings_coverage(fixtures, self.TEAMS, svc)
        assert (c.covered, c.eligible) == (1, 2)
        assert c.low and c.code == FIXTURE_RATINGS_COVERAGE_LOW
        message = c.warning()["message"]  # type: ignore[index]
        assert "1 of 2 next-gameweek fixtures" in message
        assert "neutral 4.0" in message
        # The staleness diagnosis is the remedy, without its glyph or a doubled stop.
        assert message.endswith("three clubs unrated - run `fpl ratings update`.")
        assert ".." not in message

    def test_every_side_rated_is_quiet_and_never_asks_for_the_diagnosis(self):
        fixtures = [make_fixture(id=1, gameweek=5, home_team_id=1, away_team_id=3)]
        svc = _ratings_service({"ARS": self.RATING, "MCI": self.RATING})
        c = fixture_ratings_coverage(fixtures, self.TEAMS, svc)
        assert not c.low
        svc.get_staleness_warning.assert_not_called()  # type: ignore[attr-defined]

    def test_no_ratings_at_all_falls_back_to_the_plain_remedy(self):
        fixtures = [make_fixture(id=1, gameweek=5, home_team_id=1, away_team_id=2)]
        c = fixture_ratings_coverage(fixtures, self.TEAMS, _ratings_service({}))
        assert c.low
        assert c.cause == "run `fpl ratings update`"

    def test_no_fixtures_is_nothing_to_judge(self):
        c = fixture_ratings_coverage([], self.TEAMS, _ratings_service({}))
        assert c.eligible == 0 and not c.low


# ---------------------------------------------------------------------------
# prepare_scoring_data carries the entries
# ---------------------------------------------------------------------------


def _client(players):
    client = AsyncMock()
    client.get_teams.return_value = [make_team(id=1, short_name="ARS"), make_team(id=2, short_name="SHU")]
    client.get_fixtures.return_value = [make_fixture(id=1, gameweek=20, home_team_id=1, away_team_id=2)]
    client.get_next_gameweek.return_value = {"id": 20}
    client.get_players.return_value = players
    client.get_player_detail.return_value = {"history": [{"round": 19, "minutes": 90}]}
    return client


def _codes(data) -> list[str]:
    return [w["code"] for w in data.warnings]


class TestPrepareScoringDataCoverage:
    """The bundle measures only what it was asked to build, and says what fell short."""

    @pytest.fixture(autouse=True)
    def _seams(self, stub_scoring_network_seams):
        pass

    async def test_no_ratings_file_names_the_fixtures_scoring_neutral(self):
        data = await prepare_scoring_data(_client([]))
        assert _codes(data) == [FIXTURE_RATINGS_COVERAGE_LOW]
        assert "0 of 1 next-gameweek fixtures" in data.warnings[0]["message"]

    async def test_signals_not_requested_are_not_measured(self):
        data = await prepare_scoring_data(_client(_pool()), include_players=True)
        assert UNDERSTAT_COVERAGE_LOW not in _codes(data)
        assert GW_HISTORY_COVERAGE_LOW not in _codes(data)
        assert ADJUSTED_NPXG_COVERAGE_LOW not in _codes(data)

    async def test_an_empty_understat_lookup_over_a_full_pool_is_named(self):
        # The seam stub answers {} for the lookup: a 403 from Understat looks exactly like this.
        data = await prepare_scoring_data(
            _client(_pool()), include_players=True, include_understat=True,
        )
        assert UNDERSTAT_COVERAGE_LOW in _codes(data)
        message = next(w["message"] for w in data.warnings if w["code"] == UNDERSTAT_COVERAGE_LOW)
        assert "0 of 30 players with minutes" in message

    async def test_a_full_understat_lookup_is_quiet(self):
        lookup = {i: {"npxG_per_90": 0.3} for i in range(1, 31)}
        with patch(
            "fpl_cli.services.scoring.data_prep.build_understat_by_player_id",
            new_callable=AsyncMock, return_value=lookup,
        ):
            data = await prepare_scoring_data(
                _client(_pool()), include_players=True, include_understat=True,
            )
        assert UNDERSTAT_COVERAGE_LOW not in _codes(data)

    async def test_throttled_history_fetches_are_counted(self):
        client = _client(_pool(40))

        async def detail(pid: int):
            if pid > 30:
                raise ConnectionError("429")
            return {"history": [{"round": 19, "minutes": 90}]}

        client.get_player_detail = AsyncMock(side_effect=detail)
        data = await prepare_scoring_data(client, include_history=True)
        assert GW_HISTORY_COVERAGE_LOW in _codes(data)
        message = next(w["message"] for w in data.warnings if w["code"] == GW_HISTORY_COVERAGE_LOW)
        assert "30 of 40 players with minutes" in message

    async def test_empty_match_data_over_players_with_starts_is_named(self):
        players = [make_player(id=i, team_id=1, minutes=900, starts=8) for i in range(1, 31)]
        data = await prepare_scoring_data(
            _client(players), include_players=True, include_match_data=True,
        )
        assert ADJUSTED_NPXG_COVERAGE_LOW in _codes(data)
        assert CONSISTENCY_COVERAGE_LOW in _codes(data)

    async def test_missing_priors_before_the_cutoff_are_named(self):
        client = _client(_pool())
        client.get_next_gameweek.return_value = {"id": 4}
        with patch("fpl_cli.services.player_prior.load_or_generate_player_priors", new_callable=AsyncMock, return_value=None):
            data = await prepare_scoring_data(client, include_players=True, include_prior=True)
        assert HISTORY_PRIOR_COVERAGE_LOW in _codes(data)

    async def test_warnings_default_to_empty_on_a_hand_built_bundle(self):
        from fpl_cli.services.scoring import ScoringContext, ScoringData

        bundle = ScoringData(
            teams=[], team_map={}, all_fixtures=[], next_gw_fixtures=[], next_gw_id=1,
            next_gw=None,
            scoring_ctx=ScoringContext(team_map={}, team_fixture_map={}, ratings_service=MagicMock()),
            ratings_service=MagicMock(),
        )
        assert bundle.warnings == []


@pytest.mark.parametrize("position", [PlayerPosition.GOALKEEPER, PlayerPosition.FORWARD])
def test_eligibility_does_not_depend_on_position(position):
    players = [make_player(id=i, minutes=900, starts=6, position=position) for i in range(1, 4)]
    assert consistency_coverage(players, {}).eligible == 3
