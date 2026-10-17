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
nobody has four starts before GW5, so the adjusted-npxG check has nobody to
measure until there is somebody it could have reached, and Understat's own
settling period keeps a lower floor until the clubs have all been ingested.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from fpl_cli.api.understat import UNDERSTAT_NAME_MISS_CEILING, UNDERSTAT_SETTLED_GWS
from fpl_cli.services.player_prior import CUTOFF_GW, HISTORY_PRIOR_FLOOR
from fpl_cli.services.scoring.signals import (
    ADJUSTED_NPXG_MIN_MATCHES,
    CONSISTENCY_MIN_MATCHES,
)

if TYPE_CHECKING:
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

# Core-Insights: a player with this many starts has at least that many
# matches with minutes, so the lookup could have reached them. One
# gameweek's files missing upstream (ordinary publishing lag) costs the
# players sitting exactly on the minimum, never three quarters of the pool.
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
    eligible = [p for p in players if p.minutes > 0]
    found = lookup or {}
    covered = sum(
        1 for p in eligible
        if (match := found.get(p.id)) is not None and match.get("npxG_per_90") is not None
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


def adjusted_npxg_coverage(
    players: Sequence[Player],
    lookup: Mapping[int, float] | None,
) -> SignalCoverage:
    """Opponent-adjusted npxG over the players with enough starts to carry it."""
    eligible = [p for p in players if p.starts >= ADJUSTED_NPXG_MIN_MATCHES]
    found = lookup or {}
    return SignalCoverage(
        code=ADJUSTED_NPXG_COVERAGE_LOW,
        label="Opponent-adjusted npxG",
        covered=sum(1 for p in eligible if p.id in found),
        eligible=len(eligible),
        eligible_label=f"players with {ADJUSTED_NPXG_MIN_MATCHES}+ starts",
        floor=ADJUSTED_NPXG_COVERAGE_FLOOR,
        consequence="the rest score on raw Understat npxG with no fixture adjustment",
        cause=(
            "Core-Insights per-gameweek match files may be missing, lagging or "
            f"publishing a changed format: {_DOCTOR}"
        ),
    )


def consistency_coverage(
    players: Sequence[Player],
    lookup: Mapping[int, Any] | None,
) -> SignalCoverage:
    """Consistency signals over the players with enough starts to carry them."""
    eligible = [p for p in players if p.starts >= CONSISTENCY_MIN_MATCHES]
    found = lookup or {}
    return SignalCoverage(
        code=CONSISTENCY_COVERAGE_LOW,
        label="Consistency signals",
        covered=sum(1 for p in eligible if p.id in found),
        eligible=len(eligible),
        eligible_label=f"players with {CONSISTENCY_MIN_MATCHES}+ starts",
        floor=CONSISTENCY_COVERAGE_FLOOR,
        consequence="the rest score with neutral consistency",
        cause=(
            "Core-Insights match files and the FPL per-gameweek history both feed "
            f"this, so one or both are short: {_DOCTOR}"
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
    whole pool because the prior is a within-position percentile over the
    whole pool: about half of it has a qualifying previous season (48% on
    2026-10-17, 63% of the players with minutes), and `HISTORY_PRIOR_FLOOR`
    sits well under that. The loader already returns None for a set under
    the floor, so a run that gets priors at all passes here; what this adds
    is the entry naming the gap from GW6, when the early-season notice has
    stopped caveating the scores.
    """
    eligible = list(players) if next_gw_id < CUTOFF_GW else []
    found = priors or {}
    return SignalCoverage(
        code=HISTORY_PRIOR_COVERAGE_LOW,
        label="Last season's history",
        covered=sum(1 for p in eligible if (prior := found.get(p.id)) and prior.source == "history"),
        eligible=len(eligible),
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
        staleness.lstrip("⚠️ ").rstrip(".")
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
