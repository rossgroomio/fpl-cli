"""Notes pack and season-phase marker for league-recap history (U9).

Two things live here, both pure derivations from data U8 already computes --
never a second source of truth:

- `derive_season_phase`: which of five arcs (opener, pre-chip-boundary,
  midpoint, run-in, finale) a gameweek sits in (R15). A standalone, pure
  function so a later unit can call it without a store or a built pack.
- `build_notes_pack`: atomic, provenance-stamped factoids for one gameweek
  (R14) -- one per manager per streak condition with an open run, one per
  manager per condition with a season occurrence count (issue #164), plus
  the season-phase marker and a coverage/negative-context statement, all
  bundled into one `NotesPack`. This is the object a later unit (U12)
  renders into its own anchored prompt section; the prompt text and the
  "forbid inferring history elsewhere" rule are that unit's job, not this
  one's. This module's job stops at producing correctly-windowed,
  honestly-qualified facts.

Two rules this module exists to enforce (R14, R20, R17):

- A streak with any held gameweek is rendered as an observed count over its
  true span, never as a consecutive run -- a length-3 run that held 8 is
  "3 in the last 11 gameweeks", not "3 in a row". Only a run holding nothing
  may use "in a row" phrasing.
- A claim is qualified "since GW X" only when the ledger's own coverage --
  for the partition, or for one manager -- actually begins later than the
  league's start gameweek. A bounded trailing read window (below) is a cost
  control on which rows get read, never a reason to qualify a claim: a
  streak entry always reflects a run's true, current state from the counters
  projection, however far back it truly started.

Weekly cost control (Success Criteria): every phase except the finale reads
the counters projection (`compute_counters_through`, which persists its own
cache) plus a trailing window of raw rows bounded by
`TRAILING_WINDOW_GAMEWEEKS`. Only the finale rescans the whole season, via
`rebuild_counters_through` -- a pure, point-in-time read that has no cache to
optimise for, appropriate for something that happens once a season.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pydantic import ValidationError

from fpl_cli.models.league_history import (
    EARLIEST_GAMEWEEK_CACHE_VERSION,
    FidelityTier,
    LeagueFormat,
    LeagueHistoryCountersProjection,
    LeagueHistoryRow,
    ManagerEarliestGameweekCache,
    weakest_tier,
)
from fpl_cli.season import CHIP_SPLIT_GW, TOTAL_GAMEWEEKS
from fpl_cli.services.league_history import LeagueHistoryError, LeagueHistoryStore
from fpl_cli.services.league_history_counters import (
    ConditionRunView,
    compute_counters_through,
    counters_partition_dir,
    manager_condition_views,
    rebuild_counters_through,
)
from fpl_cli.utils.files import atomic_write_text
from fpl_cli.utils.gameweek import is_opening_gameweek

logger = logging.getLogger(__name__)

# The run-in phase (below) and the pack-builder's raw-row read window (in
# `build_notes_pack`) reuse this one constant deliberately: both describe
# "how close to the end of the season are we", just applied to two different
# things -- which gameweeks count as the run-in, and how many trailing
# gameweeks of rows get read outside the finale. One named constant means a
# future change to either cannot silently drift out of step with the other.
TRAILING_WINDOW_GAMEWEEKS: int = 6


# ---------------------------------------------------------------------------
# Season phase (R15)
# ---------------------------------------------------------------------------


class SeasonPhase(str, Enum):
    """Where a gameweek sits in the season's arc, independent of any one
    league's data -- purely a function of the gameweek number against the
    fixed season-length and chip-split constants (R15)."""

    OPENER = "opener"
    PRE_CHIP_BOUNDARY = "pre_chip_boundary"
    MIDPOINT = "midpoint"
    RUN_IN = "run_in"
    FINALE = "finale"


def derive_season_phase(
    gameweek: int,
    total_gameweeks: int = TOTAL_GAMEWEEKS,
    chip_split_gw: int = CHIP_SPLIT_GW,
) -> SeasonPhase:
    """Derive the season phase for one gameweek (R15).

    Exhaustive and gap-free over 1..total_gameweeks: opener (GW1) <
    pre_chip_boundary (GW2..chip_split_gw-1) < midpoint
    (chip_split_gw..total_gameweeks-TRAILING_WINDOW_GAMEWEEKS-1) < run_in
    (the last TRAILING_WINDOW_GAMEWEEKS gameweeks before the final one) <
    finale (total_gameweeks and beyond, so a season whose real final
    gameweek differs from the constant still reaches it).

    Only the opener and finale boundaries are handed down by the plan
    verbatim; midpoint and run_in split the remaining span at the
    chip-availability boundary and at a trailing window sized to match the
    pack-builder's own raw-row read window (see `build_notes_pack`), rather
    than at some other arbitrary fraction of the season -- both boundaries
    are already-meaningful points elsewhere in the codebase, so reusing them
    here avoids inventing a third, unrelated notion of "how far into the
    season".
    """
    if is_opening_gameweek(gameweek):
        return SeasonPhase.OPENER
    if gameweek >= total_gameweeks:
        return SeasonPhase.FINALE
    run_in_start = total_gameweeks - TRAILING_WINDOW_GAMEWEEKS
    if gameweek >= run_in_start:
        return SeasonPhase.RUN_IN
    if gameweek < chip_split_gw:
        return SeasonPhase.PRE_CHIP_BOUNDARY
    return SeasonPhase.MIDPOINT


def is_season_milestone(
    gameweek: int,
    total_gameweeks: int = TOTAL_GAMEWEEKS,
    chip_split_gw: int = CHIP_SPLIT_GW,
) -> bool:
    """Whether this gameweek is one of the season's two milestone *moments*.

    Deliberately not "is this gameweek in a milestone phase": `MIDPOINT`
    spans thirteen gameweeks, so gating a once-a-season set-piece on the
    phase would fire it thirteen times and turn the set-piece into
    wallpaper. The moment is the phase's own first gameweek -- the
    chip-availability boundary, which is `TOTAL_GAMEWEEKS // 2` and so is
    the halfway point too -- and the finale.

    Both are compared exactly rather than asked of `derive_season_phase`,
    whose FINALE branch is deliberately open-ended (`>= total_gameweeks`, so
    a season running past the constant still has a phase). Open-ended is
    right for a phase and wrong for a moment: a season rearranged out to
    GW39 would satisfy it at GW38 and again at GW39, and print the
    once-a-season set-piece twice. The moment is the constant's own final
    gameweek, which fires once whatever the real season length turns out to
    be.
    """
    return gameweek in (chip_split_gw, total_gameweeks)


# ---------------------------------------------------------------------------
# Notes pack shapes (R14, KTD8)
# ---------------------------------------------------------------------------


class NoteKind(str, Enum):
    """What kind of fact a `NotesPackEntry` states.

    Lets a consumer flattening `NotesPack.all_entries` (e.g. a `--format
    json` unit, per KTD8's "emits the whole pack regardless") branch on what
    an entry is without having to infer it from which `NotesPack` field it
    came from.
    """

    STREAK = "streak"
    SEASON_COUNT = "season_count"
    SEASON_PHASE = "season_phase"
    COVERAGE = "coverage"


class NoteSurface(str, Enum):
    """One rendering surface a `NotesPackEntry` is eligible to reach (KTD8).

    Surfaces nest: an entry reaching console always also reaches report and
    prompt, and one reaching report always also reaches prompt -- there is
    no console-only or report-without-prompt entry. `--format json` (a later
    unit) emits every entry regardless of its surfaces.
    """

    CONSOLE = "console"
    REPORT = "report"
    PROMPT = "prompt"


_STREAK_SURFACES: frozenset[NoteSurface] = frozenset(
    {NoteSurface.CONSOLE, NoteSurface.REPORT, NoteSurface.PROMPT},
)
_REPORT_AND_PROMPT: frozenset[NoteSurface] = frozenset({NoteSurface.REPORT, NoteSurface.PROMPT})
_PROMPT_ONLY: frozenset[NoteSurface] = frozenset({NoteSurface.PROMPT})


@dataclass(frozen=True)
class GameweekWindow:
    """The inclusive gameweek span a notes-pack entry's fact is computed over."""

    start_gameweek: int
    end_gameweek: int

    @property
    def span_length(self) -> int:
        return self.end_gameweek - self.start_gameweek + 1


@dataclass(frozen=True)
class NotesPackEntry:
    """One atomic, provenance-stamped factoid (R14).

    `text` is written to be reused verbatim or near-verbatim on whichever
    surfaces `surfaces` names -- kept factual and neutral, not editorialised,
    since a later unit renders it directly. `window`, `held_count`, and
    `tier` are the entry's provenance: the computation span, how much of
    that span held rather than extended, and the weakest fidelity tier among
    the rows actually read for it (`None` when no row read underlies the
    entry at all, as for the season-phase marker).
    """

    kind: NoteKind
    text: str
    surfaces: frozenset[NoteSurface]
    tier: FidelityTier | None = None
    window: GameweekWindow | None = None
    manager_key: int | None = None
    manager_name: str | None = None
    condition_key: str | None = None
    # The run's own length -- gameweeks that genuinely extended it, as
    # opposed to `window.span_length`, which also counts `held_count`. Equal
    # to `window.span_length - held_count` only when every gameweek in the
    # window folded in as an extend or a hold; a manager genuinely *absent*
    # from a gameweek (as opposed to unknown) breaks that equality without
    # affecting either counter, which is exactly why this is stored directly
    # rather than derived from the window (see `_streak_entries`).
    length: int = 0
    held_count: int = 0
    excess: int | None = None
    # Season-count entries only (issue #164): how many gameweeks have ever
    # extended this condition this season, across every reset. For those
    # entries `held_count` carries the season-wide held total -- the same
    # "how much of the span was never judged" meaning it has for a streak,
    # applied to the count's whole window. None for every other kind.
    occurrences: int | None = None


@dataclass(frozen=True)
class NotesPack:
    """Provenance-stamped factoids and a season-phase marker for one gameweek
    of one partition (R14, R15).

    `entries` holds only the per-manager streak factoids: pre-sorted by
    descending `excess` so a console renderer can take the leaders without
    re-sorting (KTD8's ranking rule). `season_count_entries` holds the
    per-manager season occurrence totals (issue #164), sorted by descending
    count: one per cohort manager x condition that has occurred at all this
    season, surfaced per the registry's own `CountSurfacePolicy` rules --
    the managers who fired their condition this gameweek plus that
    condition's qualifying ride-alongs, the whole nonzero set at the two
    season milestones, nothing beyond `--format json` otherwise (see
    `_season_count_entries`). `season_phase_entry` and `coverage_entries`
    are always
    populated -- never absent, never merely implied by an empty `entries`
    list -- as their own dedicated fields: a pack with no open streaks
    still has something to say about where the season is and what history
    exists. `all_entries` gives a consumer everything at once, e.g. for a
    `--format json` unit (KTD8: "emits the whole pack regardless").
    """

    season: str
    fpl_format: LeagueFormat
    league_id: int
    gameweek: int
    phase: SeasonPhase
    league_start_gameweek: int
    season_phase_entry: NotesPackEntry
    entries: list[NotesPackEntry] = field(default_factory=list)
    season_count_entries: list[NotesPackEntry] = field(default_factory=list)
    coverage_entries: list[NotesPackEntry] = field(default_factory=list)

    @property
    def entry_count(self) -> int:
        """Count of streak entries -- zero for a partition with no open runs
        at all, independent of the season-phase marker and coverage
        statements, which are always present regardless."""
        return len(self.entries)

    @property
    def all_entries(self) -> list[NotesPackEntry]:
        """Every entry the pack holds -- streaks, then season counts, then
        the season-phase marker, then coverage statements -- flattened into
        one list."""
        return [
            *self.entries,
            *self.season_count_entries,
            self.season_phase_entry,
            *self.coverage_entries,
        ]


# ---------------------------------------------------------------------------
# Streak entries (point 2)
# ---------------------------------------------------------------------------


def _entry_tier(
    window: GameweekWindow,
    manager_key: int,
    rows_by_gameweek: dict[int, dict[int, LeagueHistoryRow]],
) -> FidelityTier | None:
    """Weakest tier among this manager's rows actually read within the
    entry's window. Bounded by whatever `rows_by_gameweek` holds -- the
    trailing window, or the full season at the finale -- never a fresh read
    of the streak's whole span regardless of how far back it truly started."""
    tiers: list[FidelityTier] = []
    for gameweek in range(window.start_gameweek, window.end_gameweek + 1):
        row = rows_by_gameweek.get(gameweek, {}).get(manager_key)
        if row is not None:
            tiers.append(row.tier)
    return weakest_tier(tiers)


def _streak_text(manager_name: str, label: str, length: int, held_count: int, window: GameweekWindow) -> str:
    """Render a run as an observed count over its true span. A run with any
    held gameweek is never rendered as consecutive (R14, R20): a length-3
    run holding 8 is "3 in the last 11", not "3 in a row". "In a row" also
    requires `window.span_length == length`, not `held_count == 0` alone: a
    manager wholly *absent* (not merely unknown) from one gameweek inside an
    otherwise-continuous run also leaves `held_count` at 0 (`_fold_gameweek`
    skips a wholly-absent manager without counting a hold), but widens
    `window` past `length` (`_streak_entries` anchors `end_gameweek` on the
    pack's own target gameweek, not on `start + length + held - 1`) -- so
    without this second check a real gap would still be claimed as
    consecutive. That case falls through to the same observed-count
    phrasing as any other held run, since it is the same situation: the
    window is wider than the count."""
    span = f"GW{window.start_gameweek}-GW{window.end_gameweek}"
    if held_count == 0 and window.span_length == length:
        return f"{manager_name}: {label} of {length}, {length} in a row ({span})."
    return (
        f"{manager_name}: {label} of {length} in the last {window.span_length} ({span}), "
        f"with {held_count} not recorded."
    )


def _streak_entries(
    *,
    gameweek: int,
    cohort: dict[int, LeagueHistoryRow],
    projection: LeagueHistoryCountersProjection,
    rows_by_gameweek: dict[int, dict[int, LeagueHistoryRow]],
) -> list[NotesPackEntry]:
    """One entry per manager x applicable condition with an open run.

    Restricted to `cohort` -- the managers present in the gameweek this pack
    is built for -- rather than every manager key `projection` has ever
    touched: a manager entirely absent from a later gameweek's rows (they
    left this particular mini-league, or that gameweek's coverage simply has
    a gap for them -- ordinary FPL inactivity does not do this, since an
    inactive squad still gets scored and still appears in the standings
    every week) keeps a frozen, no-longer-live run in `projection.runs`
    forever (folding skips a manager entirely absent from a gameweek's rows,
    per `_fold_gameweek`'s docstring), and surfacing that as a live fact in a
    much later gameweek's pack would misrepresent it as current.
    """
    entries: list[NotesPackEntry] = []
    for manager_key, manager_row in cohort.items():
        views = manager_condition_views(projection, manager_key)
        for condition_key, view in views.items():
            if view.length <= 0 or view.start_gameweek is None:
                continue  # a fresh/never-opened run conveys nothing
            # `gameweek`, not `start + length + held - 1`: those agree
            # exactly when every gameweek since `start_gameweek` folded in
            # as an extend or a hold, but a manager who was genuinely
            # *absent* from a gameweek's rows (as opposed to unknown) is
            # skipped by `_fold_gameweek` without incrementing either
            # counter, which would otherwise understate how current the
            # run's span actually is.
            window = GameweekWindow(start_gameweek=view.start_gameweek, end_gameweek=gameweek)
            surfaces = _STREAK_SURFACES if view.is_reportable else frozenset()
            entries.append(NotesPackEntry(
                kind=NoteKind.STREAK,
                text=_streak_text(manager_row.manager_name, view.label, view.length, view.held_in_run, window),
                surfaces=surfaces,
                tier=_entry_tier(window, manager_key, rows_by_gameweek),
                window=window,
                manager_key=manager_key,
                manager_name=manager_row.manager_name,
                condition_key=condition_key,
                length=view.length,
                held_count=view.held_in_run,
                excess=view.excess,
            ))

    def _sort_key(entry: NotesPackEntry) -> tuple[int, str, str]:
        excess = entry.excess if entry.excess is not None else 0
        return (-excess, entry.manager_name or "", entry.condition_key or "")

    entries.sort(key=_sort_key)
    return entries


# ---------------------------------------------------------------------------
# Season-count entries (issue #164)
# ---------------------------------------------------------------------------


def _season_count_text(
    manager_name: str,
    label_one: str,
    label_many: str,
    occurrences: int,
    held_total: int,
    window: GameweekWindow,
    *,
    occurred_this_gameweek: bool,
    run_length: int = 0,
    run_held: int = 0,
    run_framed: bool = False,
) -> str:
    """Render a season total as a count over the span it was computed on.

    Same honesty rules as `_streak_text`: the count is never a bare number.
    The span is stated inline, a held gameweek is stated as unjudged rather
    than silently rounded into innocence (a hold means the gameweek could
    not be ruled either way -- an unknown capture, a fixture-less blank, a
    condition that did not apply -- and #136 documents why that is not the
    same as "it didn't happen"), and the "this gameweek" marker appears
    exactly when the fold's own `last_occurrence_gameweek` says the count
    grew now, so a consumer quoting the line can tell fresh colour from a
    stale total.

    `run_framed` inverts which number leads, for a condition whose policy
    fires on the open run rather than the total (the green-arrow drought).
    Such a line surfacing at "22 this season" would never say why it
    appeared -- what is notable is the unbroken run of 5 -- so the run
    leads and the season total follows as context. Both numbers are still
    stated: dropping either would make the line answer a question it was
    not asked. "In a row" obeys the same rule it does in `_streak_text`:
    only a run that held nothing may claim it, since a run crossing an
    unjudged gameweek is a count over a span, not a consecutive sequence.
    """
    label = label_one if occurrences == 1 else label_many
    span = f"GW{window.start_gameweek}-GW{window.end_gameweek}"

    if run_framed and run_length > 0:
        run_label = label_one if run_length == 1 else label_many
        shape = "in a row" if run_held == 0 else "in their current run"
        text = (
            f"{manager_name}: {run_length} {run_label} {shape}, "
            f"{occurrences} this season ({span})"
        )
    else:
        text = f"{manager_name}: {occurrences} {label} this season ({span})"
        if occurred_this_gameweek:
            text += ", the first this gameweek" if occurrences == 1 else ", the latest this gameweek"

    if held_total:
        plural = "" if held_total == 1 else "s"
        text += f", with {held_total} gameweek{plural} not judged either way"
    return text + "."


def _season_count_entries(
    *,
    gameweek: int,
    cohort: dict[int, LeagueHistoryRow],
    projection: LeagueHistoryCountersProjection,
    rows_by_gameweek: dict[int, dict[int, LeagueHistoryRow]],
    milestone: bool,
    second_half: bool,
) -> list[NotesPackEntry]:
    """One entry per manager x applicable condition that has occurred at all.

    Restricted to `cohort` for the same reason `_streak_entries` is: a
    manager no longer in this gameweek's rows keeps a frozen count in
    `projection.runs`, and surfacing it in a later pack would present it as
    live. Every nonzero count is emitted so `--format json` carries the
    whole season picture (KTD8); which of them carry rendering surfaces on
    an ordinary gameweek is each condition's own `CountSurfacePolicy` in
    the registry, evaluated here in two passes because a firing is
    cross-manager: first, who *fired* their condition with this gameweek's
    increment (a total on the condition's step, an unbroken run at one of
    its run milestones, a second-half first); then, for each condition
    someone fired, which same-gameweek incrementers *ride along* beside
    them -- their total past the condition's absolute floor, or within its
    relative window of a firing total, which is why the firing totals are
    collected per condition rather than a bare set of keys. A condition
    nobody fired stays entirely quiet however many totals grew. At the two
    season milestones the whole nonzero set carries report+prompt
    regardless: the halfway and finale reports get their `## Season
    Counts` set-piece, and the editorial gets the season-spanning facts
    exactly when its phase framing invites a retrospective. Console is
    always excluded: it is a highlights view, and the streak leaders
    already cover it.

    The window is the manager's own evaluated span
    (`first_evaluated_gameweek`..this gameweek), not the league's -- a
    mid-season joiner's count is bounded to the gameweeks it was actually
    folded over, and R17's joiner coverage entry beside it states why.
    """
    counted: list[tuple[int, LeagueHistoryRow, str, ConditionRunView, bool]] = []
    for manager_key, manager_row in cohort.items():
        views = manager_condition_views(projection, manager_key)
        for condition_key, view in views.items():
            if view.occurrences <= 0 or view.first_evaluated_gameweek is None:
                continue
            occurred_this_gameweek = view.last_occurrence_gameweek == gameweek
            counted.append((manager_key, manager_row, condition_key, view, occurred_this_gameweek))

    fired_managers = {
        (manager_key, condition_key)
        for manager_key, _, condition_key, view, occurred in counted
        if occurred and view.count_policy.qualifies(view, second_half=second_half)
    }
    # Per condition, the totals its firing managers landed on -- a relative
    # ride-along window measures against these rather than a fixed floor.
    fired_totals: dict[str, list[int]] = {}
    for manager_key, _, condition_key, view, _ in counted:
        if (manager_key, condition_key) in fired_managers:
            fired_totals.setdefault(condition_key, []).append(view.occurrences)

    entries: list[NotesPackEntry] = []
    for manager_key, manager_row, condition_key, view, occurred_this_gameweek in counted:
        window = GameweekWindow(
            start_gameweek=view.first_evaluated_gameweek or gameweek, end_gameweek=gameweek,
        )
        shown_weekly = occurred_this_gameweek and (
            (manager_key, condition_key) in fired_managers
            or (
                condition_key in fired_totals
                and view.count_policy.rides_along(
                    view, fired_totals=fired_totals[condition_key],
                )
            )
        )
        # A milestone deliberately bypasses the policy rather than relaxing
        # it: the GW19 and finale set-pieces are the season's whole picture,
        # so a count the weekly rules held back all season is exactly what
        # they exist to show. Routing them through `qualifies` instead would
        # gate the set-piece by step and leave the table with holes -- and
        # `second_half_only` would empty the bottom-half rows from the very
        # table that closes the first half. Any per-condition gate added to
        # `CountSurfacePolicy` inherits this: it governs the ordinary weeks,
        # and the milestone overrides it by design (issue #164 review).
        if milestone or shown_weekly:
            surfaces = _REPORT_AND_PROMPT
        else:
            surfaces = frozenset()
        entries.append(NotesPackEntry(
            kind=NoteKind.SEASON_COUNT,
            text=_season_count_text(
                manager_row.manager_name,
                view.count_label_one,
                view.count_label_many,
                view.occurrences,
                view.held_total,
                window,
                occurred_this_gameweek=occurred_this_gameweek,
                run_length=view.length,
                run_held=view.held_in_run,
                # A run-milestone condition is one whose whole point is the
                # unbroken sequence, so its line leads with the run it
                # actually fired on rather than a season total that would
                # leave the reader wondering why this week.
                run_framed=bool(view.count_policy.run_milestones),
            ),
            surfaces=surfaces,
            tier=_entry_tier(window, manager_key, rows_by_gameweek),
            window=window,
            manager_key=manager_key,
            manager_name=manager_row.manager_name,
            condition_key=condition_key,
            # Carried even though the count, not the run, is this entry's
            # subject: a run-framed line states a run length in its text, and
            # a `--format json` consumer reading the structured field beside
            # it must not be told the run is zero (issue #164 review).
            length=view.length,
            held_count=view.held_total,
            occurrences=view.occurrences,
        ))

    def _sort_key(entry: NotesPackEntry) -> tuple[int, str, str]:
        return (-(entry.occurrences or 0), entry.manager_name or "", entry.condition_key or "")

    entries.sort(key=_sort_key)
    return entries


# ---------------------------------------------------------------------------
# Season-phase entry (point 3, point 8)
# ---------------------------------------------------------------------------


def _season_phase_text(
    phase: SeasonPhase,
    gameweek: int,
    total_gameweeks: int,
    chip_split_gw: int,
    fpl_format: LeagueFormat,
) -> str:
    """Draft has no chip mechanics, so `PRE_CHIP_BOUNDARY` and `MIDPOINT` --
    the two phrasings that name the chip split -- fall back to the plain
    halfway-point framing for a draft league rather than mentioning chips
    that don't exist for it. `chip_split_gw` is also just `TOTAL_GAMEWEEKS
    // 2` (see `derive_season_phase`), so the halfway-point framing is still
    accurate, not merely chip-free.
    """
    if phase is SeasonPhase.OPENER:
        return f"GW{gameweek} is the season opener."
    if phase is SeasonPhase.PRE_CHIP_BOUNDARY:
        if fpl_format == "draft":
            return f"GW{gameweek} is before the season's halfway point (GW{chip_split_gw})."
        return f"GW{gameweek} is before the GW{chip_split_gw} chip-availability boundary."
    if phase is SeasonPhase.MIDPOINT:
        if fpl_format == "draft":
            return (
                f"GW{gameweek} is the season midpoint, past the GW{chip_split_gw} halfway "
                "point and before the run-in."
            )
        return f"GW{gameweek} is the season midpoint, past the GW{chip_split_gw} chip boundary and before the run-in."
    if phase is SeasonPhase.RUN_IN:
        return f"GW{gameweek} is in the run-in to the season finale (GW{total_gameweeks})."
    return f"GW{gameweek} is the season finale."


def _season_phase_entry(
    phase: SeasonPhase,
    gameweek: int,
    total_gameweeks: int,
    chip_split_gw: int,
    fpl_format: LeagueFormat,
) -> NotesPackEntry:
    """Prompt-only (KTD8): this is scene-setting context for the editorial
    writer, not a fact worth printing in the report body -- true for the
    classic phrasing and meaningless for the draft one (issue #187). A
    future phase whose note *is* worth showing the reader picks that up as
    its own per-phase surface choice here, rather than this function going
    back to a blanket `_REPORT_AND_PROMPT`.
    """
    return NotesPackEntry(
        kind=NoteKind.SEASON_PHASE,
        text=_season_phase_text(phase, gameweek, total_gameweeks, chip_split_gw, fpl_format),
        surfaces=_PROMPT_ONLY,
    )


# ---------------------------------------------------------------------------
# Coverage / negative-context entries (point 5, point 6, R17)
# ---------------------------------------------------------------------------


def _partition_coverage_entry(
    gameweek: int,
    league_start_gameweek: int,
    earliest: int | None,
    captured_gameweeks: list[int],
) -> NotesPackEntry:
    """Whether any history exists before `gameweek` at all, and -- if it does
    but starts later than the league itself did -- the R17 qualifier. Always
    produced, so an empty pack never reduces to "nothing to say".

    A "complete" claim additionally requires every gameweek from
    `league_start_gameweek` through `gameweek` to actually be in
    `captured_gameweeks` (R17) -- the earliest captured gameweek being early
    enough is necessary but not sufficient, since a gameweek somewhere in
    the middle can have been never captured at all (distinct from a
    captured-but-unknown row, which *is* in `captured_gameweeks`). Without
    this check "complete from its start" would still be claimed straight
    through a genuine mid-season hole.
    """
    if earliest is None or earliest >= gameweek:
        text = f"No league history has been recorded before GW{gameweek}."
    elif earliest > league_start_gameweek:
        text = (
            f"Recorded history for this league begins at GW{earliest}, later than the league's "
            f"start (GW{league_start_gameweek}); earlier gameweeks are not available."
        )
    else:
        missing = sorted(set(range(league_start_gameweek, gameweek + 1)) - set(captured_gameweeks))
        if missing:
            missing_list = ", ".join(f"GW{gw}" for gw in missing)
            text = (
                f"Recorded history for this league begins at GW{league_start_gameweek} but is "
                f"missing {missing_list}; those gameweeks were never captured."
            )
        else:
            text = (
                f"Recorded history for this league is complete from its start (GW{league_start_gameweek}) "
                f"through GW{gameweek}."
            )
    return NotesPackEntry(kind=NoteKind.COVERAGE, text=text, surfaces=_REPORT_AND_PROMPT)


def _joiner_coverage_entry(
    manager_name: str, manager_key: int, manager_earliest: int, league_start_gameweek: int,
) -> NotesPackEntry:
    text = (
        f"{manager_name}: recorded history begins at GW{manager_earliest}, later than the league's "
        f"start (GW{league_start_gameweek}); earlier gameweeks are not available for this manager."
    )
    return NotesPackEntry(
        kind=NoteKind.COVERAGE, text=text, surfaces=_REPORT_AND_PROMPT,
        manager_key=manager_key, manager_name=manager_name,
    )


def _earliest_gameweek_cache_file(store: LeagueHistoryStore) -> Path:
    """Path of this partition's earliest-captured-gameweek cache (R17).

    Lives alongside the counters projection's own cache file, under the same
    partition directory -- both are disposable, rebuildable caches for the
    same (season, format, league_id) partition -- but as its own file, not a
    field on the counters projection, so this module's cache stays entirely
    this module's own concern.
    """
    return counters_partition_dir(store.season, store.fpl_format, store.league_id) / "earliest_gameweek.json"


def _load_earliest_gameweek_cache(store: LeagueHistoryStore) -> dict[int, int]:
    """The persisted per-manager earliest-gameweek cache, or an empty dict if
    it must be (re)built. Fails open (KTD10): a missing file, one that fails
    to parse, one stamped with a version this code no longer produces, or
    one that claims a different partition than the one asked for -- all
    return an empty dict rather than raising, exactly like
    `league_history_counters._load_projection`'s fallback for its own cache.
    """
    path = _earliest_gameweek_cache_file(store)
    try:
        text = path.read_text(encoding="utf-8")
        cache = ManagerEarliestGameweekCache.model_validate_json(text)
    except FileNotFoundError:
        return {}
    except (OSError, ValidationError) as exc:
        logger.warning("Earliest-gameweek cache %s is unreadable, rescanning: %s", path, exc)
        return {}

    if (
        cache.version != EARLIEST_GAMEWEEK_CACHE_VERSION
        or cache.season != store.season
        or cache.fpl_format != store.fpl_format
        or cache.league_id != store.league_id
    ):
        return {}
    return dict(cache.earliest_gameweek)


def _save_earliest_gameweek_cache(store: LeagueHistoryStore, earliest_gameweek: dict[int, int]) -> None:
    """Persist the per-manager earliest-gameweek cache, best-effort -- a
    write failure here must not block the recap that triggered it; the next
    call simply finds a missing or stale file and rescans (KTD10)."""
    path = _earliest_gameweek_cache_file(store)
    cache = ManagerEarliestGameweekCache(
        season=store.season, fpl_format=store.fpl_format, league_id=store.league_id,
        earliest_gameweek=earliest_gameweek,
    )
    try:
        atomic_write_text(path, cache.model_dump_json())
    except OSError as exc:
        logger.warning("Could not persist earliest-gameweek cache to %s: %s", path, exc)


def _earliest_gameweeks_for_managers(
    store: LeagueHistoryStore, manager_keys: set[int], captured_gameweeks: list[int],
) -> dict[int, int]:
    """Each manager's earliest gameweek with any row -- OK or unknown status,
    presence of a row at all (R17).

    Backed by a persisted, per-manager cache (see
    `_load_earliest_gameweek_cache`): a manager already found on some
    earlier call is an O(1) lookup, never rescanned again. Only a manager
    not yet in the cache costs a scan -- ascending, with one shared read per
    gameweek and an early exit once every still-unknown manager is found --
    and it costs that scan at most once per manager for the lifetime of the
    partition, not once per week for the rest of the season: every manager
    passed in is, by construction, present in the pack's own target
    gameweek, so the scan always finds them by the time it reaches that
    gameweek and never has to run again for them afterwards.
    """
    cached = _load_earliest_gameweek_cache(store)
    to_find = {key for key in manager_keys if key not in cached}
    if not to_find:
        return {key: cached[key] for key in manager_keys if key in cached}

    found: dict[int, int] = {}
    remaining = set(to_find)
    for gameweek in captured_gameweeks:  # already ascending per captured_gameweeks()
        if not remaining:
            break
        try:
            resolved = store.resolved_gameweek(gameweek)
        except LeagueHistoryError as exc:
            logger.warning(
                "GW%s unreadable while scanning for managers' earliest captured gameweek in "
                "%s/%s-%s; skipped: %s",
                gameweek, store.season, store.fpl_format, store.league_id, exc,
            )
            continue
        hit = remaining & resolved.keys()
        for manager_key in hit:
            found[manager_key] = gameweek
        remaining -= hit

    if found:
        _save_earliest_gameweek_cache(store, {**cached, **found})

    result = {key: cached[key] for key in manager_keys if key in cached}
    result.update(found)
    return result


def _coverage_entries(
    *,
    store: LeagueHistoryStore,
    gameweek: int,
    league_start_gameweek: int,
    captured_gameweeks: list[int],
    cohort: dict[int, LeagueHistoryRow],
) -> list[NotesPackEntry]:
    earliest_overall = captured_gameweeks[0] if captured_gameweeks else None
    entries = [_partition_coverage_entry(gameweek, league_start_gameweek, earliest_overall, captured_gameweeks)]

    manager_earliest = _earliest_gameweeks_for_managers(store, set(cohort), captured_gameweeks)
    joiners: list[NotesPackEntry] = []
    for manager_key, row in cohort.items():
        joined_at = manager_earliest.get(manager_key)
        # A joiner is flagged relative to *both* the league's own start and
        # the partition's own earliest visible record: a manager present
        # since the partition's own first capture is not a differential
        # joiner even when that baseline itself postdates league_start_gameweek
        # (a whole-league mid-season tool adoption) -- that fact is already
        # covered once by the partition-level entry above, and repeating it
        # per manager would be pure noise.
        if (
            joined_at is not None
            and joined_at > league_start_gameweek
            and (earliest_overall is None or joined_at > earliest_overall)
        ):
            joiners.append(_joiner_coverage_entry(row.manager_name, manager_key, joined_at, league_start_gameweek))

    joiners.sort(key=lambda e: e.manager_name or "")
    entries.extend(joiners)
    return entries


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_notes_pack(
    store: LeagueHistoryStore,
    gameweek: int,
    *,
    league_start_gameweek: int = 1,
    total_gameweeks: int = TOTAL_GAMEWEEKS,
    chip_split_gw: int = CHIP_SPLIT_GW,
) -> NotesPack:
    """Assemble the notes pack for one gameweek's recap (R14, R15, R17).

    Every phase but the finale reads the counters projection via
    `compute_counters_through` (the cached, incrementally-advancing weekly
    path) plus a trailing window of raw rows bounded by
    `TRAILING_WINDOW_GAMEWEEKS`. The finale instead reads every captured
    gameweek and computes the projection with `rebuild_counters_through` -- a
    pure, point-in-time read with no cache to optimise for, appropriate for
    something that happens once a season (Success Criteria's flat
    weekly-cost rule).

    The trailing window bounds *raw row reads* only -- e.g. for an entry's
    fidelity tier -- never the streak entries themselves: a run's `length`,
    `held_in_run`, and `start_gameweek` always come from the counters
    projection's true, current state, however far back the run actually
    started.
    """
    phase = derive_season_phase(gameweek, total_gameweeks, chip_split_gw)
    captured_gameweeks = store.captured_gameweeks()

    if phase is SeasonPhase.FINALE:
        read_gameweeks = [gw for gw in captured_gameweeks if gw <= gameweek]
        projection = rebuild_counters_through(store, gameweek)
    else:
        read_gameweeks = list(range(max(1, gameweek - TRAILING_WINDOW_GAMEWEEKS + 1), gameweek + 1))
        projection = compute_counters_through(store, gameweek)

    rows_by_gameweek: dict[int, dict[int, LeagueHistoryRow]] = {}
    for gw in read_gameweeks:
        try:
            rows_by_gameweek[gw] = store.resolved_gameweek(gw)
        except LeagueHistoryError as exc:
            logger.warning(
                "GW%s unreadable while building the league history notes pack for %s/%s-%s; "
                "treated as uncaptured: %s",
                gw, store.season, store.fpl_format, store.league_id, exc,
            )
            rows_by_gameweek[gw] = {}

    cohort = rows_by_gameweek.get(gameweek, {})

    return NotesPack(
        season=store.season,
        fpl_format=store.fpl_format,
        league_id=store.league_id,
        gameweek=gameweek,
        phase=phase,
        league_start_gameweek=league_start_gameweek,
        season_phase_entry=_season_phase_entry(phase, gameweek, total_gameweeks, chip_split_gw, store.fpl_format),
        entries=_streak_entries(
            gameweek=gameweek, cohort=cohort, projection=projection, rows_by_gameweek=rows_by_gameweek,
        ),
        season_count_entries=_season_count_entries(
            gameweek=gameweek, cohort=cohort, projection=projection, rows_by_gameweek=rows_by_gameweek,
            milestone=is_season_milestone(gameweek, total_gameweeks, chip_split_gw),
            # The chip boundary is the season's halfway point (GW19 of 38),
            # so its deadline is where "second half" starts for the count
            # policies' first-in-second-half and second-half-only rules.
            second_half=gameweek > chip_split_gw,
        ),
        coverage_entries=_coverage_entries(
            store=store, gameweek=gameweek, league_start_gameweek=league_start_gameweek,
            captured_gameweeks=captured_gameweeks, cohort=cohort,
        ),
    )
