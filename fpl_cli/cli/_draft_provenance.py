"""How a draft player came to his owner, as the lookup commands say it (#460).

`fpl player` and `fpl stats` show a draft owner, or "Available", for every
player they list. The league's own history (`services/draft_provenance.py`,
read by `get_draft_ownership_mapping(with_provenance=True)`) also says how he
got there: his draft pick, the waiver or free-agent move, or the trade, and
for a player nobody holds, his last exit and whether he was ever drafted.
This module turns that into the commands' words and JSON.

A player is described only where the history agrees with the owner the
command shows, so a line never pairs one manager's name with another's
pick. Where the history could not be shown complete, nobody is described and
the commands read exactly as they did before, with `ownership_warnings()`
saying why.

Managers are named by the league's entries, never with a pronoun: the data
holds no one's gender, and the player is the only "him" it can stand behind.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from fpl_cli.season import season_label
from fpl_cli.services.draft_provenance import drafted_step

if TYPE_CHECKING:
    from fpl_cli.agents.common import DraftOwnership
    from fpl_cli.api.fpl import FPLClient
    from fpl_cli.services.draft_provenance import DraftInForce, DraftProvenance, DraftStep

# The `metadata.warnings` code for a history that could not be shown
# complete, here and in the league recap.
DRAFT_PROVENANCE_UNAVAILABLE = "draft_provenance_unavailable"

# How a player came to stand where he does: the kind of the latest link that
# brought him to his owner, or for a player nobody holds, whether the history
# ever held him.
DraftStandingKind = Literal["draft", "waiver", "free_agent", "trade", "released", "undrafted"]

# The `fpl stats` Draft column's tag for each move, and the words its key gives it.
_MOVE_TAGS: dict[str, tuple[str, str]] = {
    "waiver": ("W", "waiver"), "free_agent": ("FA", "free agent"), "trade": ("T", "trade"),
}

# The key under the `fpl stats` table to its tags.
STATS_TAG_KEY = "Draft: R3 = round-3 pick (R? round unknown), " + ", ".join(
    f"{tag} = {words}" for tag, words in _MOVE_TAGS.values()
)


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
        return drafted_step(self.chain)


def player_standing(
    provenance: DraftProvenance | None, element: int | None, owner: int | None,
) -> DraftStanding | None:
    """How the draft player *element* stands, or None where the history cannot say.

    *owner* is the entry the command shows holding him, None where it shows
    him available. None comes back for a history that is missing or could
    not be shown complete, a player with no draft id, and a player the
    history holds somewhere other than the command shows: a squad the
    ownership read missed shows its players available (#438), and a move
    applied to the coming gameweek is in the history before the squads show
    it.
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


def standing_for(ownership: DraftOwnership, main_id: int) -> DraftStanding | None:
    """How the main-game player *main_id* stands, against the owner *ownership* shows for him."""
    return player_standing(ownership.provenance, ownership.main_to_draft_id.get(main_id), ownership.owner_of(main_id))


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


def ownership_warnings(ownership: DraftOwnership) -> list[dict[str, str]]:
    """Every `metadata.warnings` entry the draft lines owe: squads the ownership
    read missed (#438), and a history that could not be shown complete."""
    from fpl_cli.api.fpl_draft import unread_squads_warning

    found = (unread_squads_warning(ownership.unread_squads), provenance_warning(ownership.provenance))
    return [w for w in found if w is not None]


async def draft_history_season(client: FPLClient) -> str | None:
    """The season whose saved draft picks the history may read, or None for the clock's."""
    try:
        return season_label(await client.get_season_year())
    except Exception:  # noqa: BLE001 — optional enrichment: None falls back to the clock's season
        return None


def manager_name(entry: int, names: Mapping[int, str]) -> str:
    """An entry's manager as the draft lines name them, by entry where the league does not."""
    return names.get(entry, f"Team #{entry}")


def _draft_words(step: DraftStep, draft: DraftInForce | None, who: str | None = None) -> str:
    """One draft pick in the lines' words, the one place they are written.

    "round-3 pick", "Cam's round-3 auto-pick", "Cam's pick, round unknown",
    "drafted, round unknown", with "in the GW20 redraft" where the league has
    drafted more than once and the draft must be named.
    """
    redraft = f" in the GW{draft.gameweek} redraft" if draft is not None and draft.redraft else ""
    whose = f"{who}'s " if who else ""
    pick = step.pick
    if pick is None:
        return f"{whose}pick{redraft}, round unknown" if who else f"drafted{redraft}, round unknown"
    return f"{whose}round-{pick.round} {'auto-pick' if pick.auto else 'pick'}{redraft}"


def _origin(standing: DraftStanding, names: Mapping[int, str]) -> str:
    """Whose pick he was in the draft in force: "Cam's round-5 pick", or "undrafted"."""
    drafted = standing.drafted
    if drafted is not None:
        return _draft_words(drafted, standing.draft, manager_name(drafted.entry, names))
    draft = standing.draft
    return f"undrafted in the GW{draft.gameweek} redraft" if draft is not None and draft.redraft else "undrafted"


def ownership_note(standing: DraftStanding, names: Mapping[int, str]) -> str:
    """How he came to stand where he does, for the brackets after "Owned by Alice" or "Available".

    "round-3 pick", "GW4 waiver pickup; Cam's round-5 pick", "acquired from
    Alice in a GW7 trade; undrafted", "released by Dan in GW5; Dan's round-9
    pick", "undrafted". The draft named is the one in force.
    """
    step = standing.latest
    if step is None:
        return _origin(standing, names)
    if standing.kind == "draft":
        return _draft_words(step, standing.draft)
    origin = _origin(standing, names)
    if standing.kind == "waiver":
        return f"GW{step.gameweek} waiver pickup; {origin}"
    if standing.kind == "free_agent":
        return f"GW{step.gameweek} free-agent signing; {origin}"
    if standing.kind == "trade":
        source = f" from {manager_name(step.from_entry, names)}" if step.from_entry is not None else ""
        return f"acquired{source} in a GW{step.gameweek} trade; {origin}"
    return f"released by {manager_name(step.entry, names)} in GW{step.gameweek}; {origin}"


def _step_text(step: DraftStep, draft: DraftInForce | None, names: Mapping[int, str]) -> str:
    who = manager_name(step.entry, names)
    if step.kind == "draft":
        return _draft_words(step, draft, who)
    if step.kind == "waiver":
        return f"waiver to {who} GW{step.gameweek}"
    if step.kind == "free_agent":
        return f"free agent to {who} GW{step.gameweek}"
    if step.kind == "trade":
        source = f" from {manager_name(step.from_entry, names)}" if step.from_entry is not None else ""
        return f"trade{source} to {who} GW{step.gameweek}"
    return f"released GW{step.gameweek}"


def history_text(standing: DraftStanding, names: Mapping[int, str]) -> str | None:
    """His whole chain on one line, or None where the draft line already names every step.

    The line names his latest step and his draft pick, so a chain of those
    two alone (a pick traded on, a pick let go) adds nothing:
    "Alice's round-9 pick; released GW2; waiver to Bob GW5; released GW6;
    waiver to Cam GW7".
    """
    named = 2 if standing.drafted is not None and len(standing.chain) > 1 else 1
    if len(standing.chain) <= named:
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
    tag = _MOVE_TAGS.get(standing.kind)
    return f"GW{step.gameweek} {tag[0]}" if tag else None


def _manager(entry: int | None, names: Mapping[int, str]) -> dict[str, Any] | None:
    return {"entry_id": entry, "name": manager_name(entry, names)} if entry is not None else None


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
