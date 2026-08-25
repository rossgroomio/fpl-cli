"""Tests for the injury returnee radar's news signal parser."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from fpl_cli.api.historical_types import PlayerProfile, SeasonHistory
from fpl_cli.cli._context import load_settings
from fpl_cli.models.player import PlayerPosition, PlayerStatus
from fpl_cli.season import TOTAL_GAMEWEEKS, get_season_year
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
