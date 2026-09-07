"""TypedDict contracts for league-recap data pipeline."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from fpl_cli.utils.text import ordinal_word


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
                f"No prior FPL seasons on record for {_join_names(self.new_to_fpl)}: "
                "this is their first season of FPL on record."
            )
        if self.unavailable:
            lines.append(
                f"Prior seasons could not be fetched for {_join_names(self.unavailable)}."
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


def _join_names(names: Sequence[str]) -> str:
    """"Alice", "Alice and Bob", "Alice, Bob and Carol"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"
