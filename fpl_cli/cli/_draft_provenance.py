"""How a draft player came to his owner, as the lookup commands say it (#460).

`fpl player` and `fpl stats` show a draft owner, or "Available", for every
player they list. The league's own history (`services/draft_provenance.py`,
read by `get_draft_ownership_mapping(provenance_season=...)`) also says how
he got there: his draft pick, the waiver or free-agent move, or the trade, and
for a player nobody holds, his last exit and whether he was ever drafted.
This module turns that into the commands' words and JSON.

A player is described only where the history agrees with the owner the
command shows, so a line never pairs one manager's name with another's
pick. Where the history could not be shown complete, nobody is described and
the commands read exactly as they did before, with `provenance_warning()`
saying why.

Managers are named by the league's entries, never with a pronoun: the data
holds no one's gender, and the player is the only "him" it can stand behind.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from fpl_cli.services.draft_provenance import DraftInForce, DraftProvenance, DraftStep

# The `metadata.warnings` code for a history that could not be shown
# complete; the league recap reports the same failure under the same code.
DRAFT_PROVENANCE_UNAVAILABLE = "draft_provenance_unavailable"

# How a player came to stand where he does: the kind of the latest link that
# brought him to his owner, or for a player nobody holds, whether the history
# ever held him.
DraftStandingKind = Literal["draft", "waiver", "free_agent", "trade", "released", "undrafted"]

# The `fpl stats` Draft column's key to its tags, shown under the table.
STATS_TAG_KEY = "Draft: R3 = round-3 pick (R? round unknown), W = waiver, FA = free agent, T = trade"


@dataclass(frozen=True)
class DraftStanding:
    """One player's place in the draft league, as the history gives it.

    ``owner`` is the entry holding him, None for a free agent. ``chain`` is
    his history oldest first, from the draft in force: empty for a player it
    never touched (``undrafted``), ending on the release that freed him for
    one it let go (``released``), and otherwise on the link that brought him
    to ``owner``.
    """

    owner: int | None
    kind: DraftStandingKind
    chain: tuple[DraftStep, ...]
    draft: DraftInForce | None

    @property
    def latest(self) -> DraftStep | None:
        return self.chain[-1] if self.chain else None

    @property
    def drafted(self) -> DraftStep | None:
        """His pick in the draft in force, None if nobody drafted him in it."""
        return self.chain[0] if self.chain and self.chain[0].kind == "draft" else None


def player_standing(
    provenance: DraftProvenance | None, element: int | None, owner: int | None,
) -> DraftStanding | None:
    """How the draft player *element* stands, or None where the history cannot say.

    *owner* is the entry the command shows holding him, None where it shows
    him available. None comes back for a history that is missing or could
    not be shown complete, a player with no draft id, and a player the
    history holds somewhere other than the command shows (a squad the
    ownership read missed shows its players available).
    """
    if provenance is None or not provenance.available or element is None:
        return None
    history = provenance.players.get(element)
    if owner is not None:
        if history is None or history.owner != owner:
            return None
        kind = history.latest.kind
        if kind == "release":
            # An owned player's chain ends on the link that brought him; never a release.
            return None
        return DraftStanding(owner=owner, kind=kind, chain=history.chain, draft=provenance.draft)
    if history is not None:
        return None
    released = provenance.released.get(element)
    if released is not None:
        return DraftStanding(owner=None, kind="released", chain=released.chain, draft=provenance.draft)
    return DraftStanding(owner=None, kind="undrafted", chain=(), draft=provenance.draft)


def provenance_warning(provenance: DraftProvenance | None) -> dict[str, str] | None:
    """The `metadata.warnings` entry for a history that could not be shown complete, else None."""
    if provenance is None or provenance.problem is None:
        return None
    return {
        "code": DRAFT_PROVENANCE_UNAVAILABLE,
        "message": (
            "The draft lines do not say how each player came to his owner, because "
            f"{provenance.problem}."
        ),
    }


def _name(entry: int, names: Mapping[int, str]) -> str:
    return names.get(entry, f"Team #{entry}")


def _redraft(draft: DraftInForce | None) -> int | None:
    """The redraft's gameweek where the league has drafted more than once, so a line must say which."""
    return draft.gameweek if draft is not None and draft.redraft else None


def _origin(standing: DraftStanding, names: Mapping[int, str]) -> str:
    """Whether and how he was drafted: "drafted by Cam, round 5", "undrafted"."""
    redraft = _redraft(standing.draft)
    drafted = standing.drafted
    if drafted is None:
        return f"undrafted in the GW{redraft} redraft" if redraft else "undrafted"
    who = _name(drafted.entry, names)
    pick = drafted.pick
    if pick is None:
        return f"drafted by {who}{f' in the GW{redraft} redraft' if redraft else ''}, round unknown"
    verb = "auto-picked" if pick.auto else "drafted"
    return f"{verb} by {who}, round {pick.round}{f' of the GW{redraft} redraft' if redraft else ''}"


def ownership_note(standing: DraftStanding, names: Mapping[int, str]) -> str:
    """How he came to stand where he does, for the brackets after "Owned by Alice" or "Available".

    "round-3 pick", "GW4 waiver pickup; drafted by Cam, round 5", "from
    Alice in a GW7 trade; undrafted", "released by Dan in GW5; drafted by
    Dan, round 9", "undrafted". The draft named is the one in force, and a
    redraft names itself.
    """
    step = standing.latest
    origin = _origin(standing, names)
    if step is None:
        return origin
    if standing.kind == "draft":
        redraft = _redraft(standing.draft)
        if step.pick is None:
            return f"drafted{f' in the GW{redraft} redraft' if redraft else ''}, round unknown"
        noun = "auto-pick" if step.pick.auto else "pick"
        return f"round-{step.pick.round} {noun}{f' of the GW{redraft} redraft' if redraft else ''}"
    if standing.kind == "waiver":
        return f"GW{step.gameweek} waiver pickup; {origin}"
    if standing.kind == "free_agent":
        return f"GW{step.gameweek} free-agent signing; {origin}"
    if standing.kind == "trade":
        source = f" from {_name(step.from_entry, names)}" if step.from_entry is not None else ""
        return f"acquired{source} in a GW{step.gameweek} trade; {origin}"
    return f"released by {_name(step.entry, names)} in GW{step.gameweek}; {origin}"


def ownership_text(standing: DraftStanding, names: Mapping[int, str]) -> str:
    """The whole draft line's text: "Owned by Alice (round-3 pick)", "Available (undrafted)"."""
    head = f"Owned by {_name(standing.owner, names)}" if standing.owner is not None else "Available"
    return f"{head} ({ownership_note(standing, names)})"


def _step_text(step: DraftStep, draft: DraftInForce | None, names: Mapping[int, str]) -> str:
    who = _name(step.entry, names)
    if step.kind == "draft":
        redraft = _redraft(draft)
        where = f" in the GW{redraft} redraft" if redraft else ""
        if step.pick is None:
            return f"drafted by {who}{where}"
        return f"round-{step.pick.round} {'auto-pick' if step.pick.auto else 'pick'} by {who}{where}"
    if step.kind == "waiver":
        return f"waiver to {who} GW{step.gameweek}"
    if step.kind == "free_agent":
        return f"free agent to {who} GW{step.gameweek}"
    if step.kind == "trade":
        source = f" from {_name(step.from_entry, names)}" if step.from_entry is not None else ""
        return f"trade{source} to {who} GW{step.gameweek}"
    return f"released GW{step.gameweek}"


def history_text(standing: DraftStanding, names: Mapping[int, str]) -> str | None:
    """His whole chain on one line, or None where it is a single step the draft line already says.

    "round-9 pick by Alice; released GW2; waiver to Bob GW5; released GW6;
    waiver to Cam GW7".
    """
    if len(standing.chain) < 2:
        return None
    return "; ".join(_step_text(step, standing.draft, names) for step in standing.chain)


def stats_tag(standing: DraftStanding) -> str | None:
    """The short tag `fpl stats` puts beside an owner: "R3", "R?", "GW4 W", "GW9 FA", "GW7 T".

    None for a free agent, whose cell stays "Available": the table keeps to
    one short column, and `fpl player` carries the rest.
    """
    step = standing.latest
    if step is None or standing.owner is None:
        return None
    if standing.kind == "draft":
        return f"R{step.pick.round}" if step.pick is not None else "R?"
    suffix = {"waiver": "W", "free_agent": "FA", "trade": "T"}.get(standing.kind)
    return f"GW{step.gameweek} {suffix}" if suffix else None


def _manager(entry: int | None, names: Mapping[int, str]) -> dict[str, Any] | None:
    return {"entry_id": entry, "name": _name(entry, names)} if entry is not None else None


def _step_json(step: DraftStep, names: Mapping[int, str]) -> dict[str, Any]:
    pick = step.pick
    return {
        "kind": step.kind,
        "gameweek": step.gameweek,
        "manager": _manager(step.entry, names),
        "from_manager": _manager(step.from_entry, names),
        "round": pick.round if pick else None,
        "pick": pick.pick if pick else None,
        "overall_pick": pick.overall if pick else None,
        "auto_pick": pick.auto if pick else None,
    }


def standing_json(standing: DraftStanding, names: Mapping[int, str]) -> dict[str, Any]:
    """`info.draft` in `fpl player --format json`: the line's facts, structured.

    ``acquisition`` is the standing's kind and ``gameweek`` its latest link's
    (the draft's own for a pick, the release's for a free agent, None for a
    player the history never held). The draft fields describe his pick in
    the draft in force, whatever has happened since, so a waiver pickup still
    says who drafted him: ``drafter``, ``round``, ``pick``, ``overall_pick``
    and ``auto_pick``, all null for a player nobody drafted, and the four
    pick fields null with ``round_unknown`` true where the draft's picks are
    lost. ``counterparty`` is the manager he came from in a trade, or who
    released him for a free agent. ``history`` is the whole chain, oldest
    first.
    """
    drafted = standing.drafted
    pick = drafted.pick if drafted else None
    step = standing.latest
    counterparty: int | None = None
    if step is not None and standing.kind == "trade":
        counterparty = step.from_entry
    elif step is not None and standing.kind == "released":
        counterparty = step.entry
    draft = standing.draft
    return {
        "owner": _manager(standing.owner, names),
        "acquisition": standing.kind,
        "gameweek": step.gameweek if step else None,
        "draft_id": draft.id if draft else None,
        "draft_gameweek": draft.gameweek if draft else None,
        "redraft": bool(draft and draft.redraft),
        "drafter": _manager(drafted.entry if drafted else None, names),
        "round": pick.round if pick else None,
        "pick": pick.pick if pick else None,
        "overall_pick": pick.overall if pick else None,
        "auto_pick": pick.auto if pick else None,
        "round_unknown": drafted is not None and pick is None,
        "counterparty": _manager(counterparty, names),
        "history": [_step_json(s, names) for s in standing.chain],
    }


def standings_tagged(standings: Sequence[DraftStanding | None]) -> bool:
    """Whether any of a table's standings earns a tag, so the table needs the key."""
    return any(s is not None and stats_tag(s) is not None for s in standings)
