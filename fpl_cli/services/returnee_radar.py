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

Parsing caches nothing: every signal is derived from data the caller already
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

Optional AI-search enrichment
-----------------------------

Most long-term injuries carry no parseable date at all, so return timing for
exactly the players this radar exists to surface has to come from somewhere
else. `select_enrichment_shortlist` picks the entries worth asking about --
date-unknown, or dated so long ago it is worth re-checking -- and the caller
queries a research provider for each, one player per query so the provider's
citation list belongs to a single player.

Three rules keep enrichment from quietly becoming load-bearing:

* It never overwrites the FPL signal. `apply_enrichment` attaches an
  `EnrichedReturn` in its own field, and where both sources state a date both
  survive to the output (R8).
* An enriched date only decides an irreversible action when it is cited.
  `escalation_verdict` is where that lives: an FPL-stated date inside the
  escalation window counts on its own, an enriched one only with a source
  citation behind it (R16). Uncited intel still renders.
* Answers are cached per season and gameweek in the *cache* dir, empty answers
  included, so a second run in one gameweek buys nothing twice. Losing that
  cache costs a re-query, not data.

Nothing in this section fetches either: the caller runs the query and hands
the response text and citations to `enrichment_from_response`.

Week-over-week deltas
---------------------

The actionable trigger is not "this player is injured" but "this player's
availability improved since last week", and that needs memory. Each run stores
the watchlist it produced in one season-stamped JSON file in the data dir, and
diffs against the last watchlist stored in an earlier gameweek. The store follows `player_prior.yaml`: read it,
compare the season label, discard and rebuild when it does not match -- which
is what makes keying records on season-local player id safe, since a snapshot
never survives the id reshuffle at a season boundary. Anything unreadable, of
the wrong shape or from another season is a first run, not an error.

Two rules keep the delta worth reading:

* The file holds two slots: the *baseline* every run in the current gameweek
  diffs against, and the current gameweek's own state, refreshed by every run
  and promoted to baseline only once a run arrives in a later gameweek. One
  slot made the delta a one-shot claimed by whichever run of the gameweek
  happened to be first (#225): that run overwrote last week's baseline with
  its own view, leaving every later run in the gameweek diffing against
  itself and reporting nothing changed.
* A player who has left the watchlist is resolved against their live status
  before anything is said about them. Only status `a` is a return; a player
  the window or the quality bar excluded is reported as dropped off the list,
  naming which filter did it. Reporting a slipped return as a return is the
  most misleading thing this watchlist could say.

`run_radar` is all of that in one call. `build_radar` stays pure for callers
that want no history, and `diff_transitions` compares with no I/O of its own.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from fpl_cli.api.understat import match_fpl_to_understat
from fpl_cli.models.player import POSITION_MAP
from fpl_cli.paths import user_data_file
from fpl_cli.season import TOTAL_GAMEWEEKS, get_season_year, season_label
from fpl_cli.services.player_prior import MIN_MINUTES, PlayerPrior, percentile_rank
from fpl_cli.services.scoring.constants import (
    Position,
    _as_position,
    _value_weights_and_ceiling,
    ceiling_attainability,
)
from fpl_cli.services.scoring.display import normalise_score
from fpl_cli.services.scoring.evaluation import (
    build_player_evaluation,
    gk_xgc_quality,
    read_player_field,
)
from fpl_cli.services.scoring.value_quality import (
    calculate_mins_factor,
    calculate_player_quality_score,
)
from fpl_cli.utils.files import atomic_write_text

if TYPE_CHECKING:
    from fpl_cli.api.historical_types import PlayerProfile, SeasonHistory

logger = logging.getLogger(__name__)

# int and float settings share one reader; the cast is what separates them.
_NumberT = TypeVar("_NumberT", int, float)

# Identifies where a return date came from. The two never merge: an enriched
# date is carried beside the FPL-derived one, never over it (R8).
SOURCE_FPL_NEWS = "fpl-news"
SOURCE_AI_SEARCH = "ai-search"

# The availability statuses a radar entry can be built from. `u` (unavailable —
# left the league) is deliberately absent: those players are gone, not due back.
FLAGGED_STATUSES: frozenset[str] = frozenset({"d", "i", "s", "n"})

# How a quality verdict was reached. `prior` is the history-sourced path,
# `season-quality` the price-sourced player scored over a real season, `price`
# the last-resort within-position price percentile.
QUALITY_BASIS_PRIOR = "prior"
QUALITY_BASIS_SEASON = "season-quality"
QUALITY_BASIS_PRICE = "price"

# Why a still-flagged player is no longer an entry, reported alongside
# `TRANSITION_DROPPED`. Naming the filter is the whole point: a player the
# window pushed out has not returned, and rendering that as a return is the
# most misleading thing this watchlist could say.
EXCLUDED_BY_WINDOW = "window"
EXCLUDED_BY_QUALITY = "quality"
EXCLUDED_UNKNOWN = "unknown"

# July cutover, matching `fpl_cli.season.get_season_year`: a month at or after
# July belongs to the season start year, an earlier month to the year after.
_CUTOVER_MONTH = 7

# How far into the past a season-resolved return date may sit before it is read
# as next season's instead. Half a year is comfortably longer than any date FPL
# has actually let lapse, and comfortably shorter than the 12-month wrap.
_NEXT_SEASON_LOOKBACK_DAYS = 183

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

    @property
    def mapped_gameweek(self) -> int | None:
        """The gameweek this return lands in, or None when it has no usable one.

        Two ways to have none, and both matter: the date may be unknown or
        lapsed, or `gameweek_for_date` may have failed to place a date beyond
        the last deadline on hand. Ordering, the watchlist window and
        escalation all need the answer, so they read it from here rather than
        each re-deriving the pair of conditions.
        """
        return self.return_gameweek if self.has_return_date else None


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


def resolve_return_date(
    day: int,
    month: int,
    season_year: int | None = None,
    *,
    now: datetime | None = None,
) -> date | None:
    """Resolve a bare day/month against the season, or None if impossible.

    FPL states no year. Months at or after the July cutover belong to the
    season start year, earlier months to the following calendar year, so a
    February return in the 2026-27 season resolves to February 2027.

    That season-relative rule alone can land a genuine future return in the
    past: a preseason month quoted mid-season -- an ACL return given as
    "15 Aug" while it is March -- resolves to a date already been and gone.
    Anything further behind us than `_NEXT_SEASON_LOOKBACK_DAYS` is therefore
    read as next season's, because a return date FPL really did miss slips by
    days or weeks, never by half a year.
    """
    year = season_year if season_year is not None else get_season_year()
    if month < _CUTOVER_MONTH:
        year += 1
    try:
        resolved = date(year, month, day)
    except ValueError:
        # e.g. "Expected back 31 Feb" -- treated as date-unknown, not an error.
        return None
    reference = (_as_utc(now) if now is not None else datetime.now(timezone.utc)).date()
    if (reference - resolved).days <= _NEXT_SEASON_LOOKBACK_DAYS:
        return resolved
    try:
        return date(year + 1, month, day)
    except ValueError:
        # 29 Feb rolling onto a non-leap year: no such date, so date-unknown.
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
    return_date = resolve_return_date(*parsed, season_year, now=now) if parsed else None

    lapsed = False
    return_gameweek: int | None = None
    if return_date is not None:
        current_deadline = _current_deadline_date(gameweeks, now)
        # Inclusive, matching `gameweek_for_date`'s own boundary: a date on the
        # deadline that has just passed maps to a gameweek already locked, so
        # treating it as still upcoming would pin the entry there for good.
        lapsed = current_deadline is not None and return_date <= current_deadline
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
    # Optional AI-search enrichment. `enrich_stale_news_days` is how old FPL's
    # news has to be before a *dated* entry is worth re-checking; a date-unknown
    # entry is always worth it. `enrich_max_players` is the per-run query
    # ceiling, because enrichment is billed per player and the watchlist is not
    # bounded by anything the user typed.
    enrich_stale_news_days: int = 7
    enrich_max_players: int = 8
    # How the shortlist reaches the provider. `enrich_concurrency` caps the
    # queries in flight at once; `enrich_query_spacing_seconds` is the least
    # time between two query starts, which is what a per-minute quota actually
    # measures -- an in-flight cap alone still lets a shortlist arrive as a
    # burst (#184). Lower either on a lower provider tier.
    enrich_concurrency: int = 4
    enrich_query_spacing_seconds: float = 1.0


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
        enrich_stale_news_days=_setting_int(
            block, "enrich_stale_news_days", defaults.enrich_stale_news_days,
        ),
        enrich_max_players=_setting_int(
            block, "enrich_max_players", defaults.enrich_max_players,
        ),
        # Clamped rather than rejected, like every other knob: a zero cap
        # would stall the pass and a negative spacing means none.
        enrich_concurrency=max(1, _setting_int(
            block, "enrich_concurrency", defaults.enrich_concurrency,
        )),
        enrich_query_spacing_seconds=max(0.0, _setting_float(
            block, "enrich_query_spacing_seconds", defaults.enrich_query_spacing_seconds,
        )),
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
class EnrichedReturn:
    """Return intel from AI search, held beside the FPL signal (R8).

    Never merged into `ReturnSignal`: an enriched date and an FPL-stated date
    are different claims from different sources, and where both exist both are
    carried. `citations` is the provider's own source list -- what makes an
    enriched date usable for an irreversible decision (R16) rather than merely
    readable.
    """

    summary: str = ""
    return_date: date | None = None
    return_gameweek: int | None = None
    confidence: str | None = None
    citations: tuple[str, ...] = ()
    # Every producer today is the AI-search path, so this never varies in
    # practice. It is carried rather than assumed because `escalation_verdict`
    # and the JSON both report *which* source a verdict rests on, and a second
    # enrichment route would need to say so without touching either.
    source: str = SOURCE_AI_SEARCH

    @property
    def cited(self) -> bool:
        """Whether the intel arrived with at least one source citation."""
        return bool(self.citations)

    @property
    def has_intel(self) -> bool:
        """Whether the answer said anything at all worth showing."""
        return bool(self.summary or self.return_date)


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
    # AI-search intel, when the caller ran the optional enrichment pass and it
    # found something. None means it was not run, or found nothing.
    enrichment: EnrichedReturn | None = None
    # Whether this entry's return lands inside the shorter escalation window,
    # and whose date says so (R16). Precomputed rather than left to the reader,
    # because the rule differs by source: an FPL-stated date counts on its own,
    # an enriched one only when it carries a citation.
    escalation_eligible: bool = False
    escalation_basis: str | None = None


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
    # Previously tracked players who are no longer entries, and whether each
    # is back or merely filtered out. Empty unless the run diffed a snapshot.
    departures: list[RadarDeparture] = field(default_factory=list)
    # False on a first run, a corrupt snapshot, a season change, or a store
    # holding nothing older than this gameweek: there is nothing to diff
    # against, so an absent transition means "not known", not "nothing
    # changed" (R6).
    transitions_available: bool = False
    # The gameweek whose stored state this run diffed against, None when there
    # was none. Carried so a consumer can say what "changed" is measured from
    # rather than assuming it is the gameweek before this one.
    baseline_gameweek: int | None = None
    # Player id to the filter that dropped them (`EXCLUDED_BY_WINDOW` /
    # `EXCLUDED_BY_QUALITY` / `EXCLUDED_UNKNOWN`). Carried because a player
    # who left the watchlist is indistinguishable from one who returned
    # without it -- `diff_transitions` reads it, callers rarely need it.
    exclusions: dict[int, str] = field(default_factory=dict)


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
    exclusions: dict[int, str] = {}

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
            exclusions[player_id] = EXCLUDED_UNKNOWN
            continue

        signal = build_return_signal(
            player, gameweeks=gameweeks, now=now, season_year=season_year,
        )
        if not _within_window(signal, next_gw_id, cfg.window_gameweeks):
            exclusions[player_id] = EXCLUDED_BY_WINDOW
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
            exclusions[player_id] = EXCLUDED_BY_QUALITY
            continue

        eligible, basis = escalation_verdict(
            signal, None, next_gw_id=next_gw_id, config=cfg,
        )
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
            escalation_eligible=eligible,
            escalation_basis=basis,
        ))

    entries.sort(key=_entry_order)
    return RadarResult(entries=entries, exclusions=exclusions)


# ---------------------------------------------------------------------------
# Optional AI-search enrichment
# ---------------------------------------------------------------------------


ENRICHMENT_CACHE_DIRNAME = "returnee_enrichment"


def select_enrichment_shortlist(
    entries: Sequence[RadarEntry], *, config: RadarConfig | None = None,
) -> list[RadarEntry]:
    """The entries whose return timing is worth spending a query on.

    Two populations qualify: an entry FPL states no usable date for (the
    common case, and the one this radar exists to surface), and an entry whose
    date FPL last touched long enough ago to be worth re-checking. A freshly
    dated entry is left alone -- FPL has just said what it knows.

    Bounded twice over: only the watchlist is considered, and only the best
    `enrich_max_players` of it, because enrichment is billed per player while
    the watchlist is not bounded by anything the user typed. Quality decides
    the cut, since the point of the query is to act on the answer.
    """
    cfg = config or RadarConfig()
    stale_after = cfg.enrich_stale_news_days
    candidates = [
        entry for entry in entries
        if not entry.signal.has_return_date
        or (
            entry.signal.news_age_days is not None
            and entry.signal.news_age_days >= stale_after
        )
    ]
    candidates.sort(key=lambda entry: (-entry.quality.score, entry.web_name))
    return candidates[: max(0, cfg.enrich_max_players)]


def enrichment_from_response(
    content: str,
    *,
    citations: Sequence[str] = (),
    gameweeks: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> EnrichedReturn:
    """Read one provider answer into return intel. Never raises.

    The answer is asked for as a bare JSON object, but a model can wrap it in
    a fence or refuse in prose. Anything unreadable yields intel with nothing
    in it, which the caller renders as "asked, nothing found" rather than as an
    error -- and which is still worth caching, so the same empty answer is not
    bought twice in one gameweek. A date already behind the current deadline is
    dropped on the way in, so intel can never date a return into the past.
    """
    payload = _extract_json_object(content)
    summary = str(payload.get("summary", "") or "").strip()
    return_date = _parse_date(_date_text(payload.get("expected_return")))
    if return_date is not None:
        current_deadline = _current_deadline_date(gameweeks, now)
        if current_deadline is not None and return_date <= current_deadline:
            # The same lapse test the FPL-news path applies. The prompt asks for
            # an upcoming date, but a model can still answer with one the season
            # has left behind, and rendering that as a return still to come --
            # or letting it clear the escalation window -- is worse than saying
            # nothing. The summary survives; only the unusable date is dropped.
            return_date = None
    confidence = str(payload.get("confidence", "") or "").strip().lower() or None
    if confidence not in (None, "high", "medium", "low"):
        confidence = None
    return EnrichedReturn(
        summary=summary,
        return_date=return_date,
        return_gameweek=(
            gameweek_for_date(return_date, gameweeks) if return_date is not None else None
        ),
        confidence=confidence,
        citations=tuple(str(c) for c in citations if isinstance(c, str) and c.strip()),
    )


def apply_enrichment(
    entries: Sequence[RadarEntry],
    intel: Mapping[int, EnrichedReturn],
    *,
    next_gw_id: int,
    config: RadarConfig | None = None,
) -> list[RadarEntry]:
    """Attach enriched intel to the entries it belongs to, and re-judge escalation.

    The FPL signal is untouched (R8): the intel lands in its own field and both
    dates survive. Escalation is recomputed because a cited enriched date is
    exactly what can make a date-unknown entry eligible (R16); an entry with no
    intel keeps the verdict `build_radar` gave it.
    """
    cfg = config or RadarConfig()
    enriched: list[RadarEntry] = []
    for entry in entries:
        found = intel.get(entry.player_id)
        if found is None or not found.has_intel:
            enriched.append(entry)
            continue
        eligible, basis = escalation_verdict(
            entry.signal, found, next_gw_id=next_gw_id, config=cfg,
        )
        enriched.append(replace(
            entry, enrichment=found, escalation_eligible=eligible, escalation_basis=basis,
        ))
    return enriched


def enrichment_cache_path(*, gameweek: int, season: str | None = None) -> Path:
    """Where one gameweek's enriched intel is cached.

    The cache dir, not the data dir: losing it costs one re-query, not data
    (KTD5). Season and gameweek are both in the filename, so a later gameweek
    can never serve an earlier one's answers -- return timing is the one thing
    that goes stale fastest.
    """
    from fpl_cli.paths import user_cache_dir

    label = season or season_label()
    return user_cache_dir() / ENRICHMENT_CACHE_DIRNAME / f"{label}-gw{gameweek}.json"


def load_enrichment_cache(
    *, gameweek: int, season: str | None = None,
) -> dict[int, EnrichedReturn]:
    """Read this gameweek's cached intel, or an empty mapping.

    Every failure mode -- no file, unreadable file, wrong shape -- is an empty
    cache, which costs a re-query and nothing else.
    """
    try:
        raw = json.loads(enrichment_cache_path(gameweek=gameweek, season=season)
                         .read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.info("Returnee enrichment cache unreadable (%s); re-querying", exc)
        return {}

    players = raw.get("players") if isinstance(raw, Mapping) else None
    if not isinstance(players, Mapping):
        return {}

    cached: dict[int, EnrichedReturn] = {}
    for key, value in players.items():
        player_id = _as_int(key)
        if player_id <= 0 or not isinstance(value, Mapping):
            continue
        citations = value.get("citations")
        cached[player_id] = EnrichedReturn(
            summary=str(value.get("summary", "") or ""),
            return_date=_parse_date(value.get("expected_return")),
            return_gameweek=_as_optional_int(value.get("return_gameweek")),
            confidence=(str(value["confidence"]) if value.get("confidence") else None),
            citations=tuple(
                str(c) for c in citations if isinstance(c, str)
            ) if isinstance(citations, list) else (),
        )
    return cached


def save_enrichment_cache(
    intel: Mapping[int, EnrichedReturn], *, gameweek: int, season: str | None = None,
) -> None:
    """Store this gameweek's intel, empty answers included.

    An answer that found nothing is cached too: without it a second run in the
    same gameweek would pay again to be told nothing again.
    """
    payload = {
        "metadata": {"season": season or season_label(), "gameweek": gameweek},
        "players": {
            str(player_id): {
                "summary": found.summary,
                "expected_return": (
                    found.return_date.isoformat() if found.return_date else None
                ),
                "return_gameweek": found.return_gameweek,
                "confidence": found.confidence,
                "citations": list(found.citations),
            }
            for player_id, found in sorted(intel.items())
        },
    }
    # Raw UTF-8 rather than escapes, for the same reason `save_store`
    # below writes it: these files get read by a person when an answer looks
    # wrong, and a name is easier to recognise spelled the way it is spelled.
    atomic_write_text(
        enrichment_cache_path(gameweek=gameweek, season=season),
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )


def _date_text(value: Any) -> str | None:
    """The date portion of a model-supplied value, or None.

    Tolerates a full timestamp where a date was asked for, and treats the
    words a model reaches for instead of `null` as no date.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in ("null", "none", "unknown", "n/a", "tbc"):
        return None
    return text[:10]


def _extract_json_object(content: str) -> dict[str, Any]:
    """The first JSON object in a model reply, or an empty mapping.

    A fenced block, leading prose or a trailing note are all survivable; a
    reply with no object in it at all is simply no answer.
    """
    if not content:
        return {}
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(content[start:end + 1])
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Week-over-week snapshot store
# ---------------------------------------------------------------------------


SNAPSHOT_FILENAME = "returnee_snapshot.json"


def snapshot_path() -> Path:
    """Location of the week-over-week snapshot file.

    Resolved per call so an `FPL_CLI_DATA_DIR` set after import (notably from
    the `.env` the CLI loads late) is honoured; a module-level constant would
    freeze the override at import time.
    """
    return user_data_file(SNAPSHOT_FILENAME)


@dataclass(frozen=True)
class SnapshotRecord:
    """One tracked player's availability state as of the last stored run.

    `return_date` is stored even when the signal displays as date-unknown
    because it lapsed, and `lapsed` records which of the two it was. Without
    the date a later FPL update would diff as a newly stated return rather
    than a moved one; without the flag, a lapse would re-fire every week.
    `web_name` is carried so a player who has since left the player pool
    entirely can still be named in the departure list.
    """

    status: str
    chance_of_playing: int | None = None
    return_date: date | None = None
    lapsed: bool = False
    web_name: str = ""


@dataclass(frozen=True)
class RadarSnapshot:
    """One gameweek's stored watchlist state, stamped with the season.

    The season stamp is what makes keying records on season-local player id
    safe: a snapshot never survives the id reshuffle at a season boundary.
    """

    season: str
    gameweek: int
    players: dict[int, SnapshotRecord] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotStore:
    """Both stored states: the diff baseline, and this gameweek's own state.

    Storing one state made the week-over-week signal a one-shot (#225). The
    first run of gameweek N diffed correctly and then replaced gameweek N-1's
    state with its own, so every later run that gameweek -- including the
    `--enrich` run gw-prep makes -- diffed against itself and reported
    nothing changed. Keeping the current gameweek in its own slot lets every
    run in gameweek N diff against the last state stored before N.
    """

    season: str
    baseline: RadarSnapshot | None = None
    current: RadarSnapshot | None = None

    def baseline_for(self, gameweek: int) -> RadarSnapshot | None:
        """The most recent stored state from a gameweek earlier than this one.

        `current` once the gameweek has moved past it, `baseline` while it has
        not, and None when neither predates this run -- a store holding only
        this gameweek's state has nothing to say about the week before it, and
        saying so is the honest answer rather than diffing against itself.
        """
        for slot in (self.current, self.baseline):
            if slot is not None and slot.gameweek < gameweek:
                return slot
        return None

    def advanced_to(self, snapshot: RadarSnapshot) -> SnapshotStore | None:
        """This store with `snapshot` as the current gameweek's state.

        The state it displaces is promoted to baseline only when it belongs to
        an earlier gameweek. A rerun inside one gameweek therefore refreshes
        `current` and leaves the baseline alone, which is what keeps every run
        of that gameweek reporting the same transitions.

        None when `snapshot` predates a state either slot already holds:
        storing it would overwrite a later gameweek's state and could leave
        the file inverted, with a baseline newer than the current slot. The
        gameweek only moves forward in practice, so this guards a hand-edited
        or half-written file rather than an ordinary run -- and refusing here
        rather than at the call site is what keeps the ordering an invariant
        of the store: a caller cannot store an out-of-order run by forgetting
        to ask first, because `save_store` does not accept the None.
        """
        stored = [slot.gameweek for slot in (self.baseline, self.current) if slot is not None]
        if any(snapshot.gameweek < gameweek for gameweek in stored):
            return None
        displaced = self.current
        promote = displaced is not None and displaced.gameweek < snapshot.gameweek
        return SnapshotStore(
            season=snapshot.season,
            baseline=displaced if promote else self.baseline,
            current=snapshot,
        )


def load_store(*, season: str | None = None) -> SnapshotStore | None:
    """Load the stored states, or None when there is nothing usable.

    None covers all four ways a run can have no history at all: no file yet,
    an unreadable or truncated one, a payload whose shape does not match, and
    one stamped with a different season. Each is a first run, not an error --
    the radar's deltas are a convenience layered over output that stands on
    its own.

    A file in the pre-#225 single-slot shape loads as the `current` slot: it
    holds the last state a run stored, and the gameweek before it was never
    kept. So the first run under the new shape has a baseline again from the
    next gameweek, without discarding what is there.
    """
    expected = season or season_label()
    try:
        raw = json.loads(snapshot_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.info("Returnee snapshot unreadable (%s); treating this as a first run", exc)
        return None

    if not isinstance(raw, Mapping):
        return None
    meta = raw.get("metadata")
    if not isinstance(meta, Mapping):
        return None
    if meta.get("season") != expected:
        logger.info(
            "Returnee snapshot stale (season %s != %s)", meta.get("season"), expected,
        )
        return None

    if "current" in raw or "baseline" in raw:
        baseline = _slot_from_payload(raw.get("baseline"), season=expected)
        current = _slot_from_payload(raw.get("current"), season=expected)
    else:
        baseline = None
        current = _slot_from_payload(
            {"gameweek": meta.get("gameweek"), "players": raw.get("players")},
            season=expected,
        )
    if baseline is None and current is None:
        return None
    return SnapshotStore(season=expected, baseline=baseline, current=current)


def _slot_from_payload(payload: Any, *, season: str) -> RadarSnapshot | None:
    """One stored state, or None when the slot is absent or malformed."""
    if not isinstance(payload, Mapping):
        return None
    gameweek, players = payload.get("gameweek"), payload.get("players")
    if not isinstance(gameweek, int) or isinstance(gameweek, bool):
        return None
    if not isinstance(players, Mapping):
        return None

    records: dict[int, SnapshotRecord] = {}
    for key, value in players.items():
        player_id = _as_int(key)
        if player_id <= 0 or not isinstance(value, Mapping):
            continue
        records[player_id] = SnapshotRecord(
            status=str(value.get("status", "") or ""),
            chance_of_playing=_as_optional_int(value.get("chance")),
            return_date=_parse_date(value.get("return_date")),
            lapsed=bool(value.get("lapsed", False)),
            web_name=str(value.get("web_name", "") or ""),
        )
    return RadarSnapshot(season=season, gameweek=gameweek, players=records)


def save_store(store: SnapshotStore) -> None:
    """Write both slots atomically, so an interrupted run cannot poison the next diff."""
    payload: dict[str, Any] = {
        "metadata": {
            "season": store.season,
            # The gameweek `current` describes, mirrored into the metadata
            # because `fpl doctor` and anyone opening the file read that first.
            "gameweek": store.current.gameweek if store.current else None,
        },
        "baseline": _slot_payload(store.baseline),
        "current": _slot_payload(store.current),
    }
    # `ensure_ascii=False`: player names keep their accents rather than
    # becoming `\u00e9` escapes. The snapshot is a file a person reads and
    # diffs week to week, and the league-history ledger beside it already
    # writes raw UTF-8 -- two generated files should not disagree on it.
    atomic_write_text(
        snapshot_path(), json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )


def _slot_payload(snapshot: RadarSnapshot | None) -> dict[str, Any] | None:
    """One stored state as plain JSON, or null for an empty slot."""
    if snapshot is None:
        return None
    return {
        "gameweek": snapshot.gameweek,
        "players": {
            str(player_id): {
                "status": record.status,
                "chance": record.chance_of_playing,
                "return_date": record.return_date.isoformat() if record.return_date else None,
                "lapsed": record.lapsed,
                "web_name": record.web_name,
            }
            for player_id, record in sorted(snapshot.players.items())
        },
    }


def snapshot_from_entries(
    entries: Sequence[RadarEntry], *, gameweek: int, season: str | None = None,
) -> RadarSnapshot:
    """Capture the current watchlist as the state a later gameweek diffs against.

    Only the entries are stored: a player the quality bar or the window has
    always excluded is not tracked, so they can never be reported as having
    dropped off a list they were never on.
    """
    return RadarSnapshot(
        season=season or season_label(),
        gameweek=gameweek,
        players={
            entry.player_id: SnapshotRecord(
                status=entry.status,
                chance_of_playing=entry.chance_of_playing,
                return_date=entry.signal.return_date,
                lapsed=entry.signal.lapsed,
                web_name=entry.web_name,
            )
            for entry in entries
        },
    )


# ---------------------------------------------------------------------------
# Week-over-week transitions
# ---------------------------------------------------------------------------


# Markers carried on a surviving entry.
TRANSITION_NEWLY_FLAGGED = "newly-flagged"
TRANSITION_CHANCE_IMPROVED = "chance-improved"
TRANSITION_CHANCE_WORSENED = "chance-worsened"
TRANSITION_NEWLY_DATED = "newly-dated"
TRANSITION_DATE_EARLIER = "date-moved-earlier"
TRANSITION_DATE_LATER = "date-moved-later"
TRANSITION_DATE_LAPSED = "date-lapsed"
TRANSITION_DATE_WITHDRAWN = "date-withdrawn"
# Markers carried on a player who left the watchlist, which no surviving entry
# can hold.
TRANSITION_NOW_AVAILABLE = "now-available"
TRANSITION_DROPPED = "dropped-from-watchlist"

# One entry carries one marker, so several simultaneous moves are ranked.
# Improvements outrank deteriorations because they are the actionable trigger
# (KD4), and a date beats a chance because it is the more specific claim.
_TRANSITION_PRIORITY: tuple[str, ...] = (
    TRANSITION_DATE_EARLIER,
    TRANSITION_NEWLY_DATED,
    TRANSITION_CHANCE_IMPROVED,
    TRANSITION_DATE_LAPSED,
    TRANSITION_DATE_WITHDRAWN,
    TRANSITION_DATE_LATER,
    TRANSITION_CHANCE_WORSENED,
)


@dataclass(frozen=True)
class RadarDeparture:
    """A previously tracked player who is no longer on the watchlist.

    Three very different things look identical from the entry list alone: the
    player is fit again, the window pushed their return out of range, or the
    quality bar stopped clearing them. `transition` separates the first from
    the other two and `reason` names which filter did it.
    """

    player_id: int
    web_name: str
    status: str
    transition: str
    reason: str | None = None


def diff_transitions(
    entries: Sequence[RadarEntry],
    *,
    snapshot: RadarSnapshot | None,
    players: Sequence[Any] = (),
    exclusions: Mapping[int, str] | None = None,
) -> tuple[list[RadarEntry], list[RadarDeparture]]:
    """Mark entries against the last stored run and list who left it.

    Pure: does no I/O of its own. With no snapshot (a first run, a corrupt
    file, a season change) every transition stays unset and no departure is
    reported -- R6's degrade to snapshot-only output.

    Args:
        entries: This run's watchlist.
        snapshot: The state the last stored run left behind, or None.
        players: The full current player pool, needed to tell a tracked
            player who is fit again from one a filter excluded.
        exclusions: Player id to the filter that dropped them
            (`EXCLUDED_BY_WINDOW` / `EXCLUDED_BY_QUALITY`), as `build_radar`
            recorded it.
    """
    if snapshot is None:
        return list(entries), []

    previous = snapshot.players
    marked = [
        replace(entry, transition=_entry_transition(entry, previous.get(entry.player_id)))
        for entry in entries
    ]

    current_ids = {entry.player_id for entry in entries}
    pool = {_as_int(read_player_field(p, "id")): p for p in players}
    reasons = exclusions or {}
    departures = [
        _departure(player_id, previous[player_id], pool.get(player_id), reasons)
        for player_id in previous
        if player_id not in current_ids
    ]
    departures.sort(key=lambda d: (d.web_name, d.player_id))
    return marked, departures


def _entry_transition(entry: RadarEntry, record: SnapshotRecord | None) -> str | None:
    """The single most actionable move this entry made since the last run."""
    if record is None:
        return TRANSITION_NEWLY_FLAGGED
    candidates = (
        _date_transition(entry.signal, record),
        _chance_transition(entry.chance_of_playing, record.chance_of_playing),
    )
    found = [marker for marker in candidates if marker is not None]
    if not found:
        return None
    return min(found, key=_TRANSITION_PRIORITY.index)


def _date_transition(signal: ReturnSignal, record: SnapshotRecord) -> str | None:
    """Compare stated return dates, lapsed ones included.

    The comparison is on `return_date` rather than `has_return_date`, because
    a lapsed date is still the date FPL last stated: an update that follows it
    is a moved return, not a newly stated one.
    """
    before, after = record.return_date, signal.return_date
    if before is None:
        return TRANSITION_NEWLY_DATED if after is not None else None
    if after is None:
        return TRANSITION_DATE_WITHDRAWN
    if after < before:
        return TRANSITION_DATE_EARLIER
    if after > before:
        return TRANSITION_DATE_LATER
    # Same date as last run: the only thing that can have changed is whether
    # it has now been missed, which fires once rather than every week after.
    return TRANSITION_DATE_LAPSED if signal.lapsed and not record.lapsed else None


def _chance_transition(current: int | None, previous: int | None) -> str | None:
    """Compare chance of playing, when both runs actually stated one."""
    if current is None or previous is None or current == previous:
        return None
    return TRANSITION_CHANCE_IMPROVED if current > previous else TRANSITION_CHANCE_WORSENED


def _departure(
    player_id: int,
    record: SnapshotRecord,
    player: Any | None,
    exclusions: Mapping[int, str],
) -> RadarDeparture:
    """Resolve a tracked player who is no longer an entry against live status.

    Only status `a` is a return. Anything else is still flagged, so the run
    reports which filter excluded them -- reporting a slipped return as a
    return would be worse than saying nothing.
    """
    status = _status_code(player) if player is not None else record.status
    name = str(read_player_field(player, "web_name", "") or "") if player is not None else ""
    if status == "a":
        transition, reason = TRANSITION_NOW_AVAILABLE, None
    else:
        transition = TRANSITION_DROPPED
        reason = exclusions.get(player_id, EXCLUDED_UNKNOWN)
    return RadarDeparture(
        player_id=player_id,
        web_name=name or record.web_name,
        status=status,
        transition=transition,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# The entry point a caller wants: watchlist plus week-over-week deltas
# ---------------------------------------------------------------------------


def run_radar(
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
    persist: bool = True,
) -> RadarResult:
    """Build the watchlist, diff it against the last stored run, and store it.

    The one call a command wants: `build_radar` for the entries, the snapshot
    store for the deltas. Arguments are `build_radar`'s, plus:

    Args:
        persist: Whether this run may become the state next week diffs
            against. A run that widened the filters (an `--all` style bypass)
            should pass False: storing its larger watchlist would make the
            next ordinary run report everyone it re-excluded as having
            dropped off the list.

    Every run in a gameweek diffs against the last state stored *before* it
    and refreshes the store's current slot, so a second run inside a gameweek
    reports the same transitions as the first rather than diffing against what
    the first just wrote (#225).
    """
    result = build_radar(
        players,
        priors=priors,
        next_gw_id=next_gw_id,
        gameweeks=gameweeks,
        config=config,
        profiles=profiles,
        understat_seasons=understat_seasons,
        team_names=team_names,
        now=now,
        season_year=season_year,
    )
    if result.degraded:
        # The quality bar could not run, so this run's empty watchlist says
        # nothing about who is flagged. Storing it would erase the last real
        # one and report the whole watchlist as newly flagged next week.
        return result

    season = season_label(season_year)
    store = load_store(season=season) or SnapshotStore(season=season)
    baseline = store.baseline_for(next_gw_id)
    entries, departures = diff_transitions(
        result.entries,
        snapshot=baseline,
        players=players,
        exclusions=result.exclusions,
    )
    # None when this run is older than a stored state, which `advanced_to`
    # refuses rather than invert the store over.
    updated = (
        store.advanced_to(
            snapshot_from_entries(result.entries, gameweek=next_gw_id, season=season),
        )
        if persist
        else None
    )
    if updated is not None:
        try:
            save_store(updated)
        except OSError as exc:
            # The watchlist stands on its own; losing the write costs next
            # week's deltas, not this week's output.
            logger.warning("Could not store the returnee snapshot: %s", exc)

    return replace(
        result,
        entries=entries,
        departures=departures,
        transitions_available=baseline is not None,
        baseline_gameweek=baseline.gameweek if baseline is not None else None,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _entry_order(entry: RadarEntry) -> tuple[int, int, float, str]:
    """Near-term returns first, date-unknown last, quality breaking ties."""
    known = entry.signal.mapped_gameweek is not None
    return (
        0 if known else 1,
        entry.signal.return_gameweek or 0,
        -entry.quality.score,
        entry.web_name,
    )


def escalation_verdict(
    signal: ReturnSignal,
    enrichment: EnrichedReturn | None,
    *,
    next_gw_id: int,
    config: RadarConfig,
) -> tuple[bool, str | None]:
    """Whether this return lands inside the escalation window, and on whose date.

    The escalation window is the shorter one that separates "worth watching"
    from "worth holding a squad place for". An FPL-stated date counts on its
    own. An enriched date counts only when it carries a source citation (R16):
    enrichment is the common route by which a date-unknown player acquires a
    date at all, and the action it unlocks -- dropping a player to claim a
    returnee -- cannot be taken back. Uncited intel still renders; it just
    does not decide anything.

    A gameweek before `next_gw_id` is already behind us and never qualifies,
    which keeps a stale enriched date out without consulting the clock.
    """
    limit = next_gw_id + config.stash_window_gameweeks - 1
    if _inside(signal.mapped_gameweek, next_gw_id, limit):
        return True, SOURCE_FPL_NEWS
    if (
        enrichment is not None
        and enrichment.cited
        and _inside(enrichment.return_gameweek, next_gw_id, limit)
    ):
        return True, enrichment.source
    return False, None


def _inside(gameweek: int | None, first: int, last: int) -> bool:
    """Whether a gameweek is known and falls in the inclusive range."""
    return gameweek is not None and first <= gameweek <= last


def _within_window(signal: ReturnSignal, next_gw_id: int, window_gameweeks: int) -> bool:
    """Whether a signal falls inside the watchlist window.

    A date-unknown signal (which includes a lapsed one) is always inside it:
    R4 keeps those on the list precisely because nobody knows when they are
    back, and they are the majority of flagged players.
    """
    gameweek = signal.mapped_gameweek
    if gameweek is None:
        return True
    return gameweek <= next_gw_id + window_gameweeks - 1


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

    Defensive contribution and the GK signal block come from the season row
    when its source published them, and otherwise shrink the ceiling to the
    headroom the row can reach — see `_historical_defensive_signals` (#132).
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

    signals, missing = _historical_defensive_signals(season, position)
    data.update(signals)

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
    if missing:
        ceiling *= ceiling_attainability(weights, missing)
    mins_factor = calculate_mins_factor(minutes, appearances, TOTAL_GAMEWEEKS)
    raw = calculate_player_quality_score(
        evaluation.as_quality_dict(), weights, mins_factor, position=position,
    )
    return normalise_score(raw, ceiling)


def _historical_defensive_signals(
    season: SeasonHistory, position: Position,
) -> tuple[dict[str, float], set[str]]:
    """The DC/GK signals a past season supplies, and the weight terms it cannot.

    These four terms are the whole of what the DEF and GK weight variants
    activate beyond form and ppg, and until #132 no historical season could
    supply any of them: every one was read as 0 and the two positions were
    scored against ceilings they structurally could not reach. Scored over the
    real 2025-26 season, the best defender in the league read 77 and the best
    keeper 45, both under the 0.80 watchlist bar — no defender and no keeper
    could ever reach the list, however good their last healthy season.

    Core-Insights publishes all four pre-computed per 90 on the same season row
    the aggregates already come from, so a season inside its window scores
    against its real ceiling. Outside that window — vaastav's older seasons, or
    a season predating defensive contribution upstream — the missing terms
    shrink the ceiling instead, which is what the returned set is for. The
    result then reads "how good was this season, on the signals we have", the
    same mechanism #143 gave sample-ramped keepers.

    No sample ramp: `_last_healthy_season` only returns seasons at or above
    `MIN_MINUTES`, which is exactly `GK_SAMPLE_RAMP_MINUTES`, so every season
    reaching here would ramp at 1.0 anyway.
    """
    signals: dict[str, float] = {}
    missing: set[str] = set()

    if season.defensive_contribution_per_90 is None:
        missing.add("dc_per_90")
    else:
        signals["dc_per_90"] = season.defensive_contribution_per_90

    if position != "GK":
        # dc_per_90 is the only one of the four any other variant weights, so
        # the GK block would only ever add zero-cap terms to `missing`.
        return signals, missing

    if season.saves_per_90 is None:
        missing.add("gk_saves_per_90")
    else:
        signals["gk_saves_per_90"] = season.saves_per_90

    if season.clean_sheets_per_90 is None:
        missing.add("gk_cs_rate")
    else:
        # The live path divides clean sheets by appearances; over a whole
        # season a keeper's minutes are ~90 per start, so the published
        # per-90 rate is the same quantity without needing the raw counts.
        signals["gk_cs_rate"] = season.clean_sheets_per_90

    if season.expected_goals_conceded_per_90 is None:
        missing.add("gk_xgc_quality")
    else:
        signals["gk_xgc_quality"] = gk_xgc_quality(season.expected_goals_conceded_per_90)

    return signals, missing


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
    player's *current* club: the season's `team_id` is the FPL club code and
    no team map reaches here to resolve it to a name, so a player who has
    since moved simply fails to match and loses the xG sharpening.
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

    Ranks through `player_prior.percentile_rank`, so the last-resort bar and
    the prior's own price fallback stay the same measure rather than two
    copies of one formula.
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

    return {
        player_id: percentile_rank(price, by_position[position])
        for player_id, (position, price) in prices.items()
    }


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


def _setting_number(
    block: Mapping[str, Any], key: str, default: _NumberT, cast: Callable[[Any], _NumberT],
) -> _NumberT:
    """Read one numeric setting, falling back on anything unusable.

    Shared by the int and float readers so a tightening of what counts as
    usable applies to every knob at once rather than half of them.
    """
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return cast(value)


def _setting_int(block: Mapping[str, Any], key: str, default: int) -> int:
    """Read one integer setting, falling back on anything unusable."""
    return _setting_number(block, key, default, int)


def _setting_float(block: Mapping[str, Any], key: str, default: float) -> float:
    """Read one float setting, falling back on anything unusable."""
    return _setting_number(block, key, default, float)


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


def _as_optional_int(value: Any) -> int | None:
    """Coerce a stored number to int, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _parse_date(value: Any) -> date | None:
    """Coerce a stored ISO date to a date, or None when it is unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
