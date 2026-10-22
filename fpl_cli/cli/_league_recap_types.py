"""TypedDict contracts for league-recap data pipeline."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

from fpl_cli.api.fpl_draft import draft_claim_priority_rank
from fpl_cli.season import TOTAL_GAMEWEEKS
from fpl_cli.utils.text import join_and, ordinal_suffix, ordinal_word

# Most managers, moves or picks one line of the recap names before it counts
# the rest instead: the awards' tie details and the Standout Players lines
# alike, so the two never name a different number side by side. A wide tie
# in a fifty-manager classic league, or a template player there, would
# otherwise read as a roll call of the league.
DETAIL_CAP = 3


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
    # The main game's element id, which the Standout Players rows emit as
    # `id` (issue #455) -- a draft pick's too, through the same match that
    # gives it `code`, never the draft game's own id (#418). None for a
    # draft pick the main game could not be matched to. Never written to the
    # ledger, which identifies a player by `code` alone.
    main_id: NotRequired[int | None]
    points: int
    # Minutes played this gameweek, from the same live stats as `points`, so
    # a Standout Players blank can say he did not play, or how little he did
    # (#455). In memory only, like `main_id`.
    minutes: NotRequired[int]
    # Draft only: what the Standout Players blanks are ordered by (#455).
    # `draft_rank` is the draft game's own ranking of the player, set before
    # the season on last season's points and FPL's view of his potential and
    # never updated, so `points_per_game` and `appearances` -- his matched
    # main-game player's season to date -- take over from GW19, once that
    # ranking has gone stale. None where the draft game sent no rank, or the
    # main game could not be matched. In memory only.
    draft_rank: NotRequired[int | None]
    points_per_game: NotRequired[float | None]
    appearances: NotRequired[int | None]
    # Draft only, in memory only (#458): the draft game's own element id, the
    # key a pick is looked up by, and how he came to the manager whose squad
    # this is -- None where the league's history could not be shown to be
    # complete (see `RecapAcquisition`). Absent unless the collector was asked
    # for it, which only the gameweek being recapped is.
    draft_element: NotRequired[int]
    acquisition: NotRequired[RecapAcquisition | None]
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


class RecapDraftLostClaim(TypedDict):
    """A waiver claim a manager submitted this gameweek and a rival won.

    Evidence that the manager was active, which the accepted-only view cannot
    carry: a manager whose whole gameweek was one claim they lost has no
    transactions at all, and reading that absence as inactivity is what named
    real people as having sat the waiver wire out (issue #329).

    Only the feed's `di` rows reach here -- the incoming player went to
    someone else. Its other denial code, `do`, means the manager's own earlier
    accepted claim had already consumed the drop, which is a conditional-chain
    cascade rather than a contest, and is never an attempt.

    No points and no net: nothing moved, so there is no swing to score. Who
    won the player is derivable from the same gameweek's accepted rows and is
    left to the consumer.
    """

    player_in: str
    player_in_team: str
    player_in_team_name: NotRequired[str | None]
    player_in_code: NotRequired[int]
    player_out: str
    player_out_team: str
    player_out_team_name: NotRequired[str | None]
    player_out_code: NotRequired[int]
    kind: str
    # The manager's own ordering of the claims they submitted that gameweek,
    # 1 being their first choice -- the scarce resource in a draft league.
    # None for a free-agent pickup, which is first-come-first-served and
    # carries no priority.
    priority: int | None


class RecapContestedClaimant(TypedDict):
    """One manager who claimed a contested player and was beaten to him."""

    manager_name: str
    kind: str
    # Their own ranking of the claim, as on `RecapDraftLostClaim`: 1 is the
    # first choice they submitted that week. None for a free-agent pickup.
    priority: int | None


class RecapContestedClaim(TypedDict):
    """One player more than one manager claimed this gameweek, and who won
    him (issue #330).

    Derived, never recorded: a lost claim means the incoming player went to
    a rival, so every player any manager lost a claim on was claimed by at
    least two, and the winner is whichever manager's accepted move brought
    him in the same gameweek. Nothing here is absent from the manager rows
    the ledger already records -- this regroups them by player, so the
    editorial can say who wanted whom instead of reading the winner's pickup
    as an unremarkable move, and the biggest race can be the week's Most
    Contested award. Built by `contested_draft_claims()`; never written to
    the report on its own, whose per-move detail stays in the awards.
    """

    player: str
    player_team: str
    player_team_name: NotRequired[str | None]
    player_code: NotRequired[int]
    # The manager whose accepted move brought him in. None when no fetched
    # manager's did -- their picks could not be fetched, or the row could not
    # be placed -- and the race is then editorial context with its winner
    # unnamed, never a Most Contested award: a race nobody can be seen to
    # have won is not one the recap can hand a headline to.
    winner: str | None
    # Every manager beaten to him, first-choice claims first, then by name.
    losers: list[RecapContestedClaimant]
    # Everyone who claimed him: the losers plus the winner where one was
    # identified, and the losers alone where not -- the recap does not count
    # a manager it cannot name. Bounded by the league's size (a draft league
    # holds at most 16), which is why no surface caps the managers a race
    # names.
    claimants: int


# The Draft API's `kind` values, translated to reader-facing labels. A `kind`
# outside this mapping (or the empty string collection stores when the API
# sent none) is an "other move" -- never folded into the waiver/free-agent
# counts a reader would check against the transactions page.
DRAFT_TRANSACTION_KIND_LABELS: dict[str, str] = {"w": "waiver", "f": "free agent"}
DRAFT_TRANSACTION_OTHER_LABEL = "other move"
# The fixed order a per-manager kind count is printed in, wherever it is
# printed: the two waiver awards and the editorial's waiver roster both read
# it, so one move is never a "waiver" on one surface and a "free agent" on
# the other (issues #146, #301).
DRAFT_TRANSACTION_LABEL_ORDER: tuple[str, ...] = (
    *DRAFT_TRANSACTION_KIND_LABELS.values(), DRAFT_TRANSACTION_OTHER_LABEL,
)


def draft_transaction_kind_label(kind: str) -> str:
    """Reader-facing label for one transaction's `kind`, "other move" for any
    value the mapping does not know -- the empty string included."""
    return DRAFT_TRANSACTION_KIND_LABELS.get(kind, DRAFT_TRANSACTION_OTHER_LABEL)


def draft_transaction_kind_counts(
    txns: list[RecapDraftTransaction],
) -> list[tuple[str, int]]:
    """Count a manager's raw draft transactions by kind label, every label in
    the fixed display order whether or not it is present. Built from the raw
    list rather than a chain-contracted one, so a headline count reflects
    every move the manager made -- including an intermediate the awards'
    Best/Worst line never names because a follow-up move replaced it the
    same gameweek."""
    counts = {label: 0 for label in DRAFT_TRANSACTION_LABEL_ORDER}
    for t in txns:
        counts[draft_transaction_kind_label(t["kind"])] += 1
    return [(label, counts[label]) for label in DRAFT_TRANSACTION_LABEL_ORDER]


def draft_transaction_kind_nets(
    txns: list[RecapDraftTransaction],
) -> list[tuple[str, int]]:
    """Each kind's net across a manager's raw draft transactions, in the
    fixed display order, for the kinds present only. A manager whose moves
    mix kinds gets these beside the net overall on the editorial's waiver
    roster, so the editorial has a figure for their waivers alone rather
    than only the combined one (issue #390)."""
    nets: dict[str, int] = {}
    for t in txns:
        label = draft_transaction_kind_label(t["kind"])
        nets[label] = nets.get(label, 0) + t["net"]
    return [(label, nets[label]) for label in DRAFT_TRANSACTION_LABEL_ORDER if label in nets]


def format_move_counts(breakdown: list[tuple[str, int]]) -> str:
    """Render a labelled count breakdown as prose: "2 transfers", "1 free
    agent", or across more than one kind "3 moves: 2 waivers, 1 free agent".
    Empty buckets are dropped. The one rendering behind the transfer and
    waiver awards' headline and the editorial's waiver roster, so a new kind
    or a pluralisation change lands in every surface at once."""
    present = [(label, count) for label, count in breakdown if count]
    labelled = ", ".join(f"{count} {label}{'s' if count != 1 else ''}" for label, count in present)
    if len(present) <= 1:
        return labelled
    return f"{sum(count for _, count in present)} moves: {labelled}"


class RecapPriorSeason(TypedDict):
    """One earlier FPL season a classic manager's entry played, as the
    manager-history endpoint's `past` list reports it (issue #131).

    FPL-wide, not this league's: the endpoint knows where the entry finished
    among every FPL manager that season and nothing about which mini-leagues
    it sat in. Only seasons actually played are listed, so a list can have
    gaps inside its span.
    """

    # The API's own label, "2024/25".
    season_name: str
    total_points: int
    # Finishing rank among every FPL manager that season.
    rank: int
    # The API's `rank_percentage`, kept as the string it sends ("4", "0.1",
    # "0.0") rather than parsed: it is display text -- "top 4%" -- and the
    # precision the API chose is the precision the FPL site shows. Absent
    # where the API sent none.
    rank_percentage: NotRequired[str]


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
    # The bench's raw points sum -- the API's own `points_on_bench`, which is
    # what the ledger records at every tier. Not what the bench cost: only
    # one keeper plays, and an outfielder can only come in for someone a
    # legal formation lets him replace, so most of it was never available.
    bench_points: int
    # How many more points the best legal XI from the same 15 would have
    # scored than the XI that counted, the doubled player held in place so
    # the multiplier is not silently reassigned (`best_xi_selection()` in
    # `_league_recap_data.py`). Zero when no reselection beats the one that
    # played, however many points the bench scored -- the Biggest Bench
    # award ranks on this, never on `bench_points` (issue #350). Never
    # written to the ledger: it is derivable from `squad`.
    best_xi_gain: int
    transfer_cost: int
    auto_subs: list[str]
    transfers: NotRequired[list[RecapTransfer]]
    transactions: NotRequired[list[RecapDraftTransaction]]
    # Draft only: claims this manager submitted and lost to a rival. Absent
    # when they lost none. Never merged into `transactions` -- that list is
    # completed moves, and the ledger, the awards and every net figure are
    # built from it.
    lost_claims: NotRequired[list[RecapDraftLostClaim]]
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
    # Classic only, and only at the league's opening gameweek (issue #131):
    # every earlier FPL season this entry played, FPL-wide, from the
    # manager-history endpoint's `past` list. Three states, all distinct:
    # absent means never fetched (every later gameweek, and draft), None
    # means the fetch failed, and an empty list is the API's own answer --
    # an entry with no prior seasons on record. The report and the prompt
    # name the last two differently, and a gameweek where every fetch
    # failed still gets its section. Never written to the ledger: it is
    # the manager's record outside this league, not the league's own
    # history, and it does not change from one gameweek to the next.
    prior_seasons: NotRequired[list[RecapPriorSeason] | None]


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
    # Draft only (issue #330): the player the most managers claimed this
    # gameweek, on a week where enough of them did to make a pile-up
    # (`MOST_CONTESTED_MIN_CLAIMANTS`) and someone can be seen to have won
    # him. `manager_name` is that manager -- joined with " and " across a tie
    # -- and `value` is how many claimed him. The detail names every manager
    # beaten to him and the priority each gave the claim.
    most_contested: RecapAwardEntry


# How the blanks are ordered, best story first. Classic: the blank that
# counted for the most managers, a template player blanking for half the
# league at once, then the lowest. Draft, where a player has one owner, by
# what his manager could expect of him: FPL's pre-season draft rank before
# GW19, and the season's points per game from GW19, when that rank has gone
# stale (#455).
StandoutBlanksOrder = Literal["managers", "draft_rank", "points_per_game"]


class RecapStandoutManager(TypedDict):
    """One manager a Standout Players line names, with the ledger's key for
    them, so two managers sharing a display name stay two."""

    manager_key: int
    manager_name: str


# How a draft player came to his owner, by the kind of the latest link in his
# chain (#458).
RecapAcquisitionKind = Literal["draft", "waiver", "free_agent", "trade"]


class RecapAcquisition(TypedDict):
    """How a draft player came to the manager whose squad he was in, as of the
    recapped gameweek (#458): the latest accepted event that brought him there,
    out of the league's own draft, transaction log and trades.

    `gameweek` is the one the move first applied to (the draft's own for a
    pick). The draft fields are set for a pick only: `draft_id` and
    `draft_gameweek` name the draft it was made in, `redraft` is whether the
    league had drafted more than once by then (so a line must say which),
    `round` and `pick` are where it fell in the draft (`overall_pick` counts
    round after round), `auto_pick` whether the draft's auto-pick made it, and
    `drafter` is the manager who did, the owner himself. Where the draft's
    picks are lost (neither the API nor a snapshot holds them) who drafted
    whom is rebuilt from the squads, and a pick then carries `round_unknown`
    with `round`, `pick`, `overall_pick` and `auto_pick` all null. `from_manager` is a
    trade's counterparty, the manager he came from; None when the league's
    managers do not name that entry.
    """

    kind: RecapAcquisitionKind
    gameweek: int
    draft_id: int | None
    draft_gameweek: int | None
    redraft: bool
    # A draft pick whose round is not known; False for every other kind.
    round_unknown: bool
    round: int | None
    pick: int | None
    overall_pick: int | None
    auto_pick: bool | None
    drafter: RecapStandoutManager | None
    from_manager: RecapStandoutManager | None


class RecapStandoutPlayer(TypedDict):
    """One player in the Standout Players section (issue #455): one of the
    week's top scorers or costliest blanks, and whose team he scored for.

    `points` are his own for the gameweek, before any captaincy -- the same
    live figure the bench haul is computed from. `counted_for` are the
    managers whose points he counted towards (an auto-sub coming on and a
    Bench Boost bench included); `benched_by` the ones who had him and left
    him on a bench that did not count. A manager whose starter never played
    and was replaced is in neither. `text` is the sentence every surface
    prints, so the report, the console and the editorial's prompt cannot
    tell one player two ways.
    """

    # The main game's element id, a draft recap's included, with the code
    # both games share beside it (#418, #419).
    id: int | None
    code: int | None
    name: str
    team: str
    team_name: str | None
    position: str
    points: int
    # None where the collector recorded no minutes.
    minutes: int | None
    red_cards: int
    # Draft only, null in classic: the pre-season draft rank and the season's
    # points per game the blanks are ordered by (`blanks_order`). Here for a
    # consumer to read; no printed line states either.
    draft_rank: int | None
    points_per_game: float | None
    counted_for: list[RecapStandoutManager]
    benched_by: list[RecapStandoutManager]
    text: str
    # Draft only (#458): how he came to his manager, which the sentence says.
    # Present on every draft row and null where it could not be established;
    # absent in classic, where a player has many owners and no such history.
    acquisition: NotRequired[RecapAcquisition | None]


class RecapStandoutPlayers(TypedDict):
    """The week's standout individual scores in the league's squads (issue
    #455), built once by `find_standout_players()` (`_league_recap_data.py`)
    from the same squads and live points the bench haul reads, and read by
    every surface."""

    # The managers each line's counts are out of: every manager fetched.
    managers: int
    # Whether those are the whole league. A league past one page of classic
    # standings, or one whose picks fetch failed for a manager, is not, and
    # its lines then never say "all" or "everyone but": a manager the recap
    # could not see may well have had the player.
    whole_league: bool
    # The highest-scoring players in any manager's squad, best first.
    top_scorers: list[RecapStandoutPlayer]
    # How many more players scored what the last one listed did, left out
    # by the cap rather than outscored.
    top_scorers_omitted: int
    # Blanks that counted, in `blanks_order`. In draft, only the players
    # that ordering can rank: a blank with no draft rank, or from GW19 one
    # with too few appearances behind his average, is not listed under a
    # heading that calls the list the best.
    blanks: list[RecapStandoutPlayer]
    # How many more were level with the last one listed on managers and
    # points, left out by the cap. Always 0 in draft, whose blanks are
    # ordered by what was expected of the player rather than counted level.
    blanks_omitted: int
    blanks_order: StandoutBlanksOrder


# A blank is a return of no more than this: the appearance points and nothing
# on top. The usual reading of the word, and the line the section draws.
STANDOUT_BLANK_MAX_POINTS = 2
# Appearances a player needs before his points per game ranks him among the
# draft blanks: a two-game cameo averaging 8 would otherwise head the list.
STANDOUT_PPG_MIN_APPEARANCES = 5
# The gameweek from which draft blanks are ordered by points per game rather
# than pre-season draft rank: the halfway point, by which that rank has gone
# stale. Its own policy, deliberately not the chip split -- draft has no
# chips -- even though both fall on the same gameweek today.
STANDOUT_PPG_FROM_GW = TOTAL_GAMEWEEKS // 2


def _points_words(points: int) -> str:
    """A player's points as a sentence spells them: "1 point", "-1 point",
    "17 points".

    Deliberately not `points_label()`, whose "12 pts" is the rosters' unit
    and is left exactly as the other sections print it: a sentence spells
    the unit out, and reads -1 as one point."""
    return f"{points} point" if abs(points) == 1 else f"{points} points"


def _standout_names(managers: Sequence[RecapStandoutManager]) -> str:
    """"Alice", "Alice and Bob", or "4 managers" once there are more than the
    line names -- and a count however short the list when two of them share
    a display name, since "for Alex and Alex" tells a reader nothing."""
    names = [m["manager_name"] for m in managers]
    if len(names) > DETAIL_CAP or len(set(names)) < len(names):
        return f"{len(names)} manager{'s' if len(names) != 1 else ''}"
    return join_and(names)


def _standout_for(
    counted: Sequence[RecapStandoutManager],
    *,
    managers: int,
    not_counted: Sequence[RecapStandoutManager],
    whole_league: bool,
) -> str:
    """Whose points a player counted towards: "for Alice and Bob", "for all
    12 managers", "for 9 of the 12 managers, everyone but Alice, Bob and
    Cam", or, where neither side is short enough to name, "for 6 of the 12
    managers". The side that is short is the story: a differential's owners,
    or the few who went without a template player.

    Where the managers fetched are not the whole league, the line counts
    only the ones it can see -- "for 9 managers" -- and never says "all" or
    "everyone but", which a manager it could not fetch might contradict."""
    if len(counted) <= DETAIL_CAP:
        return f"for {_standout_names(counted)}"
    if not whole_league:
        return f"for {len(counted)} managers"
    if not not_counted:
        return f"for all {managers} managers"
    clause = f"for {len(counted)} of the {managers} managers"
    if len(not_counted) <= DETAIL_CAP:
        clause += f", everyone but {_standout_names(not_counted)}"
    return clause


def standout_acquisition_clause(
    acquisition: RecapAcquisition, *, shared_names: Collection[str] = (),
) -> str:
    """How a draft player came to his manager, as a clause that follows the
    manager's name (#458): "who drafted him in the first round", "who picked
    him up on waivers in GW4", "who signed him as a free agent in GW9", "who
    acquired him from Cam in a GW7 trade".

    The latest link only, as the league's own history gives it, so a pick
    dropped and signed again reads as the signing. A redraft names itself
    ("in the first round of the GW20 redraft") because the league has drafted
    twice and "the draft" alone no longer says which; the first draft of a
    league that has held one never does. Where the draft's picks are lost the
    round is not guessed at: "who drafted him", or "who drafted him in the
    GW20 redraft". A pick the draft's auto-pick made
    says so rather than claim the manager chose him. A trade names the
    manager he came from unless another manager shares that display name,
    since "from Alex" then tells a reader nothing; `shared_names` are the
    names the league has more than one manager of.

    A relative clause on the manager's name, not a possessive pronoun: the
    data holds no one's gender, and the player is the only "him" it can
    stand behind.
    """
    kind, gameweek = acquisition["kind"], acquisition["gameweek"]
    if kind == "waiver":
        return f"who picked him up on waivers in GW{gameweek}"
    if kind == "free_agent":
        return f"who signed him as a free agent in GW{gameweek}"
    if kind == "trade":
        source = acquisition["from_manager"]
        came_from = f" from {source['manager_name']}" if source and source["manager_name"] not in shared_names else ""
        return f"who acquired him{came_from} in a GW{gameweek} trade"
    clause = "who had him as an auto-pick" if acquisition["auto_pick"] else "who drafted him"
    rounds = acquisition["round"]
    clause += f" in the {ordinal_word(rounds)} round" if rounds else ""
    if acquisition["redraft"] and acquisition["draft_gameweek"] is not None:
        # "In the first round of the GW20 redraft", or, the round being
        # unknown, "in the GW20 redraft".
        clause += f"{' of' if rounds else ' in'} the GW{acquisition['draft_gameweek']} redraft"
    return clause


def format_standout_player(
    player: RecapStandoutPlayer,
    *,
    managers: int,
    not_counted: Sequence[RecapStandoutManager],
    whole_league: bool,
    blank: bool,
    shared_names: Collection[str] = (),
) -> str:
    """One standout player as a sentence a columnist could lift (#455):
    "Haaland (Manchester City) scored 17 points for 9 of the 12 managers,
    everyone but Alice, Bob and Cam; Alice left him on the bench."

    A draft player has one owner, and where the league's history says how he
    came to them the sentence carries it after their name (#458): "Haaland
    (Manchester City) scored 17 points for Alice, who drafted him in the
    first round." See `standout_acquisition_clause()`. Where it cannot, the
    line reads as it always has.

    A blank "managed" his points rather than scoring them and says how long
    he played where it was under the hour: "did not play for Cam", or
    "managed 1 point in 27 minutes for Dan". A red card is said either way
    ("was sent off and managed -1 point for Bob"). A draft blank reads like
    a classic one: what his list is ordered by is the heading's to say, and
    the payload's, never the line's. Given "pre-season draft rank 2" on the
    line, the editorial wrote it up as this league's "number 2 draft pick".

    Written for a reader rather than a ledger, because the editorial lifts
    what its data sections put in front of it (audit finding 20): every
    figure is the player's own and every name is one the line can stand
    behind, so the sentence can be used as it stands. `not_counted` are the
    managers he did not count for, named when the line says "everyone but",
    which it only says when `whole_league` holds.
    """
    club = player["team_name"]
    line = f"{player['name']} ({club})" if club else player["name"]
    minutes = player["minutes"]
    if blank and minutes == 0:
        line += " did not play"
    else:
        if player["red_cards"]:
            line += " was sent off and"
        line += f" {'managed' if blank else 'scored'} {_points_words(player['points'])}"
        if blank and minutes is not None and minutes < 60:
            line += f" in {minutes} minute{'s' if minutes != 1 else ''}"
    counted = player["counted_for"]
    benched = player["benched_by"]
    acquisition = player.get("acquisition")
    # Said of the one manager who has him, never of several.
    how = (
        standout_acquisition_clause(acquisition, shared_names=shared_names)
        if acquisition and len(counted) + len(benched) == 1 else ""
    )
    if counted:
        line += " " + _standout_for(
            counted, managers=managers, not_counted=not_counted, whole_league=whole_league,
        )
        line += f", {how}" if how else ""
    if benched:
        owner = _standout_names(benched) + (f", {how}," if how else "")
        line += ("; " if counted else ", but ") + f"{owner} left him on the bench"
    return line + "."


def standout_players_note(fpl_format: str, standouts: RecapStandoutPlayers | None = None) -> str:
    """What the section's figures are, said once above it in the prompt: the
    player's own points (classic's before captaincy, which the Captains
    section owns), what a blank is, and, where a draft line says how a player
    came to his manager (#458), what that is -- the latest way he joined that
    squad as of the gameweek, so a pick the manager dropped and signed again
    is the signing. The saved report has no such section (#482)."""
    captaincy = ", before any captaincy" if fpl_format == "classic" else ""
    note = (
        f"Each player's own points this gameweek{captaincy}, among the players in this league's "
        f"squads. A blank is {STANDOUT_BLANK_MAX_POINTS} points or fewer."
    )
    if standouts and any(
        p.get("acquisition") for p in standouts["top_scorers"] + standouts["blanks"]
    ):
        note += (
            " Where a line says how a player came to the manager it names, that is the latest way he "
            "joined that manager's squad as of this gameweek: a draft pick, a waiver or free-agent "
            "claim, or a trade."
        )
    return note


def standout_blanks_heading(standouts: RecapStandoutPlayers) -> str:
    """The blanks list's heading, which says what it is ordered by, so a
    reader, and the editorial, can tell why these three and not others.
    FPL's draft rank is one ranking shared by every draft league, so the
    heading never calls it "draft": a reader takes that for this league's
    own draft, and the editorial did, writing a rank up as a "draft pick"."""
    order = standouts["blanks_order"]
    if order == "draft_rank":
        return "Blanks among the best-ranked players, by FPL's pre-season player ranking"
    if order == "points_per_game":
        return "Blanks among this season's best performers, by points per game"
    return "Blanks that counted for the most managers"


def standout_players_lines(standouts: RecapStandoutPlayers) -> tuple[list[str], list[str]]:
    """The section's two lists as printed lines, top scorers then blanks,
    each closed by how many more were level with the last one listed --
    "2 more players also scored 9 points." -- so the cap never reads as the
    end of the list."""
    scorers = [p["text"] for p in standouts["top_scorers"]]
    if standouts["top_scorers"] and (more := standouts["top_scorers_omitted"]):
        last = standouts["top_scorers"][-1]
        scorers.append(
            f"{more} more player{'s' if more != 1 else ''} also scored {_points_words(last['points'])}.",
        )
    blanks = [p["text"] for p in standouts["blanks"]]
    if standouts["blanks"] and (more := standouts["blanks_omitted"]):
        last = standouts["blanks"][-1]
        counted = len(last["counted_for"])
        blanks.append(
            f"{more} more player{'s' if more != 1 else ''} also managed {_points_words(last['points'])}"
            + (f" for {counted} managers each." if counted > 1 else "."),
        )
    return scorers, blanks


class RecapFinePlayer(TypedDict):
    """One player a fine names, carried alongside the prose that names him."""

    name: str
    # Stable cross-season element_code, or None where the pick never resolved
    # to one. The message spells out whatever the player was called when the
    # fine was ruled; this is what still identifies him after a rename.
    code: int | None


class RecapFineResult(TypedDict):
    """A fine triggered for a specific manager."""

    manager_name: str
    # The manager's ledger key (classic `entry`, draft `league_entry`). Two
    # managers can share a display name, so a stored ruling is keyed rather
    # than matched back by name.
    manager_key: NotRequired[int]
    rule_type: str
    message: str
    # Who `message` names. Present and empty is a real answer -- a `last-place`
    # or `below-threshold` ruling names nobody. Absent means the caller built
    # this by hand and said nothing either way, which the ledger records as
    # unknown rather than as "names nobody" (issue #176).
    players: NotRequired[list[RecapFinePlayer]]


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
    # The week's top scorers and costliest blanks in the league's squads, with
    # whose team each one counted for (issue #455). Set by the command for
    # the gameweek it recaps, never by the collectors, so a backfill's
    # replays -- which only reach the ledger -- do not build it; absent when
    # it could not be built.
    standout_players: NotRequired[RecapStandoutPlayers]
    # Draft only (#458): why a recap that asked for how each player came to his
    # manager could not say so -- the history of the league's draft, waivers
    # and trades could not be read, or did not agree with FPL's own record of
    # who owns whom. Set by the draft collector; absent when it worked or was
    # not asked, and the Standout Players lines then read without it.
    draft_provenance_problem: NotRequired[str]
    # Draft only (#458): set when the lines could say who drafted each player
    # but not in which round, because the draft's picks are held by neither the
    # API nor a snapshot and were rebuilt from the squads. A notice, not a
    # failure: the lines are labelled, just without rounds.
    draft_provenance_notice: NotRequired[str]
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
    # was not a normal completion (#266), on the attempt that shipped (#372).
    # Present means the text above may be cut off, which the report's callout
    # says; absent means the provider either finished or said nothing.
    synthesis_stop_reason: NotRequired[str]
    # True when that stop was a refusal or content filter: the callout and the
    # JSON warning then say the provider refused, not that the text may be cut
    # off -- re-running is not how the reader gets the rest (#399 review).
    synthesis_refused: NotRequired[bool]
    # The refusal's category when the provider named one (Anthropic's
    # `stop_details.category`), so every surface that says "refused" can say
    # which check declined it (#450). Absent for every other stop.
    synthesis_stop_category: NotRequired[str]
    # How many answers were judged (1, or 2 after a retry), which of them
    # shipped (the retry only when it ranked strictly better), and the tokens
    # every judged answer spent between them as `{"input_tokens",
    # "output_tokens"}`. Present only when an editorial came back, so a recap
    # can be read against the last one (#450).
    synthesis_attempts: NotRequired[int]
    synthesis_shipped_attempt: NotRequired[int]
    synthesis_usage: NotRequired[dict[str, int]]
    # Every claim the editorial makes that its own data contradicts, keyed by
    # the warning code of the check that caught it and worded as
    # `check_recap_editorial()` words it (`prompts/league_recap_checks.py`,
    # #357, #359). Present means the editorial above is saved as written but
    # is wrong there; only a code with at least one problem is a key.
    synthesis_problems: NotRequired[dict[str, list[str]]]
    # Why FPL had not finished processing the gameweek when this live capture
    # read the league table (issue #424), so the report can say its positions
    # and totals may be stale. Absent when the feed said it had, or could not
    # say.
    league_tables_updating: NotRequired[str]
    # Ledger partition key and the league's own start gameweek (absent or 1
    # means it started at GW1, so there is nothing to offset or skip).
    league_id: NotRequired[int]
    league_start_event: NotRequired[int]
    # Draft only: `league.drafts` from the league details, verbatim -- one
    # entry per draft the league has scheduled or held. Read by the snapshot
    # that keeps each held draft's picks before a redraft erases them from
    # the API (#461). None when the details carry no such list; absent on
    # classic.
    league_drafts: NotRequired[list[Any] | None]
    # Draft only: the draft API's choices response the provenance replay
    # already read (#458), handed on for the same snapshot to save rather
    # than asked for again. Absent when the read failed or provenance was not
    # collected.
    draft_choices: NotRequired[dict[str, Any]]
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
    # The stable codes of players whose club this capture derived exactly
    # from the gameweek's own fixtures. The identity carry reads it to tell a
    # club it should keep from one it should replace with what the ledger
    # already recorded (issue #177). Absent when the caller never resolved
    # gameweek clubs at all.
    clubs_derived_codes: NotRequired[list[int]]
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
    # Report-surfaced prior-season text (issue #131), stashed as plain
    # strings like the `league_history_*` fields so the template needs no
    # knowledge of the manager rows: one line per manager with a record,
    # then the statements of who has none and who could not be asked. Both
    # absent outside the league's opening gameweek, when nothing was fetched.
    prior_seasons_lines: NotRequired[list[str]]
    prior_seasons_coverage_lines: NotRequired[list[str]]


# ---------------------------------------------------------------------------
# Prior seasons (issue #131)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorSeasonsSummary:
    """Every fetched manager's FPL record before this season, sorted into
    the three answers the fetch can give: a record, no record, or no answer.

    `lines` carries one sentence per manager with at least one earlier
    season on record, best most-recent finish first. `new_to_fpl` names the
    managers whose fetch came back with no seasons at all -- a real answer,
    not a gap -- and `unavailable` those whose fetch never completed. Built
    once and read by both the saved report and the editorial prompt, so the
    two cannot describe one manager's past two ways.
    """

    lines: list[str]
    new_to_fpl: list[str]
    unavailable: list[str]

    @property
    def total_managers(self) -> int:
        return len(self.lines) + len(self.new_to_fpl) + len(self.unavailable)

    @property
    def coverage_lines(self) -> list[str]:
        """The absence statements, in report prose: who has no record and
        who could not be asked. Stated rather than left as a missing bullet,
        the convention the League History coverage lines already follow."""
        lines: list[str] = []
        if self.new_to_fpl:
            lines.append(
                f"No prior FPL seasons on record for {join_and(self.new_to_fpl)}: "
                "this is their first season of FPL on record."
            )
        if self.unavailable:
            lines.append(
                f"Prior seasons could not be fetched for {join_and(self.unavailable)}."
            )
        return lines


def summarise_prior_seasons(
    managers: Sequence[RecapManagerEntry],
    *,
    previous_season_name: str | None = None,
) -> PriorSeasonsSummary | None:
    """Fold the managers' `prior_seasons` into the summary both surfaces
    read, or None when no manager carries the key at all: the fetch was
    never made (any gameweek but the league's opener), and the section is
    then omitted. A failed fetch leaves the key present and None, so a
    gameweek where every fetch failed is still a section -- a roster of
    "could not be fetched" -- rather than silence about the whole cohort.

    `previous_season_name` is the season just finished, in the API's own
    "2025/26" form, so a manager whose most recent season is that one can
    be described as having played "last season" -- and one who sat it out
    is not, however recent their latest season looks.

    A manager without the key at all was never asked, and is outside the
    summary rather than in `unavailable`: "could not be fetched" is a claim
    about a request that was made. The collector populates every manager it
    is handed, so today that only arises for a caller mixing cohorts; the
    three lists and `total_managers` then cover the managers it asked for.
    """
    if not any("prior_seasons" in m for m in managers):
        return None

    with_record: list[tuple[int, str, str]] = []  # (most recent rank, name, line)
    new_to_fpl: list[str] = []
    unavailable: list[str] = []
    for m in managers:
        if "prior_seasons" not in m:
            continue
        name = m["manager_name"]
        seasons = m.get("prior_seasons")
        if seasons is None:
            unavailable.append(name)
        elif not seasons:
            new_to_fpl.append(name)
        else:
            ordered = _ordered_seasons(seasons)
            line = format_prior_seasons_line(
                name, ordered, previous_season_name=previous_season_name,
            )
            with_record.append((ordered[-1]["rank"], name, line))
    with_record.sort(key=lambda entry: (entry[0], entry[1]))
    return PriorSeasonsSummary(
        lines=[line for _, _, line in with_record],
        new_to_fpl=sorted(new_to_fpl),
        unavailable=sorted(unavailable),
    )


def format_prior_seasons_line(
    name: str,
    seasons: Sequence[RecapPriorSeason],
    *,
    previous_season_name: str | None = None,
) -> str:
    """One manager's FPL record before this season, as a sentence.

    "Alice: 10 prior FPL seasons played (2014/15 to 2025/26; 2 seasons
    missed in between), making this their 11th season of FPL. Last season
    (2025/26): 2,301 pts, rank 55,120 (top 1%). Best season: 2023/24,
    2,512 pts, rank 6,780 (top 0.1%)."

    Tenure is the length of `past` -- seasons actually played -- rather than
    the entry's `years_active`, which disagrees with it in both directions
    and is undocumented; the gap count says how many seasons inside the span
    were sat out. The season being played is counted into the ordinal
    because the manager is, by construction, playing it: they are in the
    league being recapped -- and it is spelt the way the fines placement
    spells its ordinals ("their third season", "their 11th"), since both
    land in one prompt. "Last season" is only said of the season that
    actually just finished; a most recent season older than that is named as
    such, with the season sat out. Every figure is the API's own, grouped
    for reading, so the editorial has nothing left to compute.
    """
    ordered = _ordered_seasons(seasons)
    played = len(ordered)
    first, last = ordered[0], ordered[-1]
    span = (
        first["season_name"] if played == 1
        else f"{first['season_name']} to {last['season_name']}"
    )
    missed = _seasons_missed(ordered)
    if missed:
        span += f"; {missed} season{'s' if missed != 1 else ''} missed in between"
    nth = played + 1
    if previous_season_name is None or last["season_name"] == previous_season_name:
        recent = f"Last season ({last['season_name']})"
    else:
        recent = (
            f"Most recent season ({last['season_name']}; did not play "
            f"{previous_season_name})"
        )
    line = (
        f"{name}: {played} prior FPL season{'s' if played != 1 else ''} played ({span}), "
        f"making this their {ordinal_word(nth)} season of FPL. "
        f"{recent}: {_season_result(last)}"
    )
    # Reversed so a tie on points and rank resolves to the more recent
    # season, which is then the one already named above.
    best = max(reversed(ordered), key=lambda s: (s["total_points"], -s["rank"]))
    if played == 1:
        line += "."
    elif best is last:
        line += ", their best to date."
    else:
        line += f". Best season: {best['season_name']}, {_season_result(best)}."
    return line


def _ordered_seasons(seasons: Sequence[RecapPriorSeason]) -> list[RecapPriorSeason]:
    """Oldest first. The API already lists them that way; sorted here so the
    "most recent" and "first" readings never depend on it staying so."""
    return sorted(seasons, key=lambda s: s["season_name"])


def _season_result(season: RecapPriorSeason) -> str:
    """"2,301 pts, rank 55,120 (top 1%)" -- the percentage only where the
    API sent one."""
    text = f"{season['total_points']:,} pts, rank {season['rank']:,}"
    pct = season.get("rank_percentage")
    if pct:
        text += f" (top {pct}%)"
    return text


def _seasons_missed(ordered: Sequence[RecapPriorSeason]) -> int:
    """How many seasons inside the span were sat out. `past` lists only the
    seasons played, so the span's length minus the list's is the gap; zero
    where a season label does not start with its year."""
    try:
        first = int(ordered[0]["season_name"][:4])
        last = int(ordered[-1]["season_name"][:4])
    except ValueError:
        return 0
    return max(0, (last - first + 1) - len(ordered))


# ---------------------------------------------------------------------------
# Contested claims (issue #330)
# ---------------------------------------------------------------------------


# The stable player code where the collector resolved one, else the name and
# club it printed. Both sides of every move and claim in one gameweek resolve
# through the same match, so a player has the same key on the accepted row
# that won him and the lost claims that did not.
_ContestedPlayerKey = int | tuple[str, str]


def _contested_player_key(
    move: RecapDraftTransaction | RecapDraftLostClaim,
) -> _ContestedPlayerKey:
    code = move.get("player_in_code")
    if code is not None:
        return code
    return (move["player_in"], move["player_in_team"])


def _contest_winner(
    accepted: Sequence[tuple[int, str]], claimants: Iterable[RecapContestedClaimant],
) -> int | None:
    """The manager whose accepted move won the race, as a position in the
    managers list, from the (position, kind) pairs that brought the player
    in this gameweek.

    Normally exactly one. Where the feed holds more -- the waiver winner
    moved him on and a rival signed him as a free agent before the deadline
    -- the one whose kind matches the losing claims is the race they lost.
    """
    if not accepted:
        return None
    kinds = {c["kind"] for c in claimants}
    for position, kind in accepted:
        if kind in kinds:
            return position
    return accepted[0][0]


def contested_draft_claims(
    managers: Sequence[RecapManagerEntry],
) -> list[RecapContestedClaim]:
    """Every player more than one manager claimed this gameweek, most
    contested first: by claimant count, then by how early the beaten
    managers ranked him (two first-choice claims outrank one), then by name.

    Regrouped from the manager rows rather than fetched: a stored lost claim
    is one a rival won, so the gameweek's lost claims, bucketed by player, are
    the races, and the accepted move that brought the same player in names the
    winner (issue #330). Only the stored claims carry that meaning -- the
    feed's `di` code says the incoming player was gone, which the manager's
    own winning claim also causes; `resolve_lost_claims()` settles that
    upstream (issue #342). Read off the raw move list, not
    the awards' chain-contracted one, so a manager who won a player and moved
    him on again the same gameweek still won him.

    A manager who lost several claims on one player -- a conditional chain
    offering different drops for him -- wanted him once, and is named once,
    at the highest priority they gave him. The winner is never also among
    the beaten: a manager who lost the waiver and signed him as a free agent
    later the same gameweek carries both rows. Either way they wanted him
    once and got him, and a player nobody else wanted is then no race at all.

    Both rules now hold of the stored claims before this runs, since
    `resolve_lost_claims()` applies them at ingestion -- these are kept as
    the guarantee this function owns, so a caller handing it claims from
    anywhere still gets one row per manager per race. Empty for classic, whose rows
    carry no claims.
    """
    players: dict[_ContestedPlayerKey, RecapDraftLostClaim] = {}
    # Claimants are keyed by the manager's position in `managers`, never by
    # name: two managers can share a display name, and the one who won has
    # to be told apart from the one who lost.
    claimants: dict[_ContestedPlayerKey, dict[int, RecapContestedClaimant]] = {}
    accepted: dict[_ContestedPlayerKey, list[tuple[int, str]]] = {}
    for position, m in enumerate(managers):
        for move in m.get("transactions") or []:
            accepted.setdefault(_contested_player_key(move), []).append((position, move["kind"]))
        best: dict[_ContestedPlayerKey, RecapDraftLostClaim] = {}
        for claim in m.get("lost_claims") or []:
            key = _contested_player_key(claim)
            known = best.get(key)
            if known is None or draft_claim_priority_rank(claim["priority"]) < draft_claim_priority_rank(
                known["priority"],
            ):
                best[key] = claim
        for key, claim in best.items():
            players.setdefault(key, claim)
            claimants.setdefault(key, {})[position] = RecapContestedClaimant(
                manager_name=m["manager_name"], kind=claim["kind"], priority=claim["priority"],
            )

    contests: list[RecapContestedClaim] = []
    for key, claim in players.items():
        wanted = claimants[key]
        winner = _contest_winner(accepted.get(key, []), wanted.values())
        beaten = sorted(
            (c for position, c in wanted.items() if position != winner),
            key=lambda c: (draft_claim_priority_rank(c["priority"]), c["manager_name"]),
        )
        if not beaten:
            continue
        contest = RecapContestedClaim(
            player=claim["player_in"],
            player_team=claim["player_in_team"],
            player_team_name=claim.get("player_in_team_name"),
            winner=None if winner is None else managers[winner]["manager_name"],
            losers=beaten,
            claimants=len(beaten) + (0 if winner is None else 1),
        )
        code = claim.get("player_in_code")
        if code is not None:
            contest["player_code"] = code
        contests.append(contest)

    contests.sort(key=lambda c: (
        -c["claimants"],
        tuple(draft_claim_priority_rank(loser["priority"]) for loser in c["losers"]),
        c["player"],
        c.get("player_code") or 0,
    ))
    return contests


def _claimant_text(claimant: RecapContestedClaimant) -> str:
    """"Bob (priority 1)", or "Bob (free agent)" for a claim of any kind but
    a waiver -- the label the awards use -- and "Bob" alone for a waiver
    claim the feed sent no priority for."""
    parts: list[str] = []
    label = draft_transaction_kind_label(claimant["kind"])
    if label != DRAFT_TRANSACTION_KIND_LABELS["w"]:
        parts.append(label)
    if claimant["priority"] is not None:
        parts.append(f"priority {claimant['priority']}")
    return claimant["manager_name"] + (f" ({', '.join(parts)})" if parts else "")


def format_contested_claim(contest: RecapContestedClaim) -> str:
    """"Elanga was claimed by 4 managers: Alice won him; Bob (priority 1),
    Cam (priority 1) and Dan (priority 2) were beaten to him."

    One sentence for both surfaces -- the Most Contested award and the
    editorial's roster -- so no race is told two ways. Each beaten manager carries the priority they gave the
    claim, which is the point of naming them: three first-choice claims on
    one player is the week's story, and a fifth-choice claim that missed is
    a different thing from a first. Where the winner could not be identified
    the sentence says so and counts nobody it cannot name -- "Elanga was
    claimed by Alice (priority 1) and Bob (priority 1), who were beaten to
    him; the winner could not be identified" -- rather than asserting a
    winner and leaving the reader to infer that nobody won.
    """
    losers = contest["losers"]
    beaten = join_and([_claimant_text(c) for c in losers])
    verb = "was" if len(losers) == 1 else "were"
    if contest["winner"] is None:
        return (
            f"{contest['player']} was claimed by {beaten}, who {verb} beaten to him; "
            f"the winner could not be identified."
        )
    return (
        f"{contest['player']} was claimed by {contest['claimants']} managers: "
        f"{contest['winner']} won him; {beaten} {verb} beaten to him."
    )


def points_label(points: int) -> str:
    """A points figure with its unit: "1 pt", "0 pts", "12 pts"."""
    return f"{points} pt" if points == 1 else f"{points} pts"


def competition_rank(value: int, values: Iterable[int]) -> int:
    """`value`'s rank among `values` when higher is better: one more than the
    number above it, so level values share a rank and the next one down skips
    it (1, 2, 2, 4).

    The one definition the recap's own ranks are counted with (#486): the GW
    Rank column and the report's GW Standings block through
    `gameweek_rank()`, and the editorial checks' readings of a gameweek
    placing and a season total, so a change to how a tie or a missing value
    ranks reaches all of them at once. League positions -- Pos, Prev and the
    ledger's `gw_rank` -- come from `derive_point_in_time_positions()`
    (`_league_recap_data.py`), the same competition ranking over a sorted
    walk, and a change here does not reach them.
    """
    return 1 + sum(other > value for other in values)


def gameweek_rank(manager: RecapManagerEntry, managers: Sequence[RecapManagerEntry]) -> int:
    """A manager's placing on this gameweek's points: one more than the
    number who scored more, so level scores share a rank (#483).

    The one definition both recap tables read: the report's GW Standings
    block and the prompt's GW Rank column. Numbering ties apart
    on league position would hand the editorial a placing the prompt's tie
    rules say it may not claim, and a reader would see one in the report.
    """
    return competition_rank(manager["gw_points"], (other["gw_points"] for other in managers))


def last_place_escape(managers: Sequence[RecapManagerEntry]) -> str | None:
    """Who avoided last place on the week and by how much: "Bea Gale avoided
    last place on the week by 1 pt", or "Bea Gale, Cy Hart each avoided ...".
    None when every gameweek score is level, so nobody did.

    The one wording of the escape (#487): the prompt's GW Standings section
    states it on its lowest line, and the editorial check that holds the
    escape to the data (#488) hands the same sentence to the retry, so the
    two can never tell the model different things.
    """
    scores = sorted({m["gw_points"] for m in managers})
    if len(scores) < 2:
        return None
    lowest, nearest = scores[:2]
    escaped = sorted(m["manager_name"] for m in managers if m["gw_points"] == nearest)
    who = f"{', '.join(escaped)} each" if len(escaped) > 1 else escaped[0]
    return f"{who} avoided last place on the week by {points_label(nearest - lowest)}"


def place_label(rank: int) -> str:
    """A league position as the table reads it: "1st", "7th", "12th"."""
    return f"{rank}{ordinal_suffix(rank)}"


def recap_title(gameweek: int, league_name: str | None) -> str:
    """The saved recap's H1: "Gameweek 4 Recap: Sunday League".

    The one definition of the report's title, read by the template that
    renders it and the prompt that tells the editorial not to write another
    (issue #349). The league's name goes in exactly as configured -- it is
    the line the reader identifies the recap by, so it is never the model's
    to restyle. An empty name leaves just "Gameweek 4 Recap", and so does
    None: the collectors default the name only when the API omits it, not
    when it sends null, and a crash here is swallowed by the report agent
    into a run that exits 0 having saved nothing.
    """
    name = (league_name or "").strip()
    return f"Gameweek {gameweek} Recap: {name}" if name else f"Gameweek {gameweek} Recap"
