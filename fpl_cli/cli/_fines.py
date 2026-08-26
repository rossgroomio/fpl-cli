"""Config-driven fines computation for FPL leagues."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NotRequired, Protocol, TypedDict

from fpl_cli.cli._fines_config import FineRule, FinesConfig
from fpl_cli.models.player import FORMATION_LIMITS


class WorstPerformer(TypedDict):
    is_user: bool
    points: int
    gross_points: int
    name: str


class FinesLeagueData(TypedDict):
    user_gw_points: NotRequired[int]
    user_gw_net_points: NotRequired[int]
    worst_performers: NotRequired[list[WorstPerformer]]


class FinesTeamPlayer(TypedDict):
    name: str
    red_cards: int
    contributed: bool
    auto_sub_out: bool


class _RuleHandler(Protocol):
    def __call__(
        self, rule: FineRule, league_data: FinesLeagueData | None,
        team_data: list[FinesTeamPlayer], use_net_points: bool,
    ) -> FineResult: ...


@dataclass(frozen=True)
class FineResult:
    """Result of evaluating a single fine rule."""

    rule_type: str
    triggered: bool
    message: str


def _no_league_data(rule: FineRule) -> FineResult:
    return FineResult(rule_type=rule.type, triggered=False, message="No league data available.")


def rules_for_format(config: FinesConfig, format_name: str) -> list[FineRule]:
    """The rules configured for one format, in config order."""
    return list(config.classic if format_name == "classic" else config.draft)


def evaluate_rules(
    rules: list[FineRule],
    league_data: FinesLeagueData | None,
    team_data: list[FinesTeamPlayer],
    *,
    use_net_points: bool = False,
) -> list[FineResult]:
    """Evaluate an explicit rule list, one result per rule.

    Split out from `evaluate_fines` so a caller that can only rule *some* of
    the configured rules -- the coarse ledger backfill, which has no squad --
    can narrow the list up front. Narrowing matters: every handler returns a
    result either way, and a red-card handler handed an empty squad returns
    "no red card fine", which is a false acquittal rather than an abstention.
    """
    return [
        _RULE_HANDLERS[rule.type].evaluate(rule, league_data, team_data, use_net_points)
        for rule in rules
    ]


def evaluate_fines(
    config: FinesConfig,
    format_name: str,
    league_data: FinesLeagueData | None,
    team_data: list[FinesTeamPlayer],
    *,
    use_net_points: bool = False,
) -> list[FineResult]:
    """Evaluate all fine rules for a given format.

    Returns a list of FineResult for each configured rule.
    """
    return evaluate_rules(
        rules_for_format(config, format_name), league_data, team_data,
        use_net_points=use_net_points,
    )


def _eval_last_place(
    rule: FineRule,
    league_data: FinesLeagueData | None,
    team_data: list[FinesTeamPlayer],  # noqa: ARG001
    use_net_points: bool,
) -> FineResult:
    if not league_data or not league_data.get("worst_performers"):
        return _no_league_data(rule)

    worst = league_data.get("worst_performers", [])
    pts_field = "points" if use_net_points else "gross_points"
    pts_label = "net pts" if use_net_points else "pts"

    if worst[0].get("is_user", False):
        user_pts = worst[0].get(pts_field, worst[0].get("points", 0))
        return FineResult(
            rule_type=rule.type,
            triggered=True,
            message=f"FINE TRIGGERED: You finished last in the gameweek with {user_pts} {pts_label}. {rule.penalty}.",
        )

    last_name = worst[0].get("name", "Unknown")
    last_pts = worst[0].get(pts_field, worst[0].get("points", 0))
    return FineResult(
        rule_type=rule.type,
        triggered=False,
        message=f"No last-place fine. {last_name} finished bottom with {last_pts} {pts_label}.",
    )


def _eval_red_card(
    rule: FineRule,
    league_data: FinesLeagueData | None,  # noqa: ARG001
    team_data: list[FinesTeamPlayer],
    use_net_points: bool,  # noqa: ARG001
) -> FineResult:
    red_card_players = []
    if team_data:
        for p in team_data:
            if p.get("red_cards", 0) > 0 and p.get("contributed", True) and not p.get("auto_sub_out"):
                red_card_players.append(p["name"])

    if red_card_players:
        names = ", ".join(red_card_players)
        return FineResult(
            rule_type=rule.type,
            triggered=True,
            message=f"FINE TRIGGERED: Red card in your starting XI ({names}). {rule.penalty}.",
        )
    return FineResult(rule_type=rule.type, triggered=False, message="No red card fine.")


def _eval_below_threshold(
    rule: FineRule,
    league_data: FinesLeagueData | None,
    team_data: list[FinesTeamPlayer],  # noqa: ARG001
    use_net_points: bool,
) -> FineResult:
    if rule.threshold is None:
        msg = "below-threshold rule requires a threshold value"
        raise ValueError(msg)

    # An unknown score is not a zero score. Before the first standings build of
    # the season (and whenever a league fetch fails) the caller has no points to
    # hand over -- defaulting to 0 fined the user for a score nobody measured.
    if not league_data or "user_gw_points" not in league_data:
        return _no_league_data(rule)

    if use_net_points:
        user_pts = league_data.get("user_gw_net_points", league_data["user_gw_points"])
        pts_label = "net pts"
    else:
        user_pts = league_data["user_gw_points"]
        pts_label = "pts"

    if user_pts < rule.threshold:
        return FineResult(
            rule_type=rule.type,
            triggered=True,
            message=(
                f"FINE TRIGGERED: You scored {user_pts} {pts_label},"
                f" below the {rule.threshold}-point threshold. {rule.penalty}."
            ),
        )
    return FineResult(
        rule_type=rule.type,
        triggered=False,
        message=(
            f"No sub-{rule.threshold} fine. You scored {user_pts} {pts_label}"
            f" ({user_pts} >= {rule.threshold})."
        ),
    )


@dataclass(frozen=True)
class _Rule:
    """One rule type's handler, plus what it needs to be able to rule at all.

    `needs_squad` is declared here, beside the handler it describes, rather
    than in a separate list of rule types: a handler that reads `team_data`
    and a classification that says it does not are the kind of pair that
    drifts silently, and the cost of the drift is a false acquittal (a
    squad-dependent handler run against an empty squad answers "no fine")
    recorded as a real ruling (issue #136).
    """

    evaluate: _RuleHandler
    needs_squad: bool


_RULE_HANDLERS: dict[str, _Rule] = {
    "last-place": _Rule(_eval_last_place, needs_squad=False),
    "red-card": _Rule(_eval_red_card, needs_squad=True),
    "below-threshold": _Rule(_eval_below_threshold, needs_squad=False),
}

# Rule types rulable from cohort points alone -- derived from the registry
# above, so a new rule type is classified by the same edit that adds it.
# The ledger's coarse tier (the manager-history endpoint) carries no squad,
# so a backfill running at that tier can rule these and only these.
COHORT_ONLY_RULE_TYPES = frozenset(
    rule_type for rule_type, rule in _RULE_HANDLERS.items() if not rule.needs_squad
)


def compute_bench_analysis(team_data: list[dict[str, Any]]) -> str | None:
    """Compare bench players against starters with formation-aware validation."""
    starters = [
        p for p in team_data
        if p.get("contributed") and not p.get("auto_sub_in")
    ]
    bench = [
        p for p in team_data
        if not p.get("contributed")
        and not p.get("auto_sub_out")
    ]

    # Current formation counts
    formation = {}
    for s in starters:
        formation[s["position"]] = formation.get(s["position"], 0) + 1

    mistakes = []
    for bp in bench:
        outscored = []
        for s in starters:
            if bp["points"] <= s["points"]:
                continue

            bp_pos, s_pos = bp["position"], s["position"]

            # GK can only swap with GK
            if bp_pos == "GK" or s_pos == "GK":
                if bp_pos == s_pos:
                    outscored.append((s, False))
                continue

            # Same position - always valid
            if bp_pos == s_pos:
                outscored.append((s, False))
                continue

            # Cross-position - check formation validity
            new_formation = dict(formation)
            new_formation[s_pos] -= 1
            new_formation[bp_pos] = new_formation.get(bp_pos, 0) + 1

            valid = all(
                FORMATION_LIMITS[pos][0] <= new_formation.get(pos, 0) <= FORMATION_LIMITS[pos][1]
                for pos in FORMATION_LIMITS
            )
            if valid:
                outscored.append((s, True))  # True = formation change

        if outscored:
            parts = []
            for s, is_formation_change in outscored:
                entry = f"{s['name']} ({s['position']}, {s['points']})"
                if is_formation_change:
                    entry += " [formation change]"
                parts.append(entry)
            mistakes.append(
                f"- {bp['name']} ({bp['position']}, {bp['points']} pts on bench) "
                f"outscored: {', '.join(parts)}"
            )
    return "\n".join(mistakes) if mistakes else None
