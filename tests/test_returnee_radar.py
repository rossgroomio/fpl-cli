"""Tests for the injury returnee radar's news signal parser."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from fpl_cli.api.historical_types import PlayerProfile, SeasonHistory
from fpl_cli.cli._context import load_settings
from fpl_cli.models.player import PlayerPosition, PlayerStatus
from fpl_cli.season import TOTAL_GAMEWEEKS, get_season_year, season_label
from fpl_cli.services import returnee_radar
from fpl_cli.services.player_prior import MIN_MINUTES, PlayerPrior
from fpl_cli.services.returnee_radar import (
    QUALITY_BASIS_PRICE,
    QUALITY_BASIS_PRIOR,
    QUALITY_BASIS_SEASON,
    SOURCE_FPL_NEWS,
    RadarConfig,
    ReturnSignal,
    build_radar,
    build_return_signal,
    enrichment_from_response,
    gameweek_for_date,
    news_age_days,
    radar_config_from_settings,
    resolve_return_date,
)
from tests.conftest import make_player

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A 2026-27 shaped schedule with the live three-week GW5 -> GW6 break, so a
# date in the gap can only land on GW6 by walking deadlines: a fixed
# weeks-per-gameweek assumption anchored on GW1 would put 27 Sep in GW7.
GAMEWEEKS: list[dict[str, Any]] = [
    {"id": 1, "deadline_time": "2026-08-14T17:30:00Z"},
    {"id": 2, "deadline_time": "2026-08-22T10:00:00Z"},
    {"id": 3, "deadline_time": "2026-08-29T10:00:00Z"},
    {"id": 4, "deadline_time": "2026-09-12T10:00:00Z"},
    {"id": 5, "deadline_time": "2026-09-19T10:00:00Z"},
    {"id": 6, "deadline_time": "2026-10-10T10:00:00Z"},
    {"id": 7, "deadline_time": "2026-10-17T10:00:00Z"},
]

SEASON_YEAR = 2026

# Before GW1's deadline: nothing has lapsed yet.
PRESEASON_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
# After GW5's deadline: anything due before 19 Sep has lapsed.
POST_GW5_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _signal(
    news: str = "",
    *,
    news_added: str | None = None,
    chance: int | None = None,
    status: PlayerStatus = PlayerStatus.INJURED,
    gameweeks: list[dict[str, Any]] | None = None,
    now: datetime | None = PRESEASON_NOW,
    season_year: int | None = SEASON_YEAR,
) -> ReturnSignal:
    player = make_player(
        status=status,
        news=news,
        news_added=news_added,
        chance_of_playing_next_round=chance,
    )
    return build_return_signal(
        player,
        gameweeks=GAMEWEEKS if gameweeks is None else gameweeks,
        now=now,
        season_year=season_year,
    )


# ---------------------------------------------------------------------------
# Parsing the measured grammar
# ---------------------------------------------------------------------------


def test_expected_back_shape_parses_date_and_source():
    signal = _signal("Calf injury - Expected back 5 Sep")

    assert signal.return_date == date(2026, 9, 5)
    assert signal.source == SOURCE_FPL_NEWS
    assert signal.has_return_date is True
    assert signal.lapsed is False


def test_suspended_until_shape_parses_date():
    signal = _signal("Suspended until 19 Sep", status=PlayerStatus.SUSPENDED)

    assert signal.return_date == date(2026, 9, 19)
    assert signal.source == SOURCE_FPL_NEWS
    assert signal.has_return_date is True


def test_unknown_return_date_shape_yields_date_unknown_and_keeps_news():
    text = "Knee injury - Unknown return date"
    signal = _signal(text)

    assert signal.return_date is None
    assert signal.return_gameweek is None
    assert signal.source is None
    assert signal.has_return_date is False
    assert signal.news == text


def test_chance_of_playing_shape_does_not_read_percentage_as_a_day():
    signal = _signal("Thigh injury - 75% chance of playing", chance=75)

    assert signal.return_date is None
    assert signal.source is None
    assert signal.chance_of_playing == 75


def test_empty_news_yields_date_unknown_without_raising():
    signal = _signal("")

    assert signal.return_date is None
    assert signal.has_return_date is False
    assert signal.news == ""


def test_unrecognised_shape_yields_date_unknown():
    signal = _signal("Has joined Getafe permanently")

    assert signal.return_date is None
    assert signal.source is None
    assert signal.news == "Has joined Getafe permanently"


def test_impossible_calendar_date_yields_date_unknown():
    signal = _signal("Ankle injury - Expected back 31 Feb")

    assert signal.return_date is None
    assert signal.source is None


def test_unknown_month_token_yields_date_unknown():
    signal = _signal("Ankle injury - Expected back 5 Smarch")

    assert signal.return_date is None


def test_player_shaped_mapping_is_accepted():
    signal = build_return_signal(
        {"news": "Calf injury - Expected back 5 Sep", "chance_of_playing_next_round": 25},
        gameweeks=GAMEWEEKS,
        now=PRESEASON_NOW,
        season_year=SEASON_YEAR,
    )

    assert signal.return_date == date(2026, 9, 5)
    assert signal.chance_of_playing == 25


# ---------------------------------------------------------------------------
# Season-aware date resolution
# ---------------------------------------------------------------------------


def test_month_after_july_cutover_resolves_to_season_start_year():
    assert resolve_return_date(5, 9, SEASON_YEAR) == date(2026, 9, 5)


def test_month_before_july_cutover_resolves_to_following_calendar_year():
    assert resolve_return_date(14, 2, SEASON_YEAR) == date(2027, 2, 14)


def test_february_date_in_august_start_season_lands_next_year():
    signal = _signal("Cruciate ligament injury - Expected back 14 Feb")

    assert signal.return_date == date(2027, 2, 14)


def test_preseason_month_quoted_mid_season_resolves_to_next_season():
    """A long recovery given as a preseason month must not resolve backwards.

    Read against the season alone, "15 Aug" in a 2026-27 season is 15 Aug 2026 --
    already gone by the following March, so a real return months away would be
    marked lapsed and its date silently thrown away.
    """
    march = datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc)

    assert resolve_return_date(15, 8, SEASON_YEAR, now=march) == date(2027, 8, 15)


def test_a_date_only_recently_missed_still_resolves_into_this_season():
    """The rollforward must not swallow an ordinary lapse.

    A return date FPL actually missed slips by days or weeks; only something
    half a year behind us is next season's.
    """
    assert resolve_return_date(5, 9, SEASON_YEAR, now=POST_GW5_NOW) == date(2026, 9, 5)


def test_season_year_defaults_to_the_current_season():
    signal = _signal("Calf injury - Expected back 5 Sep", season_year=None, gameweeks=[])

    assert signal.return_date == date(get_season_year(), 9, 5)


# ---------------------------------------------------------------------------
# Gameweek mapping
# ---------------------------------------------------------------------------


def test_date_between_deadlines_maps_to_the_later_gameweek():
    assert gameweek_for_date(date(2026, 9, 5), GAMEWEEKS) == 4


def test_date_on_a_deadline_maps_to_that_gameweek():
    assert gameweek_for_date(date(2026, 9, 19), GAMEWEEKS) == 5


def test_date_in_the_three_week_gap_maps_to_gameweek_six():
    assert gameweek_for_date(date(2026, 9, 27), GAMEWEEKS) == 6


def test_date_after_the_final_deadline_maps_to_no_gameweek():
    assert gameweek_for_date(date(2026, 11, 1), GAMEWEEKS) is None


def test_signal_carries_the_mapped_gameweek():
    signal = _signal("Calf injury - Expected back 27 Sep")

    assert signal.return_gameweek == 6


def test_gameweek_mapping_ignores_events_without_a_usable_deadline():
    events: list[dict[str, Any]] = [
        {"id": 1, "deadline_time": None},
        {"id": 2, "deadline_time": "not-a-timestamp"},
        {"id": 3, "deadline_time": "2026-08-29T10:00:00Z"},
    ]

    assert gameweek_for_date(date(2026, 8, 20), events) == 3


def test_no_gameweeks_yields_no_gameweek_but_still_a_date():
    signal = _signal("Calf injury - Expected back 5 Sep", gameweeks=[])

    assert signal.return_date == date(2026, 9, 5)
    assert signal.return_gameweek is None


# ---------------------------------------------------------------------------
# Lapsed returns
# ---------------------------------------------------------------------------


def test_date_before_the_current_deadline_lapses_to_date_unknown():
    signal = _signal("Calf injury - Expected back 5 Sep", now=POST_GW5_NOW)

    assert signal.lapsed is True
    assert signal.has_return_date is False
    assert signal.return_gameweek is None
    # The original date survives for display and week-over-week diffing.
    assert signal.return_date == date(2026, 9, 5)
    assert signal.source == SOURCE_FPL_NEWS


def test_date_after_the_current_deadline_does_not_lapse():
    signal = _signal("Calf injury - Expected back 10 Oct", now=POST_GW5_NOW)

    assert signal.lapsed is False
    assert signal.has_return_date is True
    assert signal.return_gameweek == 6


def test_nothing_lapses_before_the_first_deadline():
    signal = _signal("Calf injury - Expected back 20 Aug", now=PRESEASON_NOW)

    assert signal.lapsed is False
    assert signal.has_return_date is True


def test_date_landing_exactly_on_the_last_passed_deadline_lapses():
    """The boundary `gameweek_for_date` treats as inclusive, treated so here too.

    GW5's deadline is 19 Sep and has passed. A return stated for 19 Sep maps to
    GW5, which is already locked, so calling it upcoming would pin the entry on
    a gameweek nobody can act on and freeze its week-over-week diff.
    """
    signal = _signal("Calf injury - Expected back 19 Sep", now=POST_GW5_NOW)

    assert signal.lapsed is True
    assert signal.has_return_date is False
    assert signal.return_gameweek is None
    assert signal.return_date == date(2026, 9, 19)


def test_date_unknown_signal_is_never_marked_lapsed():
    signal = _signal("Knee injury - Unknown return date", now=POST_GW5_NOW)

    assert signal.lapsed is False
    assert signal.has_return_date is False


# ---------------------------------------------------------------------------
# News age
# ---------------------------------------------------------------------------


def test_news_age_is_computed_from_news_added():
    assert news_age_days("2026-09-15T09:30:00Z", now=POST_GW5_NOW) == 5


def test_news_age_is_none_when_the_field_is_absent():
    assert news_age_days(None, now=POST_GW5_NOW) is None
    assert news_age_days("", now=POST_GW5_NOW) is None


def test_news_age_is_none_when_the_stamp_is_unparseable():
    assert news_age_days("last tuesday", now=POST_GW5_NOW) is None


def test_news_age_of_a_future_stamp_clamps_to_zero():
    assert news_age_days("2026-09-25T09:30:00Z", now=POST_GW5_NOW) == 0


def test_naive_news_stamp_is_read_as_utc():
    assert news_age_days("2026-09-15T09:30:00", now=POST_GW5_NOW) == 5


def test_signal_carries_news_age():
    signal = _signal(
        "Knee injury - Unknown return date",
        news_added="2026-09-15T09:30:00Z",
        now=POST_GW5_NOW,
    )

    assert signal.news_age_days == 5


def test_signal_news_age_is_none_without_a_stamp():
    signal = _signal("Knee injury - Unknown return date", now=POST_GW5_NOW)

    assert signal.news_age_days is None


def test_default_now_does_not_raise():
    player = make_player(status=PlayerStatus.INJURED, news="Knee injury - Unknown return date")

    signal = build_return_signal(player, gameweeks=GAMEWEEKS)

    assert signal.has_return_date is False


# ---------------------------------------------------------------------------
# Whole-grammar sweep (U1 definition of done)
# ---------------------------------------------------------------------------

# Every distinct shape measured on a live bootstrap-static snapshot, with the
# date each one should yield. `None` means date-unknown, which the measurement
# says is the common case rather than the error case.
GRAMMAR_CASES: list[tuple[str, date | None]] = [
    ("Calf injury - Expected back 5 Sep", date(2026, 9, 5)),
    ("Hamstring injury - Expected back 12 Sep", date(2026, 9, 12)),
    ("Knock - Expected back 19 Sep", date(2026, 9, 19)),
    ("Ankle injury - Expected back 10 Oct", date(2026, 10, 10)),
    ("Cruciate ligament injury - Expected back 14 Feb", date(2027, 2, 14)),
    ("Suspended until 19 Sep", date(2026, 9, 19)),
    ("Suspended until 10 Oct", date(2026, 10, 10)),
    ("Thigh injury - 75% chance of playing", None),
    ("Groin injury - 25% chance of playing", None),
    ("Illness - 50% chance of playing", None),
    ("Knee injury - Unknown return date", None),
    ("Foot injury - Unknown return date", None),
    ("Loan - Unknown return date", None),
    ("Has joined Getafe permanently", None),
    ("", None),
]


@pytest.mark.parametrize(("news", "expected"), GRAMMAR_CASES)
def test_measured_grammar_produces_the_expected_date_split(news: str, expected: date | None):
    signal = _signal(news)

    assert signal.return_date == expected
    assert signal.news == news
    assert (signal.source == SOURCE_FPL_NEWS) is (expected is not None)


def test_return_signal_is_frozen():
    signal = _signal("Knee injury - Unknown return date")

    with pytest.raises(AttributeError):
        signal.return_date = date(2026, 9, 5)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Radar configuration
# ---------------------------------------------------------------------------


def test_radar_config_defaults_match_the_shipped_settings():
    config = radar_config_from_settings(load_settings())

    assert config.window_gameweeks == 6
    assert config.stash_window_gameweeks == 2
    assert config.history_watchlist_strength == 0.75
    assert config.history_stash_strength == 0.85
    assert config.price_watchlist_percentile == 0.80
    assert config.price_stash_percentile == 0.90
    assert config.stash_upgrade_margin == 5.0
    assert config.enrich_stale_news_days == 7
    assert config.enrich_max_players == 8
    assert config.enrich_concurrency == 4
    assert config.enrich_query_spacing_seconds == 1.0


def test_radar_config_reads_the_enrichment_pacing_knobs():
    config = radar_config_from_settings({"returnee_radar": {
        "enrich_concurrency": 2, "enrich_query_spacing_seconds": 3,
    }})

    assert config.enrich_concurrency == 2
    assert config.enrich_query_spacing_seconds == 3.0


def test_radar_config_clamps_the_pacing_knobs_to_something_usable():
    """A zero cap would stall the pass and a negative spacing means none."""
    config = radar_config_from_settings({"returnee_radar": {
        "enrich_concurrency": 0, "enrich_query_spacing_seconds": -3,
    }})

    assert config.enrich_concurrency == 1
    assert config.enrich_query_spacing_seconds == 0.0


def test_radar_config_applies_user_overrides_key_by_key():
    config = radar_config_from_settings({"returnee_radar": {"window_gameweeks": 3}})

    assert config.window_gameweeks == 3
    # Untouched keys keep their committed defaults.
    assert config.history_watchlist_strength == 0.75


def test_radar_config_survives_a_missing_block():
    assert radar_config_from_settings({}).window_gameweeks == 6
    assert radar_config_from_settings(None).window_gameweeks == 6


# ---------------------------------------------------------------------------
# Radar assembly fixtures
# ---------------------------------------------------------------------------

# A plain weekly schedule, so "N gameweeks out" is a readable date. Deadline
# dates double as the news text a return maps onto: gameweek_for_date takes the
# first deadline on or after the date, so a date sitting exactly on GW6's
# deadline resolves to GW6.
RADAR_GAMEWEEKS: list[dict[str, Any]] = [
    {
        "id": gw,
        "deadline_time": (
            datetime(2026, 8, 14, 17, 30, tzinfo=timezone.utc) + timedelta(days=7 * (gw - 1))
        ).isoformat().replace("+00:00", "Z"),
    }
    for gw in range(1, 21)
]

NEXT_GW = 1
TEAM_NAMES = {1: "Test FC"}
LAST_SEASON = "2024-25"


def _gw_deadline(gw: int) -> date:
    return datetime(2026, 8, 14, 17, 30, tzinfo=timezone.utc).date() + timedelta(days=7 * (gw - 1))


def _news_returning_in_gw(gw: int) -> str:
    """News text whose stated date resolves to exactly *gw*."""
    deadline = _gw_deadline(gw)
    return f"Calf injury - Expected back {deadline.day} {deadline.strftime('%b')}"


def _prior(strength: float, source: str = "history") -> PlayerPrior:
    return PlayerPrior(prior_strength=strength, confidence=1.0, source=source)


def _flagged(
    pid: int = 1,
    *,
    news: str = "Knee injury - Unknown return date",
    status: PlayerStatus = PlayerStatus.INJURED,
    position: PlayerPosition = PlayerPosition.MIDFIELDER,
    now_cost: int = 100,
    web_name: str = "Flagged",
    code: int | None = None,
) -> Any:
    return make_player(
        id=pid,
        code=code if code is not None else 1000 + pid,
        web_name=web_name,
        team_id=1,
        position=position,
        now_cost=now_cost,
        status=status,
        news=news,
    )


def _season(
    code: int,
    *,
    season: str = LAST_SEASON,
    minutes: int = 2600,
    starts: int = 30,
    total_points: int = 190,
    expected_goals: float = 14.0,
    expected_assists: float = 9.0,
    position: str = "MID",
) -> SeasonHistory:
    return SeasonHistory(
        element_code=code,
        season=season,
        total_points=total_points,
        minutes=minutes,
        starts=starts,
        goals=int(expected_goals),
        assists=int(expected_assists),
        expected_goals=expected_goals,
        expected_assists=expected_assists,
        expected_goal_involvements=expected_goals + expected_assists,
        start_cost=100,
        end_cost=100,
        position=position,
        web_name="Flagged",
        team_id=1,
    )


def _profile(code: int, *seasons: SeasonHistory) -> PlayerProfile:
    return PlayerProfile(
        element_code=code,
        web_name="Flagged",
        current_position="MID",
        seasons=list(seasons),
    )


def _radar(
    players: list[Any],
    priors: dict[int, PlayerPrior] | None,
    **kwargs: Any,
) -> Any:
    return build_radar(
        players,
        priors=priors,
        next_gw_id=kwargs.pop("next_gw_id", NEXT_GW),
        gameweeks=kwargs.pop("gameweeks", RADAR_GAMEWEEKS),
        team_names=kwargs.pop("team_names", TEAM_NAMES),
        now=kwargs.pop("now", PRESEASON_NOW),
        season_year=kwargs.pop("season_year", SEASON_YEAR),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Status selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [PlayerStatus.DOUBTFUL, PlayerStatus.INJURED, PlayerStatus.SUSPENDED, PlayerStatus.NOT_AVAILABLE],
)
def test_every_flagged_status_can_become_an_entry(status: PlayerStatus):
    player = _flagged(status=status)

    result = _radar([player], {1: _prior(0.9)})

    assert [e.player_id for e in result.entries] == [1]
    assert result.entries[0].status == status.value


@pytest.mark.parametrize("status", [PlayerStatus.AVAILABLE, PlayerStatus.UNAVAILABLE])
def test_unflagged_and_departed_players_are_never_entries(status: PlayerStatus):
    player = _flagged(status=status)

    result = _radar([player], {1: _prior(0.99)})

    assert result.entries == []
    assert result.degraded is False


# ---------------------------------------------------------------------------
# Quality bar: history-sourced
# ---------------------------------------------------------------------------


def test_history_sourced_player_above_the_bar_is_kept():
    result = _radar([_flagged()], {1: _prior(0.80)})

    assert [e.player_id for e in result.entries] == [1]
    assert result.entries[0].quality.basis == QUALITY_BASIS_PRIOR
    assert result.entries[0].quality.score == pytest.approx(0.80)


def test_history_sourced_player_below_the_bar_is_dropped():
    result = _radar([_flagged()], {1: _prior(0.70)})

    assert result.entries == []


def test_history_sourced_stash_bar_is_reported_separately():
    below = _radar([_flagged()], {1: _prior(0.80)}).entries[0]
    above = _radar([_flagged()], {1: _prior(0.90)}).entries[0]

    assert below.quality.meets_stash is False
    assert above.quality.meets_stash is True


# ---------------------------------------------------------------------------
# Quality bar: price-sourced (KTD3 regression guard)
# ---------------------------------------------------------------------------


def test_price_sourced_player_with_a_strong_last_healthy_season_is_kept():
    # prior_strength 0.45 is below every history threshold and cannot rise:
    # the price fallback caps it at PRICE_CONFIDENCE_FACTOR (0.5). A flat
    # threshold above 0.5 would drop exactly this player.
    player = _flagged(code=4242)
    profiles = {4242: _profile(4242, _season(4242))}

    result = _radar([player], {1: _prior(0.45, source="price")}, profiles=profiles)

    assert [e.player_id for e in result.entries] == [1]
    quality = result.entries[0].quality
    assert quality.basis == QUALITY_BASIS_SEASON
    assert quality.season == LAST_SEASON
    assert quality.quality_score is not None and quality.quality_score >= 80


def test_price_sourced_player_with_a_weak_last_healthy_season_is_dropped():
    player = _flagged(code=4243)
    weak = _season(
        4243, minutes=900, starts=10, total_points=25, expected_goals=1.0, expected_assists=1.0,
    )

    result = _radar(
        [player], {1: _prior(0.45, source="price")}, profiles={4243: _profile(4243, weak)},
    )

    assert result.entries == []


def test_price_sourced_scoring_picks_the_most_recent_season_with_real_minutes():
    player = _flagged(code=4244)
    # Last season was the injured one: below MIN_MINUTES, so it cannot be the
    # baseline. The season before it is the last healthy one.
    profiles = {
        4244: _profile(
            4244,
            _season(4244, season="2023-24", minutes=2600),
            _season(4244, season=LAST_SEASON, minutes=300, starts=3, total_points=12),
        ),
    }

    result = _radar([player], {1: _prior(0.45, source="price")}, profiles=profiles)

    assert [e.quality.season for e in result.entries] == ["2023-24"]


def test_price_sourced_quality_uses_the_seasons_appearances_for_the_minutes_factor(monkeypatch):
    seen: list[tuple[int, int, int, float]] = []
    real = returnee_radar.calculate_mins_factor

    def _spy(minutes: int, appearances: int, next_gw_id: int) -> float:
        value = real(minutes, appearances, next_gw_id)
        seen.append((minutes, appearances, next_gw_id, value))
        return value

    monkeypatch.setattr(returnee_radar, "calculate_mins_factor", _spy)
    player = _flagged(code=4245)
    # A rotation-prone season: 60 minutes a start, so the factor lands strictly
    # between 0 and 1 and demonstrably comes from this season's own numbers.
    rotated = _season(4245, minutes=1200, starts=20, total_points=90)

    _radar(
        [player],
        {1: _prior(0.45, source="price")},
        profiles={4245: _profile(4245, rotated)},
    )

    assert seen == [(1200, 20, TOTAL_GAMEWEEKS, pytest.approx(0.75))]
    # The trap KTD3 exists to avoid: a returnee has no current appearances, so
    # scoring them on current-season totals would zero the per-90 component.
    assert seen[0][3] > 0


def test_understat_npxg_reaches_the_season_score():
    player = _flagged(code=4246)
    # A mid-tier season, so neither run clamps at the position ceiling and the
    # two scores can actually be compared. The bar is lowered so both survive.
    modest = _season(
        4246, minutes=1600, starts=20, total_points=80,
        expected_goals=4.0, expected_assists=3.0,
    )
    profiles = {4246: _profile(4246, modest)}
    understat = {
        LAST_SEASON: [
            {
                "name": "Flagged",
                "team": "Test FC",
                "position": "F M S",
                "minutes": 1600,
                "npxG_per_90": 0.6,
                "xGChain_per_90": 1.1,
                "penalty_xG_per_90": 0.1,
                "xGI_per_90": 0.8,
            },
        ],
    }
    config = RadarConfig(price_watchlist_percentile=0.1)

    matched = _radar(
        [player], {1: _prior(0.45, source="price")},
        profiles=profiles, understat_seasons=understat, config=config,
    )
    unmatched = _radar(
        [player], {1: _prior(0.45, source="price")}, profiles=profiles, config=config,
    )

    # Understat's npxG path replaces the FPL xGI fallback rather than being
    # silently dropped, so the enriched run scores strictly higher.
    assert matched.entries[0].quality.quality_score > unmatched.entries[0].quality.quality_score


def test_empty_understat_season_still_scores_from_fpl_stats_alone():
    player = _flagged(code=4247)
    profiles = {4247: _profile(4247, _season(4247))}

    result = _radar(
        [player], {1: _prior(0.45, source="price")},
        profiles=profiles, understat_seasons={LAST_SEASON: []},
    )

    assert [e.player_id for e in result.entries] == [1]
    assert result.entries[0].quality.quality_score is not None
    assert result.entries[0].quality.quality_score > 0


# ---------------------------------------------------------------------------
# Quality bar: price-percentile last resort
# ---------------------------------------------------------------------------


def _price_pool(flagged_cost: int) -> list[Any]:
    pool = [_flagged(pid=1, now_cost=flagged_cost)]
    pool += [
        make_player(id=100 + i, code=5000 + i, team_id=1,
                    position=PlayerPosition.MIDFIELDER, now_cost=40 + 5 * i)
        for i in range(10)
    ]
    return pool


def test_price_sourced_player_with_no_qualifying_season_falls_back_to_price_percentile():
    result = _radar(_price_pool(130), {1: _prior(0.45, source="price")})

    assert [e.player_id for e in result.entries] == [1]
    assert result.entries[0].quality.basis == QUALITY_BASIS_PRICE
    assert result.entries[0].quality.score >= 0.80


def test_cheap_price_sourced_player_with_no_qualifying_season_is_dropped():
    result = _radar(_price_pool(45), {1: _prior(0.45, source="price")})

    assert result.entries == []


def test_a_season_below_the_minutes_floor_is_not_a_qualifying_season():
    player = _flagged(code=4248, now_cost=45)
    thin = _season(4248, minutes=MIN_MINUTES - 1, starts=5, total_points=20)
    pool = _price_pool(45)
    pool[0] = player

    result = _radar(pool, {1: _prior(0.45, source="price")}, profiles={4248: _profile(4248, thin)})

    # Falls through to the price percentile, which this cheap player fails.
    assert result.entries == []


# ---------------------------------------------------------------------------
# Window filtering
# ---------------------------------------------------------------------------


def test_return_inside_the_default_window_is_kept():
    player = _flagged(news=_news_returning_in_gw(6))

    result = _radar([player], {1: _prior(0.9)})

    assert [e.signal.return_gameweek for e in result.entries] == [6]


def test_return_beyond_the_default_window_is_dropped():
    player = _flagged(news=_news_returning_in_gw(9))

    result = _radar([player], {1: _prior(0.9)})

    assert result.entries == []


def test_date_unknown_player_is_kept_regardless_of_the_window():
    player = _flagged(news="Knee injury - Unknown return date")

    result = _radar([player], {1: _prior(0.9)}, config=RadarConfig(window_gameweeks=1))

    assert [e.player_id for e in result.entries] == [1]
    assert result.entries[0].signal.has_return_date is False


def test_a_non_default_window_from_settings_changes_what_is_kept():
    player = _flagged(news=_news_returning_in_gw(6))
    config = radar_config_from_settings({"returnee_radar": {"window_gameweeks": 2}})

    result = _radar([player], {1: _prior(0.9)}, config=config)

    assert result.entries == []


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_entries_order_by_return_gameweek_then_quality_with_unknown_last():
    players = [
        _flagged(pid=1, web_name="Unknown", news="Knee injury - Unknown return date"),
        _flagged(pid=2, web_name="Far", news=_news_returning_in_gw(5)),
        _flagged(pid=3, web_name="NearWeak", news=_news_returning_in_gw(2)),
        _flagged(pid=4, web_name="NearStrong", news=_news_returning_in_gw(2)),
    ]
    priors = {1: _prior(0.95), 2: _prior(0.90), 3: _prior(0.80), 4: _prior(0.92)}

    result = _radar(players, priors)

    assert [e.web_name for e in result.entries] == ["NearStrong", "NearWeak", "Far", "Unknown"]


# ---------------------------------------------------------------------------
# Degraded and missing data
# ---------------------------------------------------------------------------


def test_missing_priors_map_is_a_degraded_run_not_an_empty_one():
    result = _radar([_flagged()], None)

    assert result.entries == []
    assert result.degraded is True
    assert result.degraded_reason


def test_empty_priors_map_reports_the_same_degraded_run():
    result = _radar([_flagged()], {})

    assert result.entries == []
    assert result.degraded is True
    assert result.degraded_reason


def test_player_absent_from_the_priors_map_is_dropped_without_raising():
    result = _radar([_flagged(pid=1)], {999: _prior(0.99)})

    assert result.entries == []
    assert result.degraded is False


def test_no_flagged_players_is_a_healthy_empty_result():
    result = _radar([_flagged(status=PlayerStatus.AVAILABLE)], {1: _prior(0.99)})

    assert result.entries == []
    assert result.degraded is False


# ---------------------------------------------------------------------------
# Entry shape
# ---------------------------------------------------------------------------


def test_entry_carries_identity_signal_and_an_empty_transition_slot():
    player = _flagged(news="Calf injury - Expected back 18 Sep", web_name="Returnee")

    entry = _radar([player], {1: _prior(0.9)}).entries[0]

    assert entry.player_id == 1
    assert entry.code == 1001
    assert entry.web_name == "Returnee"
    assert entry.team_id == 1
    assert entry.team_name == "Test FC"
    assert entry.position == "MID"
    assert entry.price == pytest.approx(10.0)
    assert entry.signal.return_date == date(2026, 9, 18)
    # U3 fills this in from the persisted week-over-week snapshot.
    assert entry.transition is None


def test_radar_entry_is_frozen():
    entry = _radar([_flagged()], {1: _prior(0.9)}).entries[0]

    with pytest.raises(AttributeError):
        entry.transition = "new"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Snapshot store
# ---------------------------------------------------------------------------


def _record(
    *,
    status: str = "i",
    chance: int | None = None,
    return_date: date | None = None,
    lapsed: bool = False,
    web_name: str = "Flagged",
) -> Any:
    return returnee_radar.SnapshotRecord(
        status=status,
        chance_of_playing=chance,
        return_date=return_date,
        lapsed=lapsed,
        web_name=web_name,
    )


def _snapshot(gameweek: int = 1, season: str = "2026-27", **players: Any) -> Any:
    return returnee_radar.RadarSnapshot(
        season=season,
        gameweek=gameweek,
        players={int(pid): record for pid, record in players.items()},
    )


def test_snapshot_path_lands_in_the_isolated_data_dir(tmp_path):
    path = returnee_radar.snapshot_path()

    assert path == tmp_path / "user-data" / returnee_radar.SNAPSHOT_FILENAME


def test_saved_snapshot_round_trips_through_load(tmp_path):
    snapshot = _snapshot(
        gameweek=4,
        **{"7": _record(status="d", chance=25, return_date=date(2026, 10, 5), web_name="Rider")},
    )

    returnee_radar.save_snapshot(snapshot)
    loaded = returnee_radar.load_snapshot(season="2026-27")

    assert (tmp_path / "user-data" / returnee_radar.SNAPSHOT_FILENAME).is_file()
    assert loaded == snapshot


def test_snapshot_writes_accented_names_as_utf8_not_escapes(tmp_path):
    """Issue #147: the snapshot is a file a person reads and diffs week to
    week, so an accented name stays legible rather than becoming a run of
    `\\u00e9` escapes -- matching the league-history ledger beside it."""
    returnee_radar.save_snapshot(_snapshot(**{"7": _record(web_name="Ekitiké")}))

    text = (tmp_path / "user-data" / returnee_radar.SNAPSHOT_FILENAME).read_text(
        encoding="utf-8",
    )
    assert "Ekitiké" in text
    assert "\\u00e9" not in text


def test_snapshot_keeps_the_return_date_of_a_lapsed_signal(tmp_path):
    snapshot = _snapshot(**{"7": _record(return_date=date(2026, 9, 5), lapsed=True)})

    returnee_radar.save_snapshot(snapshot)
    loaded = returnee_radar.load_snapshot(season="2026-27")

    assert loaded is not None
    assert loaded.players[7].return_date == date(2026, 9, 5)
    assert loaded.players[7].lapsed is True


def test_missing_snapshot_file_loads_as_none():
    assert returnee_radar.load_snapshot(season="2026-27") is None


def test_snapshot_from_a_previous_season_is_discarded():
    returnee_radar.save_snapshot(_snapshot(season="2025-26", **{"7": _record()}))

    assert returnee_radar.load_snapshot(season="2026-27") is None


def test_snapshot_defaults_to_the_current_season_label():
    returnee_radar.save_snapshot(
        returnee_radar.RadarSnapshot(season=season_label(), gameweek=2, players={7: _record()}),
    )

    loaded = returnee_radar.load_snapshot()

    assert loaded is not None and loaded.gameweek == 2


@pytest.mark.parametrize(
    "text",
    [
        '{"metadata": {"season": "2026-27", "gameweek": 3}, "play',  # truncated
        "not json at all",
        "[]",
        '{"metadata": {"season": "2026-27"}}',  # no players block
    ],
)
def test_corrupt_snapshot_file_loads_as_none_without_raising(text: str):
    returnee_radar.snapshot_path().write_text(text, encoding="utf-8")

    assert returnee_radar.load_snapshot(season="2026-27") is None


def test_interrupted_write_leaves_the_previous_snapshot_readable(monkeypatch):
    import os as os_module

    returnee_radar.save_snapshot(_snapshot(gameweek=1, **{"7": _record(chance=25)}))

    def _boom(src: Any, dst: Any) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(os_module, "replace", _boom)
    with pytest.raises(OSError):
        returnee_radar.save_snapshot(_snapshot(gameweek=2, **{"7": _record(chance=75)}))
    monkeypatch.undo()

    loaded = returnee_radar.load_snapshot(season="2026-27")
    assert loaded is not None
    assert loaded.gameweek == 1
    assert loaded.players[7].chance_of_playing == 25


# ---------------------------------------------------------------------------
# Transition diffing
# ---------------------------------------------------------------------------


def _entry(
    pid: int = 1,
    *,
    news: str = "Knee injury - Unknown return date",
    chance: int | None = None,
    status: PlayerStatus = PlayerStatus.INJURED,
    web_name: str = "Flagged",
    now: datetime = PRESEASON_NOW,
    next_gw_id: int = NEXT_GW,
) -> Any:
    player = make_player(
        id=pid,
        code=1000 + pid,
        web_name=web_name,
        team_id=1,
        position=PlayerPosition.MIDFIELDER,
        now_cost=100,
        status=status,
        news=news,
        chance_of_playing_next_round=chance,
    )
    result = _radar([player], {pid: _prior(0.9)}, now=now, next_gw_id=next_gw_id)
    return result.entries[0]


def _diff(entries: list[Any], snapshot: Any, **kwargs: Any) -> Any:
    return returnee_radar.diff_transitions(
        entries,
        snapshot=snapshot,
        players=kwargs.pop("players", []),
        exclusions=kwargs.pop("exclusions", {}),
        **kwargs,
    )


def test_no_snapshot_leaves_every_transition_unset():
    marked, departures = _diff([_entry(chance=25)], None)

    assert [e.transition for e in marked] == [None]
    assert departures == []


def test_chance_moving_up_marks_chance_improved():
    entry = _entry(chance=25)
    snapshot = _snapshot(**{"1": _record(chance=0)})

    marked, _ = _diff([entry], snapshot)

    assert [e.transition for e in marked] == [returnee_radar.TRANSITION_CHANCE_IMPROVED]


def test_chance_moving_down_marks_chance_worsened():
    marked, _ = _diff([_entry(chance=25)], _snapshot(**{"1": _record(chance=50)}))

    assert [e.transition for e in marked] == [returnee_radar.TRANSITION_CHANCE_WORSENED]


def test_a_return_date_pulled_forward_marks_moved_earlier():
    entry = _entry(news="Calf injury - Expected back 5 Sep")
    snapshot = _snapshot(**{"1": _record(return_date=date(2026, 10, 10))})

    marked, _ = _diff([entry], snapshot)

    assert [e.transition for e in marked] == [returnee_radar.TRANSITION_DATE_EARLIER]


def test_a_return_date_pushed_back_marks_moved_later():
    entry = _entry(news="Calf injury - Expected back 5 Sep")
    snapshot = _snapshot(**{"1": _record(return_date=date(2026, 8, 29))})

    marked, _ = _diff([entry], snapshot)

    assert [e.transition for e in marked] == [returnee_radar.TRANSITION_DATE_LATER]


def test_a_date_appearing_where_there_was_none_marks_newly_dated():
    entry = _entry(news="Calf injury - Expected back 5 Sep")

    marked, _ = _diff([entry], _snapshot(**{"1": _record(return_date=None)}))

    assert [e.transition for e in marked] == [returnee_radar.TRANSITION_NEWLY_DATED]


def test_a_lapsed_date_still_diffs_against_the_fpl_update_that_follows_it():
    # The stored date is kept through the lapse (U1 keeps `return_date`), so a
    # later FPL update reads as a moved return rather than a brand new one.
    entry = _entry(news="Calf injury - Expected back 10 Oct", now=POST_GW5_NOW, next_gw_id=6)
    snapshot = _snapshot(**{"1": _record(return_date=date(2026, 9, 5), lapsed=True)})

    marked, _ = _diff([entry], snapshot)

    assert [e.transition for e in marked] == [returnee_radar.TRANSITION_DATE_LATER]


def test_a_stated_return_passing_without_arriving_marks_date_lapsed():
    # now is past the GW5 deadline, so a 5 Sep return has been missed.
    entry = _entry(
        news="Calf injury - Expected back 5 Sep",
        now=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
        next_gw_id=6,
    )
    snapshot = _snapshot(**{"1": _record(return_date=date(2026, 9, 5), lapsed=False)})

    assert entry.signal.lapsed is True
    marked, _ = _diff([entry], snapshot)

    assert [e.transition for e in marked] == [returnee_radar.TRANSITION_DATE_LAPSED]


def test_an_already_lapsed_date_does_not_re_fire_the_lapse():
    entry = _entry(
        news="Calf injury - Expected back 5 Sep",
        now=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
        next_gw_id=6,
    )
    snapshot = _snapshot(**{"1": _record(return_date=date(2026, 9, 5), lapsed=True)})

    marked, _ = _diff([entry], snapshot)

    assert [e.transition for e in marked] == [None]


def test_fpl_replacing_a_date_with_an_unknown_return_marks_date_withdrawn():
    entry = _entry(news="Calf injury - Unknown return date")
    snapshot = _snapshot(**{"1": _record(return_date=date(2026, 9, 5))})

    marked, _ = _diff([entry], snapshot)

    assert [e.transition for e in marked] == [returnee_radar.TRANSITION_DATE_WITHDRAWN]


def test_a_player_absent_from_the_previous_snapshot_marks_newly_flagged():
    marked, _ = _diff([_entry(pid=1)], _snapshot(**{"9": _record()}))

    assert [e.transition for e in marked] == [returnee_radar.TRANSITION_NEWLY_FLAGGED]


def test_an_unchanged_player_marks_no_transition():
    entry = _entry(chance=25, news="Calf injury - Expected back 5 Sep")
    snapshot = _snapshot(**{"1": _record(chance=25, return_date=date(2026, 9, 5))})

    marked, _ = _diff([entry], snapshot)

    assert [e.transition for e in marked] == [None]


def test_an_unknown_chance_on_either_side_reports_no_chance_transition():
    forwards, _ = _diff([_entry(chance=25)], _snapshot(**{"1": _record(chance=None)}))
    backwards, _ = _diff([_entry(chance=None)], _snapshot(**{"1": _record(chance=25)}))

    assert [e.transition for e in forwards] == [None]
    assert [e.transition for e in backwards] == [None]


def test_the_improving_signal_wins_when_date_and_chance_both_move():
    entry = _entry(chance=50, news="Calf injury - Expected back 5 Sep")
    snapshot = _snapshot(**{"1": _record(chance=25, return_date=date(2026, 10, 10))})

    marked, _ = _diff([entry], snapshot)

    assert [e.transition for e in marked] == [returnee_radar.TRANSITION_DATE_EARLIER]


# --- Departures ------------------------------------------------------------


def test_a_tracked_player_now_available_marks_now_available():
    pool = [_flagged(pid=1, status=PlayerStatus.AVAILABLE, web_name="Back")]

    _, departures = _diff([], _snapshot(**{"1": _record()}), players=pool)

    assert [(d.player_id, d.transition) for d in departures] == [
        (1, returnee_radar.TRANSITION_NOW_AVAILABLE),
    ]
    assert departures[0].web_name == "Back"
    assert departures[0].reason is None


@pytest.mark.parametrize(
    "reason",
    [returnee_radar.EXCLUDED_BY_WINDOW, returnee_radar.EXCLUDED_BY_QUALITY],
)
def test_a_still_flagged_player_that_left_the_list_names_the_filter(reason: str):
    pool = [_flagged(pid=1, status=PlayerStatus.INJURED)]

    _, departures = _diff(
        [], _snapshot(**{"1": _record()}), players=pool, exclusions={1: reason},
    )

    assert [(d.transition, d.reason) for d in departures] == [
        (returnee_radar.TRANSITION_DROPPED, reason),
    ]


def test_a_tracked_player_who_left_the_player_pool_is_named_from_the_snapshot():
    _, departures = _diff([], _snapshot(**{"1": _record(web_name="Gone")}), players=[])

    assert [(d.web_name, d.transition, d.reason) for d in departures] == [
        ("Gone", returnee_radar.TRANSITION_DROPPED, returnee_radar.EXCLUDED_UNKNOWN),
    ]


def test_a_player_still_on_the_watchlist_is_never_a_departure():
    entry = _entry(pid=1)
    pool = [_flagged(pid=1)]

    marked, departures = _diff([entry], _snapshot(**{"1": _record()}), players=pool)

    assert len(marked) == 1
    assert departures == []


# ---------------------------------------------------------------------------
# run_radar: assembly, persistence and degradation
# ---------------------------------------------------------------------------


def _run(
    players: list[Any],
    priors: dict[int, PlayerPrior] | None,
    **kwargs: Any,
) -> Any:
    return returnee_radar.run_radar(
        players,
        priors=priors,
        next_gw_id=kwargs.pop("next_gw_id", NEXT_GW),
        gameweeks=kwargs.pop("gameweeks", RADAR_GAMEWEEKS),
        team_names=kwargs.pop("team_names", TEAM_NAMES),
        now=kwargs.pop("now", PRESEASON_NOW),
        season_year=kwargs.pop("season_year", SEASON_YEAR),
        **kwargs,
    )


def _tracked(pid: int = 1, *, chance: int | None = None, **kwargs: Any) -> Any:
    player = _flagged(pid=pid, **kwargs)
    return make_player(
        id=player.id,
        code=player.code,
        web_name=player.web_name,
        team_id=player.team_id,
        position=player.position,
        now_cost=player.now_cost,
        status=player.status,
        news=player.news,
        chance_of_playing_next_round=chance,
    )


def test_first_run_reports_no_transitions_and_writes_a_snapshot(tmp_path):
    result = _run([_tracked(chance=0)], {1: _prior(0.9)})

    assert [e.player_id for e in result.entries] == [1]
    assert [e.transition for e in result.entries] == [None]
    assert result.departures == []
    assert result.transitions_available is False
    stored = returnee_radar.load_snapshot(season="2026-27")
    assert stored is not None
    assert stored.gameweek == NEXT_GW
    assert stored.players[1].chance_of_playing == 0
    assert (tmp_path / "user-data" / returnee_radar.SNAPSHOT_FILENAME).is_file()


def test_second_run_marks_the_chance_improvement_against_the_stored_run():
    _run([_tracked(chance=0)], {1: _prior(0.9)})

    result = _run([_tracked(chance=25)], {1: _prior(0.9)})

    assert [e.transition for e in result.entries] == [returnee_radar.TRANSITION_CHANCE_IMPROVED]
    assert result.transitions_available is True


def test_two_runs_in_one_gameweek_report_the_same_delta_and_write_once():
    _run([_tracked(chance=0)], {1: _prior(0.9)})

    second = _run([_tracked(chance=25)], {1: _prior(0.9)})
    written = returnee_radar.snapshot_path().read_text(encoding="utf-8")
    third = _run([_tracked(chance=25)], {1: _prior(0.9)})

    assert [e.transition for e in third.entries] == [e.transition for e in second.entries]
    assert returnee_radar.snapshot_path().read_text(encoding="utf-8") == written
    # Still the first run's state: a per-run write would have emptied the delta.
    stored = returnee_radar.load_snapshot(season="2026-27")
    assert stored is not None and stored.players[1].chance_of_playing == 0


def test_a_later_gameweek_rewrites_the_snapshot_after_diffing_the_previous_one():
    _run([_tracked(chance=0)], {1: _prior(0.9)})

    result = _run([_tracked(chance=25)], {1: _prior(0.9)}, next_gw_id=2)

    assert [e.transition for e in result.entries] == [returnee_radar.TRANSITION_CHANCE_IMPROVED]
    stored = returnee_radar.load_snapshot(season="2026-27")
    assert stored is not None
    assert stored.gameweek == 2
    assert stored.players[1].chance_of_playing == 25


def test_a_snapshot_from_last_season_resets_the_run_to_first_run_behaviour():
    returnee_radar.save_snapshot(
        returnee_radar.RadarSnapshot(
            season="2025-26", gameweek=NEXT_GW, players={1: _record(chance=0)},
        ),
    )

    result = _run([_tracked(chance=25)], {1: _prior(0.9)})

    assert [e.transition for e in result.entries] == [None]
    assert result.transitions_available is False
    stored = returnee_radar.load_snapshot(season="2026-27")
    assert stored is not None and stored.season == "2026-27"


def test_a_corrupt_snapshot_resets_the_run_to_first_run_behaviour():
    returnee_radar.snapshot_path().write_text('{"metadata": {"seas', encoding="utf-8")

    result = _run([_tracked(chance=25)], {1: _prior(0.9)})

    assert [e.transition for e in result.entries] == [None]
    assert result.transitions_available is False
    assert returnee_radar.load_snapshot(season="2026-27") is not None


def test_a_tracked_player_who_became_available_is_reported_as_a_return():
    _run([_tracked(pid=1)], {1: _prior(0.9)})

    result = _run([_tracked(pid=1, status=PlayerStatus.AVAILABLE)], {1: _prior(0.9)})

    assert result.entries == []
    assert [(d.player_id, d.transition) for d in result.departures] == [
        (1, returnee_radar.TRANSITION_NOW_AVAILABLE),
    ]


def test_a_still_flagged_player_pushed_out_by_the_window_is_not_reported_as_a_return():
    _run([_tracked(pid=1)], {1: _prior(0.9)})

    result = _run([_tracked(pid=1, news=_news_returning_in_gw(9))], {1: _prior(0.9)})

    assert result.entries == []
    assert [(d.transition, d.reason) for d in result.departures] == [
        (returnee_radar.TRANSITION_DROPPED, returnee_radar.EXCLUDED_BY_WINDOW),
    ]


def test_a_still_flagged_player_dropped_by_the_quality_bar_names_that_filter():
    _run([_tracked(pid=1)], {1: _prior(0.9)})

    result = _run([_tracked(pid=1)], {1: _prior(0.5)})

    assert result.entries == []
    assert [(d.transition, d.reason) for d in result.departures] == [
        (returnee_radar.TRANSITION_DROPPED, returnee_radar.EXCLUDED_BY_QUALITY),
    ]


def test_build_radar_records_why_each_flagged_player_was_excluded():
    result = _radar(
        [_flagged(pid=1, news=_news_returning_in_gw(9)), _flagged(pid=2)],
        {1: _prior(0.9), 2: _prior(0.5)},
    )

    assert result.exclusions == {
        1: returnee_radar.EXCLUDED_BY_WINDOW,
        2: returnee_radar.EXCLUDED_BY_QUALITY,
    }


def test_a_degraded_run_leaves_the_stored_snapshot_untouched():
    _run([_tracked(chance=0)], {1: _prior(0.9)})
    before = returnee_radar.snapshot_path().read_text(encoding="utf-8")

    result = _run([_tracked(chance=25)], None, next_gw_id=2)

    assert result.degraded is True
    assert result.entries == []
    assert result.departures == []
    assert returnee_radar.snapshot_path().read_text(encoding="utf-8") == before


def test_persist_false_diffs_without_storing_the_run():
    _run([_tracked(chance=0)], {1: _prior(0.9)})
    before = returnee_radar.snapshot_path().read_text(encoding="utf-8")

    result = _run([_tracked(chance=25)], {1: _prior(0.9)}, next_gw_id=2, persist=False)

    assert [e.transition for e in result.entries] == [returnee_radar.TRANSITION_CHANCE_IMPROVED]
    assert returnee_radar.snapshot_path().read_text(encoding="utf-8") == before


def test_run_radar_without_a_stored_gameweek_still_returns_the_ordered_watchlist():
    players = [
        _tracked(pid=1, web_name="Unknown"),
        _tracked(pid=2, web_name="Near", news=_news_returning_in_gw(2)),
    ]

    result = _run(players, {1: _prior(0.95), 2: _prior(0.9)})

    assert [e.web_name for e in result.entries] == ["Near", "Unknown"]


def test_a_failed_snapshot_write_still_returns_the_watchlist(monkeypatch):
    def _boom(snapshot: Any) -> None:
        raise OSError("read-only data dir")

    monkeypatch.setattr(returnee_radar, "save_snapshot", _boom)

    result = _run([_tracked(chance=0)], {1: _prior(0.9)})

    assert [e.player_id for e in result.entries] == [1]
    assert result.transitions_available is False


def test_radar_departure_is_frozen():
    _run([_tracked(pid=1)], {1: _prior(0.9)})
    departure = _run(
        [_tracked(pid=1, status=PlayerStatus.AVAILABLE)], {1: _prior(0.9)},
    ).departures[0]

    with pytest.raises(AttributeError):
        departure.reason = "window"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Enriched intel is held to the same lapse rule as FPL news
# ---------------------------------------------------------------------------


def _intel_json(expected_return: str | None) -> str:
    return (
        '{"expected_return": ' + ("null" if expected_return is None
                                  else f'"{expected_return}"')
        + ', "summary": "Back in training", "confidence": "high"}'
    )


def test_an_enriched_date_already_behind_the_deadline_is_dropped():
    """A model can answer with a date the season has passed; rendering it as an
    upcoming return -- or letting it clear the escalation window -- is worse
    than reporting no date at all."""
    enrichment = enrichment_from_response(
        _intel_json("2026-09-05"), gameweeks=GAMEWEEKS, now=POST_GW5_NOW,
    )

    assert enrichment.return_date is None
    assert enrichment.return_gameweek is None
    # Only the unusable date goes; what the search actually said survives.
    assert enrichment.summary == "Back in training"


def test_an_enriched_date_still_ahead_of_the_deadline_is_kept():
    enrichment = enrichment_from_response(
        _intel_json("2026-10-10"), gameweeks=GAMEWEEKS, now=POST_GW5_NOW,
    )

    assert enrichment.return_date == date(2026, 10, 10)
    assert enrichment.return_gameweek == 6
