"""Injury returnee radar: turning FPL availability news into return signals.

The FPL `news` field is the only return-timing signal that ships with the data
every run already fetches, so this module parses it directly. The grammar is
observed rather than contractual: a live bootstrap-static snapshot resolved to
exactly four shapes, and only two of them carry a date.

    {reason} - Expected back {D} {Mmm}      -> date
    Suspended until {D} {Mmm}               -> date
    {reason} - {NN}% chance of playing      -> no date (the percentage
                                               duplicates
                                               `chance_of_playing_next_round`)
    {reason} - Unknown return date          -> no date

Parse contract:

* Nothing here raises on bad input. Anything the two dated shapes do not match
  -- a new phrasing, a transfer note, an empty string -- yields a signal with
  no date, because date-unknown is the common case (roughly one flagged player
  in eight carries a date), not the error case.
* FPL states a day and a month with no year. The year is resolved against the
  season start year on the same July cutover `fpl_cli.season` uses, so a
  February return during an August-start season lands in the following calendar
  year.
* A resolved date is mapped to a gameweek by walking event deadlines, never by
  assuming a fixed number of weeks per gameweek -- the live schedule has a
  three-week break between GW5 and GW6. A date past the final deadline maps to
  no gameweek rather than being clamped onto GW38.
* A date that falls before the current gameweek's deadline while the player is
  still flagged has *lapsed*: `has_return_date` goes False and the signal reads
  as date-unknown, while `return_date` keeps the stated date for display and
  for week-over-week diffing. Decaying into the date-unknown bucket rather than
  inventing a new state means a failed return stays on the watchlist instead of
  advertising a return gameweek that has already been missed.

There is no cache here: every signal is derived from data the caller already
holds. Internal date maths is UTC throughout; formatting a date for a user is
the caller's job and goes through `fpl_cli.utils.time`.

Radar assembly
--------------

`build_radar` turns those signals into the short, ordered watchlist the radar
command renders. Two rules keep it short:

* A *quality bar* that is source-aware, because a returnee cannot be judged on
  current-season form or cumulative minutes -- they structurally have neither.
  `generate_player_prior` only assigns `source="history"` at 450+ minutes in the
  previous season, so a player who missed most of it lands on the `"price"`
  fallback where `prior_strength` can never exceed 0.5. Gating everyone on one
  `prior_strength` threshold above 0.5 would therefore exclude exactly the
  population this radar exists to surface. History-sourced players are gated on
  `prior_strength`; price-sourced players are scored through the repo's own
  VALUE quality function over their most recent season carrying real minutes,
  and only a player with no such season falls back to the within-position price
  percentile -- price tracks ownership churn and editorial pricing, not output.
* A *window*: a return that lands inside the next N gameweeks, or a return whose
  date is unknown (the common case, and the one worth watching).

Nothing here fetches. `prepare_scoring_data(include_prior=True)` builds priors
and then discards the `PlayerProfile` objects it built them from -- and skips
fetching them at all on a cache hit -- so the historical seasons and the
Understat season data the price-sourced branch needs are passed in by the
caller. That keeps the service pure, keeps the deterministic core inside the
data a run already fetched, and lets tests stub both seams with plain fixtures.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from fpl_cli.api.understat import match_fpl_to_understat
from fpl_cli.models.player import POSITION_MAP
from fpl_cli.season import TOTAL_GAMEWEEKS, get_season_year
from fpl_cli.services.player_prior import MIN_MINUTES, PlayerPrior
from fpl_cli.services.scoring.constants import Position, _as_position, _value_weights_and_ceiling
from fpl_cli.services.scoring.display import normalise_score
from fpl_cli.services.scoring.evaluation import build_player_evaluation, read_player_field
from fpl_cli.services.scoring.value_quality import (
    calculate_mins_factor,
    calculate_player_quality_score,
)

if TYPE_CHECKING:
    from fpl_cli.api.historical_types import PlayerProfile, SeasonHistory

# Identifies where a return date came from. U5's optional AI-search enrichment
# adds its own source alongside this one.
SOURCE_FPL_NEWS = "fpl-news"

# The availability statuses a radar entry can be built from. `u` (unavailable —
# left the league) is deliberately absent: those players are gone, not due back.
FLAGGED_STATUSES: frozenset[str] = frozenset({"d", "i", "s", "n"})

# How a quality verdict was reached. `prior` is the history-sourced path,
# `season-quality` the price-sourced player scored over a real season, `price`
# the last-resort within-position price percentile.
QUALITY_BASIS_PRIOR = "prior"
QUALITY_BASIS_SEASON = "season-quality"
QUALITY_BASIS_PRICE = "price"

# July cutover, matching `fpl_cli.season.get_season_year`: a month at or after
# July belongs to the season start year, an earlier month to the year after.
_CUTOVER_MONTH = 7

_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# The two measured shapes that carry a date. Each anchors on its own keyword
# phrase, so the `{NN}% chance of playing` shape cannot be read as a day.
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bexpected\s+back\s+(\d{1,2})\s+([a-z]+)", re.IGNORECASE),
    re.compile(r"\bsuspended\s+until\s+(\d{1,2})\s+([a-z]+)", re.IGNORECASE),
)


@dataclass(frozen=True)
class ReturnSignal:
    """One player's parsed availability news.

    `return_date` survives lapsing so a display can say "was due 5 Sep" and the
    week-over-week diff can tell a moved date apart from a newly stated one.
    `has_return_date` -- not `return_date is not None` -- is the check for
    whether a usable date exists.
    """

    news: str
    chance_of_playing: int | None = None
    return_date: date | None = None
    return_gameweek: int | None = None
    source: str | None = None
    news_age_days: int | None = None
    lapsed: bool = False

    @property
    def has_return_date(self) -> bool:
        """Whether a return date is both known and still ahead of us."""
        return self.return_date is not None and not self.lapsed


def parse_news_date(news: str) -> tuple[int, int] | None:
    """Extract a `(day, month)` pair from FPL news text, or None.

    Matches only the two measured date-bearing shapes. An unrecognised
    phrasing, an unknown month token or an empty string yields None.
    """
    if not news:
        return None
    for pattern in _DATE_PATTERNS:
        match = pattern.search(news)
        if match is None:
            continue
        month = _MONTHS.get(match.group(2)[:3].lower())
        if month is None:
            continue
        return int(match.group(1)), month
    return None


def resolve_return_date(day: int, month: int, season_year: int | None = None) -> date | None:
    """Resolve a bare day/month against the season, or None if impossible.

    FPL states no year. Months at or after the July cutover belong to the
    season start year, earlier months to the following calendar year, so a
    February return in the 2026-27 season resolves to February 2027.
    """
    year = season_year if season_year is not None else get_season_year()
    if month < _CUTOVER_MONTH:
        year += 1
    try:
        return date(year, month, day)
    except ValueError:
        # e.g. "Expected back 31 Feb" -- treated as date-unknown, not an error.
        return None


def gameweek_for_date(target: date, gameweeks: Sequence[Mapping[str, Any]]) -> int | None:
    """Return the gameweek a date falls in, walking event deadlines.

    The first gameweek whose deadline is on or after *target* wins, so a date
    in a multi-week break lands on the gameweek that follows it. A date past
    the final deadline returns None rather than being clamped -- a return
    beyond the fixture list on hand is unknown, not imminent.
    """
    best_gw: int | None = None
    best_deadline: date | None = None
    for event in gameweeks:
        deadline = _parse_utc(event.get("deadline_time"))
        gw_id = event.get("id")
        if deadline is None or not isinstance(gw_id, int):
            continue
        deadline_date = deadline.date()
        if deadline_date < target:
            continue
        if best_deadline is None or deadline_date < best_deadline:
            best_gw, best_deadline = gw_id, deadline_date
    return best_gw


def news_age_days(news_added: str | datetime | None, now: datetime | None = None) -> int | None:
    """Whole days since FPL last touched this player's news, or None.

    None covers both an absent stamp and an unparseable one. A stamp in the
    future (clock skew between the API and this machine) clamps to 0.
    """
    added = _parse_utc(news_added)
    if added is None:
        return None
    reference = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    return max(0, (reference - added).days)


def build_return_signal(
    player: Any,
    *,
    gameweeks: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
    season_year: int | None = None,
) -> ReturnSignal:
    """Build the return signal for a player model or player-shaped mapping.

    Reads `news`, `news_added` and `chance_of_playing_next_round` through
    `read_player_field`, so both shapes are accepted. Never raises: an
    unparseable news string yields a date-unknown signal.
    """
    news = read_player_field(player, "news", "") or ""
    chance = read_player_field(player, "chance_of_playing_next_round")
    added = read_player_field(player, "news_added")

    parsed = parse_news_date(news)
    return_date = resolve_return_date(*parsed, season_year) if parsed else None

    lapsed = False
    return_gameweek: int | None = None
    if return_date is not None:
        current_deadline = _current_deadline_date(gameweeks, now)
        lapsed = current_deadline is not None and return_date < current_deadline
        if not lapsed:
            return_gameweek = gameweek_for_date(return_date, gameweeks)

    return ReturnSignal(
        news=news,
        chance_of_playing=chance,
        return_date=return_date,
        return_gameweek=return_gameweek,
        source=SOURCE_FPL_NEWS if return_date is not None else None,
        news_age_days=news_age_days(added, now),
        lapsed=lapsed,
    )


# ---------------------------------------------------------------------------
# Radar assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RadarConfig:
    """Resolved radar tuning knobs.

    Built from the `returnee_radar` block in settings so the service itself
    never reaches for configuration. Defaults here mirror `defaults.yaml`; the
    duplication is deliberate, so a caller that has no settings to hand (a
    test, another service) still gets the shipped behaviour.
    """

    window_gameweeks: int = 6
    stash_window_gameweeks: int = 2
    history_watchlist_strength: float = 0.75
    history_stash_strength: float = 0.85
    price_watchlist_percentile: float = 0.80
    price_stash_percentile: float = 0.90
    stash_upgrade_margin: float = 5.0


def radar_config_from_settings(settings: Mapping[str, Any] | None) -> RadarConfig:
    """Resolve radar config from a settings mapping, key by key.

    A missing block, a missing key or a non-numeric value each fall back to the
    shipped default rather than raising -- a hand-edited `settings.yaml` should
    not be able to break the radar.
    """
    block = (settings or {}).get("returnee_radar") or {}
    if not isinstance(block, Mapping):
        block = {}
    defaults = RadarConfig()
    return RadarConfig(
        window_gameweeks=_setting_int(block, "window_gameweeks", defaults.window_gameweeks),
        stash_window_gameweeks=_setting_int(
            block, "stash_window_gameweeks", defaults.stash_window_gameweeks,
        ),
        history_watchlist_strength=_setting_float(
            block, "history_watchlist_strength", defaults.history_watchlist_strength,
        ),
        history_stash_strength=_setting_float(
            block, "history_stash_strength", defaults.history_stash_strength,
        ),
        price_watchlist_percentile=_setting_float(
            block, "price_watchlist_percentile", defaults.price_watchlist_percentile,
        ),
        price_stash_percentile=_setting_float(
            block, "price_stash_percentile", defaults.price_stash_percentile,
        ),
        stash_upgrade_margin=_setting_float(
            block, "stash_upgrade_margin", defaults.stash_upgrade_margin,
        ),
    )


@dataclass(frozen=True)
class QualityVerdict:
    """Why one flagged player did or did not clear the quality bar.

    `score` is always the 0-1 measure that was actually compared against
    `threshold`, whichever branch produced it, so a caller can sort a mixed
    list without knowing which branch each entry came from. `quality_score`
    carries the 0-100 normalised season score for display, and is None on the
    branches that never computed one.
    """

    basis: str
    score: float
    threshold: float
    passed: bool
    meets_stash: bool = False
    prior_source: str | None = None
    season: str | None = None
    quality_score: int | None = None


@dataclass(frozen=True)
class RadarEntry:
    """One flagged player worth watching, with why and when."""

    player_id: int
    code: int
    web_name: str
    team_id: int
    team_name: str
    position: str
    status: str
    chance_of_playing: int | None
    price: float
    signal: ReturnSignal
    quality: QualityVerdict
    # Week-over-week transition ("new", "date-moved", "returned", ...). U3's
    # snapshot diff fills this in with `dataclasses.replace`; the radar core
    # holds no history of its own.
    transition: str | None = None


@dataclass(frozen=True)
class RadarResult:
    """The radar's watchlist plus whether the run had everything it needed.

    `degraded` exists because an empty watchlist is ambiguous otherwise:
    `prepare_scoring_data` swallows a failed prior generation and leaves
    `player_priors` as None, which leaves the quality bar nothing to gate on.
    "Nobody is flagged" and "the quality bar could not run" must not render the
    same way.
    """

    entries: list[RadarEntry] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None


def build_radar(
    players: Sequence[Any],
    *,
    priors: Mapping[int, PlayerPrior] | None,
    next_gw_id: int,
    gameweeks: Sequence[Mapping[str, Any]] = (),
    config: RadarConfig | None = None,
    profiles: Mapping[int, PlayerProfile] | None = None,
    understat_seasons: Mapping[str, Sequence[dict[str, Any]]] | None = None,
    team_names: Mapping[int, str] | None = None,
    now: datetime | None = None,
    season_year: int | None = None,
) -> RadarResult:
    """Assemble the ordered radar watchlist. Fetches nothing.

    Args:
        players: The full current player pool. All of it, not just the flagged
            ones -- the price-percentile last resort ranks within position and
            needs the whole distribution.
        priors: `ScoringData.player_priors`, keyed by season-local player id.
            None or empty is a degraded run, not an empty watchlist.
        next_gw_id: The gameweek the window is measured from.
        gameweeks: Raw event dicts, for mapping a return date to a gameweek.
        config: Resolved tuning knobs; shipped defaults when omitted.
        profiles: `PlayerProfile` per `element_code`, from
            `HistoricalDataProvider.get_all_player_histories()`. Supplied by the
            caller because `prepare_scoring_data` discards the profiles it built
            the priors from. Without them the price-sourced branch has no season
            to score and falls back to the price percentile.
        understat_seasons: Understat league players keyed by *vaastav* season
            label ("2024-25"), each the whole-season list `get_league_players`
            returns in one memoised request. A season that is missing or empty
            degrades to the FPL-only path rather than failing.
        team_names: Team id to full team name, as `match_fpl_to_understat`
            expects it (it maps through `TEAM_NAME_MAP`).
        now: Reference time for lapsing and news age; defaults to now.
        season_year: Season start year for resolving bare day/month dates.
    """
    cfg = config or RadarConfig()
    if not priors:
        return RadarResult(
            degraded=True,
            degraded_reason=(
                "Player priors are unavailable, so the quality bar cannot run — "
                "the watchlist is empty because it could not be built, not "
                "because nobody is flagged."
            ),
        )

    names = team_names or {}
    percentiles = _price_percentiles(players)
    entries: list[RadarEntry] = []

    for player in players:
        status = _status_code(player)
        if status not in FLAGGED_STATUSES:
            continue
        player_id = _as_int(read_player_field(player, "id"))
        prior = priors.get(player_id)
        position = _player_position(player)
        if prior is None or position is None:
            # No prior entry means no bar to clear: drop the player rather than
            # guessing, and rather than raising on a pool/prior mismatch.
            continue

        signal = build_return_signal(
            player, gameweeks=gameweeks, now=now, season_year=season_year,
        )
        if not _within_window(signal, next_gw_id, cfg.window_gameweeks):
            continue

        team_id = _as_int(read_player_field(player, "team_id"))
        verdict = _judge_quality(
            player,
            prior=prior,
            position=position,
            config=cfg,
            profiles=profiles,
            understat_seasons=understat_seasons,
            team_name=names.get(team_id, ""),
            price_percentile=percentiles.get(player_id, 0.0),
        )
        if not verdict.passed:
            continue

        entries.append(RadarEntry(
            player_id=player_id,
            code=_as_int(read_player_field(player, "code")),
            web_name=str(read_player_field(player, "web_name", "") or ""),
            team_id=team_id,
            team_name=names.get(team_id, ""),
            position=position,
            status=status,
            chance_of_playing=signal.chance_of_playing,
            price=_player_price(player),
            signal=signal,
            quality=verdict,
        ))

    entries.sort(key=_entry_order)
    return RadarResult(entries=entries)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _entry_order(entry: RadarEntry) -> tuple[int, int, float, str]:
    """Near-term returns first, date-unknown last, quality breaking ties."""
    known = entry.signal.has_return_date and entry.signal.return_gameweek is not None
    return (
        0 if known else 1,
        entry.signal.return_gameweek or 0,
        -entry.quality.score,
        entry.web_name,
    )


def _within_window(signal: ReturnSignal, next_gw_id: int, window_gameweeks: int) -> bool:
    """Whether a signal falls inside the watchlist window.

    A date-unknown signal (which includes a lapsed one) is always inside it:
    R4 keeps those on the list precisely because nobody knows when they are
    back, and they are the majority of flagged players.
    """
    if not signal.has_return_date or signal.return_gameweek is None:
        return True
    return signal.return_gameweek <= next_gw_id + window_gameweeks - 1


def _judge_quality(
    player: Any,
    *,
    prior: PlayerPrior,
    position: Position,
    config: RadarConfig,
    profiles: Mapping[int, PlayerProfile] | None,
    understat_seasons: Mapping[str, Sequence[dict[str, Any]]] | None,
    team_name: str,
    price_percentile: float,
) -> QualityVerdict:
    """Apply the source-aware quality bar to one flagged player (KTD3).

    History-sourced players are judged on `prior_strength`. Everyone else is
    price-sourced, where `prior_strength` is capped at `PRICE_CONFIDENCE_FACTOR`
    and so cannot be compared against the same number: they are scored over
    their last season with real minutes, and fall back to the within-position
    price percentile only when no such season exists.
    """
    if prior.source == "history":
        return QualityVerdict(
            basis=QUALITY_BASIS_PRIOR,
            score=prior.prior_strength,
            threshold=config.history_watchlist_strength,
            passed=prior.prior_strength >= config.history_watchlist_strength,
            meets_stash=prior.prior_strength >= config.history_stash_strength,
            prior_source=prior.source,
        )

    season = _last_healthy_season(profiles, _as_int(read_player_field(player, "code")))
    if season is not None:
        quality = _season_quality(
            player,
            position=position,
            season=season,
            understat_seasons=understat_seasons,
            team_name=team_name,
        )
        if quality is not None:
            # The 0-100 score is already normalised against the calibrated
            # per-position ceiling, so dividing by 100 puts it in the same
            # within-position units as the price percentile it shares a
            # threshold with.
            score = quality / 100
            return QualityVerdict(
                basis=QUALITY_BASIS_SEASON,
                score=score,
                threshold=config.price_watchlist_percentile,
                passed=score >= config.price_watchlist_percentile,
                meets_stash=score >= config.price_stash_percentile,
                prior_source=prior.source,
                season=season.season,
                quality_score=quality,
            )

    return QualityVerdict(
        basis=QUALITY_BASIS_PRICE,
        score=price_percentile,
        threshold=config.price_watchlist_percentile,
        passed=price_percentile >= config.price_watchlist_percentile,
        meets_stash=price_percentile >= config.price_stash_percentile,
        prior_source=prior.source,
    )


def _last_healthy_season(
    profiles: Mapping[int, PlayerProfile] | None, code: int,
) -> SeasonHistory | None:
    """The most recent season in the window carrying real minutes.

    "Real" is `MIN_MINUTES` — the same 450 that decides whether
    `generate_player_prior` trusts a season at all, so the radar cannot judge a
    season the prior would have rejected. Season labels sort chronologically.
    """
    if not profiles or code <= 0:
        return None
    profile = profiles.get(code)
    if profile is None:
        return None
    qualifying = [s for s in profile.seasons if s.minutes >= MIN_MINUTES]
    if not qualifying:
        return None
    return max(qualifying, key=lambda s: s.season)


def _season_quality(
    player: Any,
    *,
    position: Position,
    season: SeasonHistory,
    understat_seasons: Mapping[str, Sequence[dict[str, Any]]] | None,
    team_name: str,
) -> int | None:
    """Score one completed season through the repo's VALUE quality function.

    The season is assembled into a player-shaped mapping and read by
    `build_player_evaluation`, which goes through `read_player_field` and so
    accepts a mapping as readily as a model. No scoring formula is touched.

    Two substitutions make a past season comparable to the calibrated ceiling:

    * `appearances` is the season's starts, the only appearance count vaastav
      carries. It is what makes the minutes factor measure rotation risk —
      scoring a returnee on their (empty) current-season appearances would send
      `calculate_mins_factor` to 0.0 and zero the per-90 component outright.
    * `form` is the season's points per appearance. FPL's form is a 30-day
      average with no historical equivalent, and the ceiling was calibrated
      with the form term present; leaving it at zero would depress every
      historical score by up to 40% of that ceiling and make the bar
      unreachable. Over a whole season a player's own points per appearance is
      the best estimate of the quantity form measures.

    The reference gameweek is `TOTAL_GAMEWEEKS`: the season is complete, so the
    minutes factor should be fully active regardless of how far into the
    current season the radar happens to run.
    """
    minutes, appearances = season.minutes, season.starts
    if minutes <= 0 or appearances <= 0:
        return None

    ppg = season.total_points / appearances
    xgi = season.expected_goal_involvements or (season.expected_goals + season.expected_assists)
    data: dict[str, Any] = {
        "id": _as_int(read_player_field(player, "id")),
        "web_name": season.web_name,
        "position": position,
        "minutes": minutes,
        "appearances": appearances,
        "form": ppg,
        "ppg": ppg,
        "xGI_per_90": xgi / minutes * 90,
        "price": season.end_cost / 10,
    }

    match = _understat_match(player, position=position, season=season,
                             understat_seasons=understat_seasons, team_name=team_name)
    if match:
        # Only the per-90 rates the quality weights read: the season's own
        # totals stay authoritative for minutes, appearances and identity.
        for key in ("npxG_per_90", "xGChain_per_90", "penalty_xG_per_90"):
            value = match.get(key)
            if value is not None:
                data[key] = value

    evaluation, _ = build_player_evaluation(data)
    weights, ceiling = _value_weights_and_ceiling(position)
    mins_factor = calculate_mins_factor(minutes, appearances, TOTAL_GAMEWEEKS)
    raw = calculate_player_quality_score(
        evaluation.as_quality_dict(), weights, mins_factor, position=position,
    )
    return normalise_score(raw, ceiling)


def _understat_match(
    player: Any,
    *,
    position: Position,
    season: SeasonHistory,
    understat_seasons: Mapping[str, Sequence[dict[str, Any]]] | None,
    team_name: str,
) -> dict[str, Any] | None:
    """Find this player in the injected Understat season, or None.

    A missing or empty season degrades to the FPL-only path. Matching uses the
    player's *current* club: vaastav's `team_id` is that season's league-local
    id and cannot be resolved to a name here, so a player who has since moved
    simply fails to match and loses the xG sharpening.
    """
    if not understat_seasons:
        return None
    pool = understat_seasons.get(season.season)
    if not pool:
        return None
    web_name = str(read_player_field(player, "web_name", "") or season.web_name)
    try:
        return match_fpl_to_understat(
            web_name, team_name, list(pool),
            fpl_position=position, fpl_minutes=season.minutes,
        )
    except (KeyError, TypeError, ValueError):
        # The Understat payload is undocumented and injected from outside; a
        # renamed key must cost xG sharpening, not the whole watchlist.
        return None


def _price_percentiles(players: Sequence[Any]) -> dict[int, float]:
    """Within-position price percentile per player id (0.0-1.0).

    Mirrors `player_prior._percentile_rank` so the last-resort bar and the
    prior's own price fallback rank a player identically.
    """
    prices: dict[int, tuple[Position, float]] = {}
    by_position: dict[Position, list[float]] = {}
    for player in players:
        position = _player_position(player)
        if position is None:
            continue
        price = _player_price(player)
        prices[_as_int(read_player_field(player, "id"))] = (position, price)
        by_position.setdefault(position, []).append(price)

    result: dict[int, float] = {}
    for player_id, (position, price) in prices.items():
        values = by_position[position]
        if len(values) <= 1:
            result[player_id] = 0.5
            continue
        below = sum(1 for v in values if v < price)
        equal = sum(1 for v in values if v == price)
        result[player_id] = (below + equal * 0.5) / len(values)
    return result


def _player_position(player: Any) -> Position | None:
    """Resolve a player's position, or None when it cannot be narrowed."""
    name = read_player_field(player, "position_name")
    if not name or name == "???":
        raw = read_player_field(player, "position")
        value = getattr(raw, "value", raw)
        name = POSITION_MAP.get(value) if isinstance(value, int) else value
    if not name:
        return None
    try:
        return _as_position(str(name))
    except ValueError:
        return None


def _player_price(player: Any) -> float:
    """Price in millions, from the model's computed field or raw now_cost."""
    price = read_player_field(player, "price")
    if price is None:
        return float(read_player_field(player, "now_cost", 0) or 0) / 10
    return float(price)


def _status_code(player: Any) -> str:
    """The single-character availability status, enum or string."""
    raw = read_player_field(player, "status", "a")
    return str(getattr(raw, "value", raw) or "a")


def _as_int(value: Any) -> int:
    """Coerce an id-shaped field to int, defaulting to 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _setting_int(block: Mapping[str, Any], key: str, default: int) -> int:
    """Read one integer setting, falling back on anything unusable."""
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _setting_float(block: Mapping[str, Any], key: str, default: float) -> float:
    """Read one float setting, falling back on anything unusable."""
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _as_utc(value: datetime) -> datetime:
    """Normalise a datetime to UTC, reading a naive one as UTC (FPL convention)."""
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_utc(value: Any) -> datetime | None:
    """Coerce an FPL ISO timestamp to a UTC datetime, or None if unusable."""
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _current_deadline_date(gameweeks: Sequence[Mapping[str, Any]], now: datetime | None) -> date | None:
    """The most recent deadline already passed, which is what a date must beat.

    Measuring against the deadline that has passed rather than the one coming
    up keeps a return stated for later this week off the lapsed pile: only a
    date the current gameweek has already left behind counts as failed.
    """
    reference = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    latest: date | None = None
    for event in gameweeks:
        deadline = _parse_utc(event.get("deadline_time"))
        if deadline is None or deadline > reference:
            continue
        if latest is None or deadline.date() > latest:
            latest = deadline.date()
    return latest
