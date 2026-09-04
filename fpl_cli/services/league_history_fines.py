"""Season fine tally, folded out of the league-history ledger (issue #136).

Fines are ruled per gameweek and rendered once, in that week's recap. The
ledger has recorded every ruling since capture existed (`LeagueHistoryRow.
fines`, keyed on `manager_key` so a mid-season rename cannot split a tally
and two managers sharing a display name cannot merge into one), but nothing
read them back -- so the question a league actually cares about, "who owes
what this season", had no answer.

This module answers it as a pure derivation over `store.captured_gameweeks()`
-> `store.resolved_gameweek(gw)`, which already resolves duplicate rows per
R3. No second source of truth, and no persistence of its own: the sweep is
one small file read per captured gameweek, and `LeagueHistoryStore` memoizes
each read for the life of the instance, so a recap that has already built its
notes pack pays nothing again for the gameweeks they share. If the sweep ever
does prove slow, the cache to add mirrors the counters projection
(`fpl_cli/services/league_history_counters.py`), not a parallel store.

Three rules this module exists to enforce:

- **Counts, not money.** `FineRule.penalty` is free text and `LedgerFine`
  carries a rule type plus a rendered message, so "4 last-place, 1 red-card"
  is supportable and "£14 owed" is not. A numeric amount would have to be
  stamped onto the row at capture time to be honest, which is a separate
  decision from reading back what is already stored.
- **An un-ruled gameweek is not an innocent one.** A manager with an unknown
  capture row (R19), a gameweek nobody captured, and a coarse-tier gameweek
  that structurally could not rule `red-card` all produce no fine -- and a
  naive fold scores every one of them as "not fined". Each is qualified
  instead, against the ledger's own coverage, the way `build_notes_pack`
  qualifies a streak claim rather than quietly rounding it off.
- **Rulings are frozen at capture.** Change a `below-threshold` value in
  settings and history stays as it was ruled. This is a ledger of rulings,
  not a re-derivation from today's config: re-ruling GW3 in March because
  the threshold moved in January would silently rewrite a season's history.
  The tally therefore only ever counts what a row already records.

A manager who joined mid-season keeps their real (lower) totals and is
qualified in the output rather than scaled up to a full season -- the same
call R17's joiner qualifier makes in the notes pack, and for the same
reason: the tally states what was ruled, and inventing a per-gameweek rate
for someone who was not there would state something nobody measured. A
manager who has since *left* the league keeps their totals too, bounded to
the gameweeks they were actually recorded for: fines are historical facts and
they still owe them, unlike a streak, which is a claim about the present and
is dropped for anyone outside the live cohort.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fpl_cli.models.league_history import (
    CaptureStatus,
    FidelityTier,
    LeagueFormat,
    LeagueHistoryRow,
)
from fpl_cli.services.league_history import LeagueHistoryError, LeagueHistoryStore
from fpl_cli.utils.gameweek import format_gameweek_list

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagerFineTally:
    """One manager's season fine record, and how much of the season it covers.

    `counts` holds only rule types that actually triggered at least once; a
    renderer wanting a column per configured rule reads
    `SeasonFinesTally.rule_types` and defaults the rest to zero.
    `unruled_gameweeks` is the honesty field: gameweeks inside this manager's
    own recorded span where no rule was ruled against them at all, so a zero
    total reads as "clean across the gameweeks that were ruled" rather than
    "clean all season".
    """

    manager_key: int
    manager_name: str
    counts: dict[str, int] = field(default_factory=dict)
    fined_gameweeks: list[int] = field(default_factory=list)
    ruled_gameweeks: list[int] = field(default_factory=list)
    # Which gameweeks ruled *each* rule against them, where
    # `ruled_gameweeks` only says that something was. A gameweek recorded at
    # the coarse tier rules `last-place` and not `red-card`, so the two
    # answer different questions and only this one can settle "has this rule
    # ever produced a fine against them before" (issue #233).
    ruled_gameweeks_by_rule: dict[str, list[int]] = field(default_factory=dict)
    unruled_gameweeks: list[int] = field(default_factory=list)
    # Earliest and latest gameweek in the span holding any row for them,
    # unknown-status ones included -- presence of a row at all, exactly as
    # R17 means it.
    first_recorded_gameweek: int | None = None
    last_recorded_gameweek: int | None = None

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def is_fully_ruled(self) -> bool:
        """Every gameweek in their recorded span ruled at least one rule
        against them -- the only case where a zero total means a clean season
        rather than a clean subset of one."""
        return not self.unruled_gameweeks


@dataclass(frozen=True)
class SeasonFinesTally:
    """Per-manager, per-rule fine counts for one partition, with qualifiers.

    `qualifiers` are finished, factual sentences meant to be rendered
    verbatim beside the table on whichever surface shows it -- console,
    report, or LLM prompt -- in the style `NotesPackEntry.text` is written
    for. They are never empty: a fully ruled span says so outright, because
    "these totals cover everything" is itself the fact a reader needs before
    trusting a zero.
    """

    season: str
    fpl_format: LeagueFormat
    league_id: int
    through_gameweek: int
    start_gameweek: int
    rule_types: list[str] = field(default_factory=list)
    managers: list[ManagerFineTally] = field(default_factory=list)
    qualifiers: list[str] = field(default_factory=list)

    @property
    def total_fines(self) -> int:
        return sum(manager.total for manager in self.managers)

    @property
    def fined_managers(self) -> list[ManagerFineTally]:
        """Only the managers who actually picked up a fine, in table order."""
        return [manager for manager in self.managers if manager.total]

    def unruled_gameweeks_for(
        self, manager: ManagerFineTally, rule_type: str, *, before: int,
    ) -> list[int]:
        """Gameweeks before `before` where `rule_type` was never ruled against
        this manager, so a fine of that type there is neither recorded nor
        ruled out (issue #233).

        The one question a claim like "their first last-place fine of the
        season" rests on, and the reason it is asked per rule rather than
        read off `ruled_gameweeks`: a coarse-tier gameweek rules `last-place`
        and structurally cannot rule `red-card`, so it is proof for one and a
        blind spot for the other. Everything else that can hide a ruling --
        a gameweek never captured, unreadable, reaching nobody, predating
        fine recording, or falling before this manager has a row at all --
        is absent from their per-rule set for free, so it lands here without
        needing to be enumerated a second time.

        Counts from the partition's own `start_gameweek`, so a league whose
        ledger begins mid-season names every gameweek before that rather than
        treating a missing history as a clean one.
        """
        ruled = set(manager.ruled_gameweeks_by_rule.get(rule_type, ()))
        return [gw for gw in range(self.start_gameweek, before) if gw not in ruled]

    @property
    def is_reportable(self) -> bool:
        """Worth surfacing at all. A league that has never configured a fine
        rule, and whose ledger holds no ruling from when it did, gets no
        fines section rather than an empty one announcing that nothing was
        ruled -- which is true of most leagues and useful to none of them."""
        return self.has_records and bool(self.rule_types)

    @property
    def has_records(self) -> bool:
        """Whether the ledger held anything at all across the span. False
        means the table is empty because nothing is recorded, which is a
        different statement from an empty table because nobody was fined."""
        return bool(self.managers)


# ---------------------------------------------------------------------------
# Reading one row
# ---------------------------------------------------------------------------


def _ruled_rule_types(row: LeagueHistoryRow) -> list[str]:
    """The rule types this row records as actually ruled.

    Empty for an unknown-status row however its `fines` list looks (R19: the
    capture never reached that manager), and empty for a row written before
    schema version 4, which recorded nothing either way. Both are qualified
    by the caller rather than read as an acquittal -- which is the whole
    point of the field: without it, "nobody was fined", "no rules were
    configured" and "no rule was ever checked" are one indistinguishable
    empty list.
    """
    if row.capture_status is CaptureStatus.UNKNOWN:
        return []
    return list(row.fine_rules_evaluated or ())


# ---------------------------------------------------------------------------
# Sweep state
# ---------------------------------------------------------------------------


@dataclass
class _Accumulator:
    """Mutable per-manager state while the sweep runs."""

    manager_key: int
    manager_name: str
    counts: dict[str, int] = field(default_factory=dict)
    fined_gameweeks: set[int] = field(default_factory=set)
    ruled_gameweeks: set[int] = field(default_factory=set)
    ruled_gameweeks_by_rule: dict[str, set[int]] = field(default_factory=dict)
    first_recorded_gameweek: int | None = None
    last_recorded_gameweek: int | None = None


@dataclass
class _SpanCoverage:
    """What the sweep learned about the span as a whole, not per manager."""

    uncaptured: list[int] = field(default_factory=list)
    unreadable: list[int] = field(default_factory=list)
    # Captured and holding rows, every one of them unknown (R19): the capture
    # ran and reached nobody. Distinct from `unrecorded`, where the capture
    # reached managers but recorded nothing about what it ruled.
    unreached: list[int] = field(default_factory=list)
    # Captured and holding rows, but at least one of them predates schema
    # version 4 and so records nothing about what it ruled. Distinct from
    # `unconfigured`, where the capture did record a ruling -- of nothing.
    unrecorded: list[int] = field(default_factory=list)
    unconfigured: list[int] = field(default_factory=list)
    # Rule type -> gameweeks that recorded a ruling which did not include it.
    # The coarse tier's structural gap -- no squad, so no red-card ruling --
    # is what usually lands here.
    partial: dict[str, list[int]] = field(default_factory=dict)
    coarse: set[int] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Shared renderings
# ---------------------------------------------------------------------------


def format_fine_breakdown(manager: ManagerFineTally) -> str:
    """One manager's counts as "2 last-place, 1 red-card".

    Shared by every surface that spells a manager's fines out -- the console
    highlights, the recap prompt -- so the human-facing wording and the
    wording the editorial is given can never drift apart.
    """
    return ", ".join(
        f"{count} {rule_type}" for rule_type, count in sorted(manager.counts.items())
    )


def serialize_manager_fine_tally(
    manager: ManagerFineTally, rule_types: Sequence[str],
) -> dict[str, object]:
    """One manager's tally, JSON-shaped, against a fixed column order.

    Shared by `league-fines --format json` and `league-recap --format json`
    so the same dataclass cannot reach two consumers in two shapes; the
    `counts` map is filled out against `rule_types` rather than emitted
    sparsely, so a configured rule nobody triggered is a recorded zero.
    """
    return {
        "manager_key": manager.manager_key,
        "manager_name": manager.manager_name,
        "total": manager.total,
        "counts": {rule: manager.counts.get(rule, 0) for rule in rule_types},
        "fined_gameweeks": manager.fined_gameweeks,
        "ruled_gameweeks": manager.ruled_gameweeks,
        "unruled_gameweeks": manager.unruled_gameweeks,
        "first_recorded_gameweek": manager.first_recorded_gameweek,
        "last_recorded_gameweek": manager.last_recorded_gameweek,
        "is_fully_ruled": manager.is_fully_ruled,
    }


# ---------------------------------------------------------------------------
# Coverage qualifiers
# ---------------------------------------------------------------------------


def _subject(gameweeks: Iterable[int]) -> str:
    """"That gameweek is" / "Those gameweeks are", for a clause that follows
    a rendered gameweek list."""
    return "That gameweek is" if len(set(gameweeks)) == 1 else "Those gameweeks are"


def _span_qualifiers(coverage: _SpanCoverage, start_gameweek: int, through_gameweek: int) -> list[str]:
    """Partition-level statements about what the span could not rule."""
    lines: list[str] = []
    if coverage.uncaptured:
        lines.append(
            f"{format_gameweek_list(coverage.uncaptured)} was never captured, so no fine was "
            f"ruled there for anyone and nobody's total counts it.",
        )
    if coverage.unreadable:
        lines.append(
            f"{format_gameweek_list(coverage.unreadable)} could not be read and is left out of "
            f"these totals; move the file aside and re-run the recap to recapture it.",
        )
    if coverage.unreached:
        lines.append(
            f"{format_gameweek_list(coverage.unreached)} was captured but the capture reached "
            f"nobody, so no fine was ruled there for anyone and nobody's total counts it; "
            f"re-run the recap to retry it.",
        )
    if coverage.unrecorded:
        # Deliberately silent on *why*: a row captured before fine rulings
        # were recorded and a row whose rule evaluation raised look
        # identical from here, and naming either cause would assert more
        # than the ledger holds.
        lines.append(
            f"{format_gameweek_list(coverage.unrecorded)} holds no record of what was ruled, so "
            f"the fines counted there are real but a zero is not proof of a clean week.",
        )
    if coverage.unconfigured:
        # Likewise: an empty ruling means no rule *could be* ruled there,
        # which is "nothing configured" for a detailed capture and "nothing
        # configured that this tier can rule" for a coarse one.
        cause = ""
        if set(coverage.unconfigured) <= coverage.coarse:
            cause = (
                f" {_subject(coverage.unconfigured)} recorded at the coarse tier, which carries "
                f"no squad, so a rule needing one was never rulable there."
            )
        lines.append(
            f"{format_gameweek_list(coverage.unconfigured)} recorded a ruling on no rules at "
            f"all, so no fine could be recorded there.{cause}",
        )
    for rule_type, gameweeks in sorted(coverage.partial.items()):
        # Naming the cause where the ledger can prove it: the coarse tier is
        # the manager-history endpoint, which returns headline numbers and no
        # squad, so a rule needing one was never merely skipped there.
        cause = ""
        if set(gameweeks) <= coverage.coarse:
            cause = (
                f" {_subject(gameweeks)} recorded at the coarse tier, which carries no squad, so "
                f"that ruling is not recoverable there."
            )
        lines.append(
            f"'{rule_type}' was not ruled in {format_gameweek_list(gameweeks)}, so no "
            f"'{rule_type}' fine can appear against anyone there.{cause}",
        )
    if not lines:
        lines.append(
            f"Every gameweek from GW{start_gameweek} through GW{through_gameweek} was ruled, so "
            f"these totals cover the whole span.",
        )
    return lines


def _manager_qualifiers(
    managers: list[ManagerFineTally], start_gameweek: int, league_wide_gaps: set[int],
) -> list[str]:
    """Per-manager statements: joiners, and gaps that are theirs alone.

    A gameweek nobody was ruled in -- never captured, unreadable, or holding
    no ruling at all -- is stated once at the partition level, so repeating
    it under every manager's name would turn one fact into a line per member
    and bury the gaps that really are personal (their own unknown capture
    row). Their table row still carries the asterisk either way, and the
    partition-level line explains it.
    """
    lines: list[str] = []
    for manager in managers:
        joined = manager.first_recorded_gameweek
        if joined is not None and joined > start_gameweek:
            lines.append(
                f"{manager.manager_name}: recorded history begins at GW{joined}, later than "
                f"GW{start_gameweek}; their totals cover fewer gameweeks than a manager "
                f"recorded for the whole span.",
            )
        personal = [gw for gw in manager.unruled_gameweeks if gw not in league_wide_gaps]
        if personal:
            lines.append(
                f"{manager.manager_name}: no fine was ruled against them in "
                f"{format_gameweek_list(personal)}, so their total covers "
                f"{len(manager.ruled_gameweeks)} ruled gameweek(s), not their whole span.",
            )
    return lines


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_season_fines_tally(
    store: LeagueHistoryStore,
    through_gameweek: int,
    *,
    league_start_gameweek: int | None = None,
    rule_types: Sequence[str] | None = None,
) -> SeasonFinesTally:
    """Fold one partition's ledger into per-manager, per-rule season totals.

    `league_start_gameweek` is the league's own first scored gameweek where
    the caller knows it (the recap does; a standalone read of the ledger does
    not, since that number lives in the API rather than in the store).
    Passing None falls back to the partition's earliest captured gameweek,
    which never invents a gap the ledger cannot see -- a league created at
    GW12 has no GW1 to be missing.

    `rule_types` is the currently-configured rule order, used to order the
    table's columns and to keep a configured-but-never-triggered rule visible
    as a zero column. Rule types observed in the ledger but no longer
    configured are appended after them rather than dropped: they were ruled,
    so hiding them would lose recorded history.

    Never raises for a store problem: an unreadable gameweek is reported as a
    qualifier and left out of the totals, exactly as R4 requires of every
    other ledger reader.
    """
    captured = [gw for gw in store.captured_gameweeks() if gw <= through_gameweek]
    # Membership only -- `captured` stays a list because its order and first
    # element are what set the span below.
    captured_set = set(captured)
    start_gameweek = (
        league_start_gameweek
        if league_start_gameweek is not None
        else (captured[0] if captured else 1)
    )
    span = list(range(start_gameweek, through_gameweek + 1))

    accumulators: dict[int, _Accumulator] = {}
    coverage = _SpanCoverage()
    observed_rule_types: list[str] = []
    ruled_types_by_gameweek: dict[int, set[str]] = {}
    populated: list[int] = []

    for gameweek in span:
        if gameweek not in captured_set:
            coverage.uncaptured.append(gameweek)
            continue
        try:
            resolved = store.resolved_gameweek(gameweek)
        except LeagueHistoryError as exc:
            store.log_unreadable(
                gameweek, exc, context="while tallying fines; left out of the totals",
            )
            coverage.unreadable.append(gameweek)
            continue
        if not resolved:
            coverage.uncaptured.append(gameweek)
            continue

        populated.append(gameweek)
        ruled_here: set[str] = set()
        predates_recording = False
        reached_anyone = False

        for manager_key, row in resolved.items():
            accumulator = accumulators.get(manager_key)
            if accumulator is None:
                accumulator = _Accumulator(manager_key=manager_key, manager_name=row.manager_name)
                accumulators[manager_key] = accumulator
            # Ascending sweep, so the last name seen is the current one: a
            # manager who renamed mid-season is tallied under the name they
            # use now, while the key keeps every earlier gameweek attached.
            accumulator.manager_name = row.manager_name
            if accumulator.first_recorded_gameweek is None:
                accumulator.first_recorded_gameweek = gameweek
            accumulator.last_recorded_gameweek = gameweek

            # Counted even from a row that never recorded what it ruled: the
            # fine itself is recorded history and dropping it would lose a
            # real ruling. What such a row cannot support is the *absence* of
            # a fine, which is why the gameweek is qualified below instead.
            for fine in row.fines:
                if row.capture_status is CaptureStatus.UNKNOWN:
                    continue
                accumulator.counts[fine.rule_type] = accumulator.counts.get(fine.rule_type, 0) + 1
                accumulator.fined_gameweeks.add(gameweek)

            row_rules = _ruled_rule_types(row)
            if row_rules:
                accumulator.ruled_gameweeks.add(gameweek)
                for rule_type in row_rules:
                    accumulator.ruled_gameweeks_by_rule.setdefault(
                        rule_type, set(),
                    ).add(gameweek)
                ruled_here.update(row_rules)
            if row.capture_status is CaptureStatus.OK:
                reached_anyone = True
                if row.fine_rules_evaluated is None:
                    predates_recording = True
            if row.tier is FidelityTier.COARSE:
                coverage.coarse.add(gameweek)

        for rule_type in sorted(ruled_here):
            if rule_type not in observed_rule_types:
                observed_rule_types.append(rule_type)

        if ruled_here:
            ruled_types_by_gameweek[gameweek] = ruled_here
        elif not reached_anyone:
            # Every row unknown (R19). Not "nothing was configured": the
            # capture never got far enough to rule anything either way.
            coverage.unreached.append(gameweek)
        elif predates_recording:
            coverage.unrecorded.append(gameweek)
        else:
            # A recorded ruling of nothing: rows carry `fine_rules_evaluated`
            # and it is empty, so no rule was rulable here.
            coverage.unconfigured.append(gameweek)

    ordered_rule_types = [*(rule_types or ())]
    ordered_rule_types.extend(r for r in observed_rule_types if r not in ordered_rule_types)

    # Only gameweeks that recorded a ruling can be said to have *missed* a
    # rule. A gameweek that recorded nothing is already stated once, in its
    # own qualifier, and claiming a specific rule went unruled there would
    # assert more than the row supports.
    for gameweek, ruled_types in ruled_types_by_gameweek.items():
        for rule_type in ordered_rule_types:
            if rule_type not in ruled_types:
                coverage.partial.setdefault(rule_type, []).append(gameweek)

    last_populated = populated[-1] if populated else None
    managers: list[ManagerFineTally] = []
    for accumulator in accumulators.values():
        first = accumulator.first_recorded_gameweek
        last = accumulator.last_recorded_gameweek
        # Still in the league (their last row is in the most recent gameweek
        # holding any rows) means their span runs to the end, so a trailing
        # uncaptured gameweek counts against their coverage. Otherwise they
        # left, and gameweeks after their final row were never theirs to be
        # ruled in.
        personal_end = through_gameweek if last is not None and last == last_populated else last
        personal_span = (
            set(range(first, personal_end + 1))
            if first is not None and personal_end is not None
            else set()
        )
        managers.append(ManagerFineTally(
            manager_key=accumulator.manager_key,
            manager_name=accumulator.manager_name,
            counts=dict(sorted(accumulator.counts.items())),
            fined_gameweeks=sorted(accumulator.fined_gameweeks),
            ruled_gameweeks=sorted(accumulator.ruled_gameweeks),
            ruled_gameweeks_by_rule={
                rule_type: sorted(gameweeks)
                for rule_type, gameweeks in sorted(
                    accumulator.ruled_gameweeks_by_rule.items(),
                )
            },
            unruled_gameweeks=sorted(personal_span - accumulator.ruled_gameweeks),
            first_recorded_gameweek=first,
            last_recorded_gameweek=last,
        ))
    managers.sort(key=lambda m: (-m.total, m.manager_name, m.manager_key))

    qualifiers: list[str] = []
    if not managers:
        qualifiers.append(
            f"No league history has been recorded through GW{through_gameweek}, so there is "
            f"nothing to tally.",
        )
    else:
        qualifiers.extend(_span_qualifiers(coverage, start_gameweek, through_gameweek))
        qualifiers.extend(_manager_qualifiers(
            managers, start_gameweek,
            # Every gameweek the span-level lines above already account for.
            set(coverage.uncaptured) | set(coverage.unreadable)
            | set(coverage.unreached) | set(coverage.unrecorded)
            | set(coverage.unconfigured),
        ))

    return SeasonFinesTally(
        season=store.season,
        fpl_format=store.fpl_format,
        league_id=store.league_id,
        through_gameweek=through_gameweek,
        start_gameweek=start_gameweek,
        rule_types=ordered_rule_types,
        managers=managers,
        qualifiers=qualifiers,
    )
