"""How much of the player pool each scoring feed actually reached (#438).

Every tripwire the feeds had before this fired on total loss: a header
check, a zero-rows check, a club no Understat row carries. A feed that
reaches half the pool passes all of them, and a field that reads 0 where it
should read "unknown" passes them too, so every score degrades and nothing
says so. #431 was that shape: Core-Insights kept two gameweeks of match
records, enough to stay clear of the zero-records warning and too few for
any player to reach the adjusted-npxG minimum, and every command scored on
raw npxG for a month.

Measuring the outcome instead of predicting the trigger catches every such
case from one place: for each signal `prepare_scoring_data` builds, the
share of the players that could have carried it and did. A share under the
signal's floor becomes a `metadata.warnings` entry, which is the channel a
`--format json` consumer (gw-prep among them) actually reads; a
`logger.warning` on stderr never reaches one, since fpl-cli configures no
logging handlers and the skill parses stdout.

The floors sit well under a healthy run and well above a collapse. The early
season is handled by what counts as eligible rather than by a calendar:
nobody has four appearances the match files could carry before GW6, so the
adjusted-npxG check has nobody to measure until there is somebody it could
have reached, and Understat's own settling period keeps a lower floor until
the clubs have all been ingested.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from fpl_cli.api.understat import UNDERSTAT_NAME_MISS_CEILING, UNDERSTAT_SETTLED_GWS
from fpl_cli.services.player_prior import CUTOFF_GW, HISTORY_PRIOR_FLOOR, history_prior_reach
from fpl_cli.services.scoring.constants import SIGNAL_WINDOW_LOOKBACK_GWS
from fpl_cli.services.scoring.signals import (
    ADJUSTED_NPXG_MIN_MATCHES,
    CONSISTENCY_MIN_MATCHES,
    CONSISTENCY_MIN_MINUTES,
    match_record_window,
)

if TYPE_CHECKING:
    from fpl_cli.api.core_insights import MatchRecord
    from fpl_cli.models.player import Player
    from fpl_cli.services.player_prior import PlayerPrior
    from fpl_cli.services.team_ratings import TeamRatingsService

# `metadata.warnings` codes, one per signal, so a consumer can key on which
# feed went rather than parse prose.
UNDERSTAT_COVERAGE_LOW = "understat_coverage_low"
ADJUSTED_NPXG_COVERAGE_LOW = "adjusted_npxg_coverage_low"
CONSISTENCY_COVERAGE_LOW = "consistency_coverage_low"
HISTORY_PRIOR_COVERAGE_LOW = "history_prior_coverage_low"
GW_HISTORY_COVERAGE_LOW = "gw_history_coverage_low"
FIXTURE_RATINGS_COVERAGE_LOW = "fixture_ratings_coverage_low"

# Below this many eligible players a share says more about the sample than
# about the feed: `fpl stats --value --team ARS` has about twenty players
# with minutes, and the one or two the name join always misses would read
# as a tenth of the pool lost. Fixtures are judged one by one instead.
MIN_ELIGIBLE_PLAYERS = 25

# Understat: the live pool is allowed to miss the same share of names the
# `fpl doctor --providers` name-join probe tolerates, once the clubs have
# all been ingested; until then a club or two can legitimately be absent.
UNDERSTAT_COVERAGE_FLOOR = 1.0 - UNDERSTAT_NAME_MISS_CEILING
UNDERSTAT_EARLY_COVERAGE_FLOOR = 0.5

# Core-Insights: judged over the FPL per-gameweek history, the feed that
# says who played, inside the window the signal reads, and with the latest
# gameweek left out of the count. FPL writes a history row from kick-off
# while Core-Insights publishes its match files after the gameweek, so a
# player who reached the minimum only with it is not expected to be covered
# yet; a count that included it read a healthy feed as 0% all through the
# first weekend anyone was eligible. Files lagging a gameweek beyond that
# cost the players sitting exactly on the minimum, never three quarters of
# the pool.
ADJUSTED_NPXG_COVERAGE_FLOOR = 0.75
CONSISTENCY_COVERAGE_FLOOR = 0.75

# Per-GW history: every `element-summary` call should succeed, so the floor
# sits just under full coverage, where a run of throttled calls lands and a
# single transient failure does not.
GW_HISTORY_COVERAGE_FLOOR = 0.95

_DOCTOR = "run `fpl doctor --providers`"


@dataclasses.dataclass(frozen=True)
class SignalCoverage:
    """One signal's reach over the players that could have carried it."""

    code: str
    label: str
    covered: int
    eligible: int
    eligible_label: str
    floor: float
    consequence: str
    cause: str
    min_eligible: int = MIN_ELIGIBLE_PLAYERS

    @property
    def share(self) -> float:
        """The covered share, 1.0 when nobody was eligible."""
        return self.covered / self.eligible if self.eligible else 1.0

    @property
    def low(self) -> bool:
        """Whether the share falls under the floor on a sample worth judging."""
        return self.eligible >= self.min_eligible and self.share < self.floor

    def warning(self) -> dict[str, str] | None:
        """The `metadata.warnings` entry for a low share, else None."""
        if not self.low:
            return None
        return {
            "code": self.code,
            "message": (
                f"{self.label} reached {self.covered} of {self.eligible} "
                f"{self.eligible_label} ({self.share:.0%}; at least {self.floor:.0%} "
                f"expected), so {self.consequence}. {self.cause}."
            ),
        }


def coverage_warnings(coverages: Iterable[SignalCoverage]) -> list[dict[str, str]]:
    """The `metadata.warnings` entries for every low share, in the order given."""
    return [w for c in coverages if (w := c.warning()) is not None]


def understat_coverage(
    players: Sequence[Player],
    lookup: Mapping[int, Mapping[str, Any]] | None,
    *,
    next_gw_id: int,
) -> SignalCoverage:
    """Understat npxG over the players with minutes, keyed by main-game id.

    Covered means an npxG rate the quality score can read: a matched row
    whose `npxG_per_90` is None (the field gone from the payload, #438) is
    the xGI fallback for that player, the same as no match at all.
    """
    return _understat_reach(
        [p.id for p in players if p.minutes > 0], lookup, next_gw_id=next_gw_id,
    )


def understat_join_coverage(
    rows: Sequence[Mapping[str, Any]],
    lookup: Mapping[int, Mapping[str, Any]] | None,
    *,
    next_gw_id: int,
) -> SignalCoverage:
    """`understat_coverage` for a join an agent made with `fetch_understat_lookup` itself.

    *rows* are the player dicts it was handed, eligible on their `minutes`;
    *lookup* is its answer, keyed by row index. `StatsAgent` joins the rows
    it scores and `WaiverAgent` the draft pool, both outside the bundle, so
    this is how a join the bundle never saw is held to the bundle's rule.
    """
    return _understat_reach(
        [i for i, row in enumerate(rows) if (row.get("minutes") or 0) > 0],
        lookup,
        next_gw_id=next_gw_id,
    )


def _understat_reach(
    eligible: Sequence[int],
    lookup: Mapping[int, Mapping[str, Any]] | None,
    *,
    next_gw_id: int,
) -> SignalCoverage:
    found = lookup or {}
    covered = sum(
        1 for key in eligible
        if (match := found.get(key)) is not None and match.get("npxG_per_90") is not None
    )
    settled = next_gw_id > UNDERSTAT_SETTLED_GWS
    return SignalCoverage(
        code=UNDERSTAT_COVERAGE_LOW,
        label="Understat npxG",
        covered=covered,
        eligible=len(eligible),
        eligible_label="players with minutes",
        floor=UNDERSTAT_COVERAGE_FLOOR if settled else UNDERSTAT_EARLY_COVERAGE_FLOOR,
        consequence=(
            "the rest score on FPL xGI in place of npxG and xGChain, and every quality "
            "and value score built on them reads differently from a full run"
        ),
        cause=(
            "Understat may be unreachable, refusing requests or publishing a changed "
            f"payload: {_DOCTOR}"
        ),
    )


def recent_appearances(
    history: Sequence[Mapping[str, Any]],
    *,
    next_gw_id: int,
    min_minutes: int,
) -> int:
    """Rows of one player's per-gameweek *history* a match-file signal could carry.

    The window is the signal's own (`_rolling_window`: rounds above
    ``next_gw_id - SIGNAL_WINDOW_LOOKBACK_GWS``) less the latest gameweek,
    which Core-Insights has not necessarily published: FPL carries its row
    from kick-off, the match files land after the gameweek. A row without a
    readable `round` cannot be placed on the axis and is skipped, as the
    signal skips it.
    """
    oldest = next_gw_id - SIGNAL_WINDOW_LOOKBACK_GWS  # exclusive, as the signal's cutoff is
    newest = next_gw_id - 2  # inclusive: next_gw_id - 1 is the gameweek in progress or just done
    count = 0
    for row in history:
        try:
            gw = int(row["round"])
            minutes = int(row.get("minutes") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if oldest < gw <= newest and minutes >= min_minutes:
            count += 1
    return count


def _eligible_by_history(
    histories: Mapping[int, Sequence[Mapping[str, Any]]] | None,
    *,
    next_gw_id: int,
    min_matches: int,
    min_minutes: int,
) -> list[int]:
    return [
        pid for pid, history in (histories or {}).items()
        if recent_appearances(history, next_gw_id=next_gw_id, min_minutes=min_minutes)
        >= min_matches
    ]


def adjusted_npxg_coverage(
    histories: Mapping[int, Sequence[Mapping[str, Any]]] | None,
    lookup: Mapping[int, float] | None,
    *,
    next_gw_id: int,
) -> SignalCoverage:
    """Opponent-adjusted npxG over the players with enough recent appearances to carry it.

    Eligibility comes from the FPL per-gameweek history, not the season's
    `starts`: the signal is a rolling window, so a player with ten starts by
    GW12 and none since is unreachable at GW30 by design, and a season
    total counts the gameweek in progress, which the match files do not
    carry yet (`recent_appearances`). A bundle built without the history
    has nobody to judge, and says nothing.
    """
    eligible = _eligible_by_history(
        histories, next_gw_id=next_gw_id, min_matches=ADJUSTED_NPXG_MIN_MATCHES, min_minutes=1,
    )
    found = lookup or {}
    return SignalCoverage(
        code=ADJUSTED_NPXG_COVERAGE_LOW,
        label="Opponent-adjusted npxG",
        covered=sum(1 for pid in eligible if pid in found),
        eligible=len(eligible),
        eligible_label=f"players with {ADJUSTED_NPXG_MIN_MATCHES}+ recent appearances",
        floor=ADJUSTED_NPXG_COVERAGE_FLOOR,
        consequence="the rest score on raw Understat npxG with no fixture adjustment",
        cause=(
            "Core-Insights per-gameweek match files may be missing, lagging or "
            f"publishing a changed format: {_DOCTOR}"
        ),
    )


def consistency_coverage(
    histories: Mapping[int, Sequence[Mapping[str, Any]]] | None,
    match_records: Mapping[int, Sequence[MatchRecord]] | None,
    *,
    next_gw_id: int,
) -> SignalCoverage:
    """The match files' half of the consistency signals, over the players they could reach.

    The FPL history alone gives every player with six recent appearances a
    blank rate and a raw CV, so an entry in the consistency lookup says
    nothing about Core-Insights. What the match files add is the
    opponent-adjusted CV and floor, the involvement rate for attackers and
    the save consistency for keepers, none of which has a fallback, and a
    player has them when the files give a full window: six matches of 60+
    minutes inside the lookback (`match_record_window`). Eligible is the
    same rule read off the FPL history, the latest gameweek aside.
    """
    eligible = _eligible_by_history(
        histories,
        next_gw_id=next_gw_id,
        min_matches=CONSISTENCY_MIN_MATCHES,
        min_minutes=CONSISTENCY_MIN_MINUTES,
    )
    records = match_records or {}
    covered = sum(
        1 for pid in eligible
        if len(match_record_window(list(records.get(pid, ())), next_gw_id))
        >= CONSISTENCY_MIN_MATCHES
    )
    return SignalCoverage(
        code=CONSISTENCY_COVERAGE_LOW,
        label="Consistency signals",
        covered=covered,
        eligible=len(eligible),
        eligible_label=(
            f"players with {CONSISTENCY_MIN_MATCHES}+ recent appearances of "
            f"{CONSISTENCY_MIN_MINUTES}+ minutes"
        ),
        floor=CONSISTENCY_COVERAGE_FLOOR,
        consequence=(
            "the rest score consistency from the FPL history alone: no opponent "
            "adjustment, and no involvement rate for attackers or save consistency "
            "for keepers"
        ),
        cause=(
            "Core-Insights per-gameweek match files may be missing, lagging or "
            f"publishing a changed format: {_DOCTOR}"
        ),
    )


def history_prior_coverage(
    players: Sequence[Player],
    priors: Mapping[int, PlayerPrior] | None,
    *,
    next_gw_id: int,
) -> SignalCoverage:
    """Last season's history over the pool, before the prior cutoff only.

    From the cutoff the prior carries no weight, so a missing one costs
    nothing and nobody is eligible. Before it, the share is judged over the
    whole set because the prior is a within-position percentile over the
    whole pool: about half of it has a qualifying previous season (48% on
    2026-10-17, 63% of the players with minutes), and `HISTORY_PRIOR_FLOOR`
    sits well under that. *priors* is the set as the loader returned it,
    before `informed_priors` gates it for scoring, so the entry carries the
    real reach (a half-joined file reads as 133 of 667, not 0 of 667) and a
    consumer can tell a dataset outage, which alone reads as nobody, from a
    partial join. From GW6 this is the one place the gap is named, the
    early-season notice having stopped caveating the scores.
    """
    if next_gw_id >= CUTOFF_GW:
        covered, eligible = 0, 0
    elif priors is None:
        covered, eligible = 0, len(players)
    else:
        covered, eligible = history_prior_reach(priors)
    return SignalCoverage(
        code=HISTORY_PRIOR_COVERAGE_LOW,
        label="Last season's history",
        covered=covered,
        eligible=eligible,
        eligible_label="player priors",
        floor=HISTORY_PRIOR_FLOOR,
        consequence=(
            f"until GW{CUTOFF_GW} every prior-blended score is pure observation rather "
            "than a blend with last season's pedigree"
        ),
        cause=(
            "the historical datasets may be unreachable, or last season's file may be "
            f"missing or publishing blank minutes: {_DOCTOR}"
        ),
    )


def gw_history_coverage(
    players: Sequence[Player],
    histories: Mapping[int, Sequence[Any]] | None,
) -> SignalCoverage:
    """Per-gameweek history over the players it was fetched for.

    A player with minutes has at least one gameweek row, so an empty list
    counts as missing too: it is what a renamed `history` key would leave.
    """
    eligible = [p for p in players if p.minutes > 0]
    found = histories or {}
    return SignalCoverage(
        code=GW_HISTORY_COVERAGE_LOW,
        label="Per-gameweek history",
        covered=sum(1 for p in eligible if found.get(p.id)),
        eligible=len(eligible),
        eligible_label="players with minutes",
        floor=GW_HISTORY_COVERAGE_FLOOR,
        consequence=(
            "the rest score with a neutral form trajectory and xGI sustainability "
            "and without a consistency signal"
        ),
        cause=(
            "the FPL API may be throttling or failing some of the per-player "
            "element-summary calls: re-run, and if it persists " + _DOCTOR
        ),
    )


def fixture_ratings_coverage(
    fixtures: Sequence[Any],
    team_map: Mapping[int, Any],
    ratings_service: TeamRatingsService,
) -> SignalCoverage:
    """Rated clubs over the next gameweek's fixtures.

    `get_positional_fdr` serves a neutral 4.0 for any fixture with an unrated
    side, whether the ratings file is missing, from another season or short
    of a club. `get_staleness_warning` already diagnoses which, so its text
    rides along as the remedy; the scoring commands never read it (#438).
    """
    def rated(team_id: int) -> bool:
        team = team_map.get(team_id)
        return team is not None and ratings_service.get_rating(team.short_name) is not None

    covered = sum(1 for f in fixtures if rated(f.home_team_id) and rated(f.away_team_id))
    staleness = ratings_service.get_staleness_warning() if len(fixtures) > covered else None
    remedy = (
        staleness.removeprefix("⚠️").strip().rstrip(".")
        if isinstance(staleness, str) and staleness
        else "run `fpl ratings update`"
    )
    return SignalCoverage(
        code=FIXTURE_RATINGS_COVERAGE_LOW,
        label="Team ratings",
        covered=covered,
        eligible=len(fixtures),
        eligible_label="next-gameweek fixtures",
        floor=1.0,
        consequence="the rest score a neutral 4.0 fixture difficulty for every position",
        cause=remedy,
        min_eligible=1,
    )
