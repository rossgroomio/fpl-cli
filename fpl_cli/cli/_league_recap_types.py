"""TypedDict contracts for league-recap data pipeline."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class RecapManagerPlayer(TypedDict):
    """A single player in a manager's GW squad."""

    name: str
    team: str
    position: str
    points: int
    is_captain: bool
    is_vice_captain: bool
    contributed: bool
    auto_sub_in: bool
    auto_sub_out: bool
    red_cards: int


class RecapTransfer(TypedDict):
    """A single classic transfer made by a manager."""

    player_in: str
    player_in_team: str
    player_in_points: int
    player_out: str
    player_out_team: str
    player_out_points: int
    net: int
    cost: int


class RecapDraftTransaction(TypedDict):
    """A single draft waiver/free-agent pickup."""

    player_in: str
    player_in_team: str
    player_in_points: int
    player_out: str | None
    player_out_team: str | None
    player_out_points: int | None
    net: int
    kind: str


class RecapManagerEntry(TypedDict):
    """Per-manager data for one gameweek."""

    manager_name: str
    entry_id: int
    gw_points: int
    total_points: int
    gw_rank: int
    overall_rank: int
    previous_rank: int
    captain: str
    captain_points: int
    captain_played: bool
    vice_captain: str
    active_chip: str | None
    squad: list[RecapManagerPlayer]
    bench_points: int
    transfer_cost: int
    auto_subs: list[str]
    transfers: NotRequired[list[RecapTransfer]]
    transactions: NotRequired[list[RecapDraftTransaction]]


class RecapAwardEntry(TypedDict):
    """A single award winner/loser."""

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
    rule_type: str
    message: str


class LeagueRecapData(TypedDict):
    """Top-level collected_data shape for league-recap."""

    gameweek: int
    league_name: str
    fpl_format: str
    managers: list[RecapManagerEntry]
    awards: RecapAwards
    fines: NotRequired[list[RecapFineResult]]
    synthesis_summary: NotRequired[str]
