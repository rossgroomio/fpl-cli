"""TypedDict contracts for league-recap data pipeline."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class RecapManagerPlayer(TypedDict):
    """A single player in a manager's GW squad."""

    name: str
    team: str
    # Full club name, for prose that has to state where a player plays. The
    # 3-letter `team` reads as a surname in a sentence (LEE is Leeds, not
    # someone called Lee). None when the club didn't resolve -- consumers omit
    # the club rather than printing a placeholder that reads like one.
    team_name: NotRequired[str | None]
    position: str
    # Stable cross-season element_code, so a recorded squad still identifies
    # its players after the API reshuffles seasonal ids. None when the
    # reference could not be resolved (always paired with `unmatched`).
    code: int | None
    points: int
    is_captain: bool
    is_vice_captain: bool
    contributed: bool
    is_bench_boost_player: bool
    auto_sub_in: bool
    auto_sub_out: bool
    red_cards: int
    # Draft only: the draft-to-main-player name/team match failed, so `points`
    # is a false zero rather than a real score. Always False for classic.
    unmatched: bool
    # Whether the player's club had a fixture this gameweek. A bench zero in a
    # blank gameweek is not a choice that failed, and a captain with no fixture
    # never contributes to a captain-blank run.
    had_fixture: bool


class RecapTransfer(TypedDict):
    """A single classic transfer made by a manager."""

    player_in: str
    player_in_team: str
    player_in_team_name: NotRequired[str | None]
    player_in_points: int
    player_in_code: NotRequired[int]
    player_out: str
    player_out_team: str
    player_out_team_name: NotRequired[str | None]
    player_out_points: int
    player_out_code: NotRequired[int]
    net: int
    cost: int


class RecapDraftTransaction(TypedDict):
    """A single draft waiver/free-agent pickup. Drops are always required in
    draft (fixed 15-player squad) so player_out fields are never null."""

    player_in: str
    player_in_team: str
    player_in_team_name: NotRequired[str | None]
    player_in_points: int
    player_in_code: NotRequired[int]
    player_out: str
    player_out_team: str
    player_out_team_name: NotRequired[str | None]
    player_out_points: int
    player_out_code: NotRequired[int]
    net: int
    kind: str


class RecapManagerEntry(TypedDict):
    """Per-manager data for one gameweek."""

    manager_name: str
    entry_id: int
    # Draft only: the league-local `league_entry` id. It is always present,
    # while `entry_id` is null for an unclaimed team -- two of which would
    # otherwise collide on the ledger key.
    league_entry_id: NotRequired[int]
    gw_points: int
    # Always gross, unlike gw_points (which flips net/gross on use_net_points).
    gross_points: int
    # Unset for a replayed gameweek where no point-in-time cumulative total
    # could be reconstructed (draft has no such source before a ledger exists).
    total_points: NotRequired[int]
    gw_rank: int
    # League position. Unset alongside total_points when it can't be derived.
    overall_rank: NotRequired[int]
    previous_rank: NotRequired[int]
    captain: str
    captain_points: int
    captain_played: bool
    vice_captain: str
    vice_captain_points: int
    active_chip: str | None
    squad: list[RecapManagerPlayer]
    bench_points: int
    transfer_cost: int
    auto_subs: list[str]
    transfers: NotRequired[list[RecapTransfer]]
    transactions: NotRequired[list[RecapDraftTransaction]]
    # Classic only: five figures the picks response's `entry_history` carries
    # and the season rollover destroys. Draft has no budget, no FPL-wide rank,
    # and acquires by waiver, so it omits all five.
    # Prices are in the repo's £0.1m units (1000 = £100.0m). `team_value` is
    # the API's `value` verbatim: squad selling value *plus* the bank, which
    # is why it is not called `squad_value` (issue #147) -- squad-only value
    # is `team_value - bank`.
    team_value: NotRequired[int]
    bank: NotRequired[int]
    # The manager's FPL-wide rank, cumulative for the season. Deliberately not
    # `overall_rank`, which on this TypedDict means league position.
    global_rank: NotRequired[int]
    # The manager's FPL-wide rank for this gameweek alone -- the API's `rank`,
    # not its `overall_rank` (issue #148).
    global_gw_rank: NotRequired[int]
    # How many transfers the API says were made. `transfers` is best-effort, so
    # this is the only way to tell an empty list apart from a manager who made
    # none -- and to detect a captured list that came back short.
    transfers_made: NotRequired[int]


class RecapAwardEntry(TypedDict):
    """A single award winner/loser.

    For transfer_genius / transfer_disaster, `value` is the post-hit aggregate
    true_net (raw transfer net minus transfer_cost). For waiver_genius /
    waiver_disaster it is the aggregate net (waivers have no hit cost). Other
    awards use `value` per their own conventions (points totals, etc.).
    """

    manager_name: str
    value: int | str
    detail: str


class RecapAwards(TypedDict, total=False):
    """Computed awards for the gameweek."""

    gw_winner: RecapAwardEntry
    gw_loser: RecapAwardEntry
    biggest_bench_haul: RecapAwardEntry
    best_captain: RecapAwardEntry
    worst_captain: RecapAwardEntry
    transfer_genius: RecapAwardEntry
    transfer_disaster: RecapAwardEntry
    waiver_genius: RecapAwardEntry
    waiver_disaster: RecapAwardEntry


class RecapFineResult(TypedDict):
    """A fine triggered for a specific manager."""

    manager_name: str
    # The manager's ledger key (classic `entry`, draft `league_entry`). Two
    # managers can share a display name, so a stored ruling is keyed rather
    # than matched back by name.
    manager_key: NotRequired[int]
    rule_type: str
    message: str


class RecapStandingsEntry(TypedDict):
    """One row of the league table, whether or not that manager was fetched.

    Both collectors fetch standings and then discard everything but the rows
    they could enrich. Capture cannot enumerate league members without the
    whole table: a manager missing from `managers` needs an unknown-status row
    rather than no row at all.
    """

    manager_key: int
    manager_name: str
    # Null for an unclaimed draft team; always set for classic.
    entry_id: int | None
    gw_points: int
    total_points: int


class LeagueRecapData(TypedDict):
    """Top-level collected_data shape for league-recap."""

    gameweek: int
    league_name: str
    fpl_format: str
    managers: list[RecapManagerEntry]
    awards: RecapAwards
    fines: NotRequired[list[RecapFineResult]]
    # Which fine rule types were ruled on for this gameweek, whether or not
    # any triggered -- stamped onto every captured row so the ledger can tell
    # "nobody was fined" apart from "nothing was ruled" (issue #136). Absent
    # when the caller never evaluated fines at all, which is not the same as
    # present-and-empty ("evaluated, nothing configured").
    fine_rules_evaluated: NotRequired[list[str]]
    # The manager keys `fine_rules_evaluated` actually holds for. A manager
    # whose own evaluation raised is absent, so their row records nothing
    # ruled rather than a rule list they were never measured against.
    # Absent entirely means the caller ruled every manager it collected.
    fines_ruled_manager_keys: NotRequired[list[int]]
    synthesis_summary: NotRequired[str]
    # The provider's own stop reason for the editorial, recorded only when it
    # was not a normal completion (#266). Present means the text above may be
    # cut off; absent means the provider either finished or said nothing.
    synthesis_stop_reason: NotRequired[str]
    # Ledger partition key and the league's own start gameweek (absent or 1
    # means it started at GW1, so there is nothing to offset or skip).
    league_id: NotRequired[int]
    league_start_event: NotRequired[int]
    # Every member of the league table, in standings order.
    standings_cohort: NotRequired[list[RecapStandingsEntry]]
    # The standings response reported members beyond the ones fetched (classic
    # pages at 50). The ledger inherits the truncation, so it must be visible.
    standings_truncated: NotRequired[bool]
    # The league's member count where the API states one; absent when the
    # standings response only reports that another page exists.
    league_size: NotRequired[int]
    # Whether the gameweek itself was blank or double, recorded on every row.
    is_bgw: NotRequired[bool]
    is_dgw: NotRequired[bool]
    # Report-surfaced League History text (U10), absent when capture could
    # not build a notes pack at all.
    league_history_phase_text: NotRequired[str]
    league_history_streak_lines: NotRequired[list[str]]
    # Season occurrence totals (issue #164). Populated only at the two
    # season milestones -- the report's Season Counts subsection is a
    # set-piece on the fines cadence, absent every other week.
    league_history_season_count_lines: NotRequired[list[str]]
    league_history_coverage_lines: NotRequired[list[str]]
    # Report-surfaced season fine tally (issue #136). All three are absent
    # together for a league with no fine rules configured and none ever
    # ruled, so the report omits the section rather than heading an empty
    # table.
    season_fines_span: NotRequired[str]
    season_fines_lines: NotRequired[list[str]]
    season_fines_coverage_lines: NotRequired[list[str]]
