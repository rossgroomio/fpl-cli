"""Checks the league-recap editorial against the data its prompt carried (#357, #359).

The prompt states every rule these read, and the model has broken each of
them anyway: a manager named as beaten in a waiver race they won, a
manager's whole-gameweek net pinned on one transfer, a tie nobody shared.
Where the claim is about known strings -- managers, players, figures, ranks
-- it can be read back mechanically instead of trusting a rule the model
has already ignored once.

Each check is narrow by design. It reads only the clauses it can pin down
and lets everything else pass, so a problem it returns is a sentence the
data contradicts, never a guess -- a false alarm costs a retry and puts a
warning above a correct editorial. `EDITORIAL_CHECKS` is the one registry:
each check's warning code, the prose its JSON warning opens with, what the
retry is told, and the checker itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from fpl_cli.cli._league_recap_types import (
    LeagueRecapData,
    RecapContestedClaim,
    RecapManagerEntry,
    contested_draft_claims,
    format_contested_claim,
    place_label,
    points_label,
)
from fpl_cli.utils.text import ORDINAL_WORDS

# The JSON warning code each check raises when a claim it caught survives
# the retry. Stable for scripts, like every code on that channel.
RECAP_WARNING_CONTESTED_MISATTRIBUTION = "synthesis_contested_misattribution"
RECAP_WARNING_NET_MISATTRIBUTION = "synthesis_net_misattribution"
RECAP_WARNING_UNSUPPORTED_TIE = "synthesis_unsupported_tie"


# =============================================================================
# Contested waiver races (#357)
# =============================================================================

# The verbs that put a manager in a contested race's roles. The voice comes
# from the sentence, never the verb alone: "Alice beat Bob to Isak" and "Alice
# pipped Bob to Isak" make Alice the winner, while "Bob was beat to Isak",
# "Bob got pipped to Isak" and "Bob was outbid by Alice for Isak" make Bob the
# beaten one. Only these three are passive whatever surrounds them. Bare
# "lost" is not a cue at all -- "lost 3 points on Isak" and "lost Isak to
# injury" are not races -- only "lost out" and "lost the race" are.
_CUE_RE = re.compile(
    r"(?<!\w)(beat|beats|beating|beaten|pipped|edged out|outbid|missed out|lost out"
    r"|lost the (?:race|battle|scrap|tussle|fight))(?!\w)",
    re.IGNORECASE,
)
_PASSIVE_CUE_PREFIXES = ("beaten", "missed out", "lost")
# "beat" with nobody after it is no race claim ("Alice beat everyone to Isak"
# names no rival); the cues that read passively bare ("Bob, pipped to Isak")
# are the rest.
_ACTIVE_ONLY_CUES = frozenset({"beat", "beats", "beating"})
_AUXILIARY_RE = re.compile(
    r"(?<!\w)(?:was|were|is|are|got|get|gets|getting|been|being)\s*$", re.IGNORECASE,
)
_BY_RE = re.compile(r"^\s*by\s*$", re.IGNORECASE)
_NEGATION_RE = re.compile(r"(?<!\w)(?:not|never|nobody|no one)(?!\w)|n['’]t(?!\w)", re.IGNORECASE)
# What may sit between a cue and the player it names: a race preposition, with
# only the furniture of a race phrase before it ("to", "on", "the race for",
# "to the punch on") and an optional "both" after. Anything else -- "3 points
# on", "thanks to" -- is a different sentence about the same names.
_RACE_PREPOSITIONS = frozenset({"to", "for", "on", "over"})
_RACE_FILLER = _RACE_PREPOSITIONS | frozenset({
    "the", "a", "race", "battle", "scrap", "tussle", "fight", "punch", "signing", "of", "waiver",
})
_RACE_LINK_MAX_WORDS = 4
_SUBJECT_CUE_MAX_WORDS = 6
# Names in one list: "Alice, Bob and Cam", "Alice & Bob".
_LIST_JOIN_RE = re.compile(r"^\s*(?:,|,?\s*(?:and|&))\s*$", re.IGNORECASE)
# A comma before "and" joins a list only inside one of three or more ("Alice,
# Bob, and Cam"). Between two names it is a clause boundary: "Giles went to
# Dan, and Bob was beaten to King" says nothing about Dan.
_SERIAL_AND_RE = re.compile(r"^\s*,\s*(?:and|&)\s*$", re.IGNORECASE)
# A singular verb before the cue takes a single subject, whatever list the
# name before it might otherwise seem to close.
_SINGULAR_AUXILIARY_RE = re.compile(
    r"(?<!\w)(?:was|is|got|gets|has|wasn['\u2019]t|isn['\u2019]t)(?!\w)", re.IGNORECASE,
)
# What may follow a player for him to stay in a list: the list's end, its
# next join, "respectively" or a trailing "by <winner>". "Bob missed out on
# King, and Bogle was the consolation" opens a new clause with Bogle, and he
# is not part of the race Bob lost.
_PLAYER_LIST_CONTINUES_RE = re.compile(
    r"^\s*(?:$|[,.;:!?)]|(?:and|&|respectively|by)(?!\w))", re.IGNORECASE,
)
_RESPECTIVELY_RE = re.compile(r"^\s*,?\s*respectively(?!\w)", re.IGNORECASE)
# A clause ends at sentence punctuation followed by a space (so "£4.5m" stays
# whole), at a semicolon or colon, or at a line break. Every check reads a
# claim only against what its own clause names.
_CLAUSE_BREAK_RE = re.compile(r"[.!?;:](?=\s|$)|\n")
_EMPHASIS_RE = re.compile(r"[*_]")
_POSSESSIVE_RE = re.compile(r"['’]s(?!\w)")


@dataclass(frozen=True)
class _Mention:
    start: int
    end: int
    name: str
    is_manager: bool


def _mention_aliases(manager_names: Sequence[str], players: set[str]) -> dict[str, tuple[str, bool]]:
    """Every string the checker reads as a name, mapped to (canonical, is_manager).

    Managers are matched by their full name, and by a first or last name no
    other manager shares -- the editorial reaches for "Hill" on a second
    mention -- so long as that short form is not also a contested player's
    name. A full name that collides with a player stays the manager's.
    """
    names: dict[str, tuple[str, bool]] = {player: (player, False) for player in players}
    full = {name.strip() for name in manager_names if name.strip()}
    for name in full:
        names[name] = (name, True)
    counts: dict[str, int] = {}
    owner: dict[str, str] = {}
    for name in full:
        parts = name.split()
        if len(parts) < 2:
            continue
        for part in {parts[0], parts[-1]}:
            counts[part] = counts.get(part, 0) + 1
            owner[part] = name
    for part, count in counts.items():
        if count == 1 and len(part) >= 3 and part not in names:
            names[part] = (owner[part], True)
    return names


def _player_names(managers: Sequence[RecapManagerEntry]) -> frozenset[str]:
    """Every player name the recap data carries: squads, captains, moves and
    lost claims. `auto_subs` adds none -- its lines are sentences ("King on
    for Wood (6 pts)") about squad players already counted."""
    names: set[str] = set()
    for m in managers:
        names.update(p["name"] for p in m.get("squad", []))
        names.update((m.get("captain", ""), m.get("vice_captain", "")))
        for move in [*m.get("transfers", []), *m.get("transactions", []), *m.get("lost_claims", [])]:
            names.update((move["player_in"], move["player_out"]))
    return frozenset(name for name in names if name)


@dataclass(frozen=True)
class RecapNames:
    """Every name the recap data carries, read in one alternation (#379).

    The alternation runs longest first, so a name is read whole rather than
    as a shorter one inside it: "Gibbs-White" is not a "White", and "Bob
    White" is not the player "White". That only holds against names in the
    same alternation, so it is built once per editorial over every player,
    every manager and every short form a manager goes by, and each check
    keeps the matches it is after. A check that read only its own names
    found a bystander's surname inside "Gibbs-White" and stood down on the
    very sentence it was written for. Build one with `of()`.
    """

    managers: tuple[str, ...]
    players: frozenset[str]
    pattern: re.Pattern[str] | None

    @classmethod
    def of(cls, managers: Sequence[RecapManagerEntry]) -> RecapNames:
        manager_names = tuple(m["manager_name"] for m in managers)
        players = _player_names(managers)
        names = {*players, *_mention_aliases(manager_names, set())}
        pattern = re.compile(
            r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(names, key=lambda n: (-len(n), n))) + r")(?!\w)",
        ) if names else None
        return cls(manager_names, players, pattern)

    def find(self, text: str) -> list[re.Match[str]]:
        """Every name in `text`, in order, each read whole."""
        return list(self.pattern.finditer(text)) if self.pattern else []


def _find_mentions(text: str, names: RecapNames, wanted: Mapping[str, tuple[str, bool]]) -> list[_Mention]:
    """Every name in `text` that `wanted` maps, in order. Every other name
    is read too and dropped, so none of `wanted` is found inside one. A
    player in the possessive ("King's bonus") is not the player a race was
    for, so he is not read as one."""
    mentions: list[_Mention] = []
    for match in names.find(text):
        if match.group(0) not in wanted:
            continue
        canonical, is_manager = wanted[match.group(0)]
        if not is_manager and _POSSESSIVE_RE.match(text, match.end()):
            continue
        mentions.append(_Mention(match.start(), match.end(), canonical, is_manager))
    return mentions


def _plain(gap: str) -> str:
    return _EMPHASIS_RE.sub("", gap)


def _gap_after(text: str, mentions: list[_Mention], index: int) -> str:
    following = mentions[index + 1].start if index + 1 < len(mentions) else len(text)
    return text[mentions[index].end:following]


def _race_link(gap: str) -> bool:
    """Whether `gap` joins a cue (or the rival after it) to the player the
    race was for: a few words of race phrasing ending on a preposition."""
    plain = _plain(gap)
    if _CLAUSE_BREAK_RE.search(plain) or "," in plain:
        return False
    words = plain.lower().split()
    if words and words[-1] == "both":
        words.pop()
    return (
        0 < len(words) <= _RACE_LINK_MAX_WORDS
        and words[-1] in _RACE_PREPOSITIONS
        and all(word in _RACE_FILLER for word in words)
    )


def _manager_group(text: str, mentions: list[_Mention], index: int, *, step: int) -> list[int]:
    """The indices of the manager list `mentions[index]` belongs to, walking
    in `step`'s direction while each gap is a list join, in text order."""
    if not 0 <= index < len(mentions) or not mentions[index].is_manager:
        return []
    def join(a: int, b: int) -> str:
        left, right = sorted((a, b))
        return _plain(text[mentions[left].end:mentions[right].start])

    members = [index]
    while True:
        there = members[-1] + step
        if not 0 <= there < len(mentions) or not mentions[there].is_manager:
            break
        gap = join(members[-1], there)
        if not _LIST_JOIN_RE.match(gap):
            break
        if _SERIAL_AND_RE.match(gap):
            # Only the last join of a list of three or more carries ", and":
            # the name beyond it must be comma-joined to yet another.
            further = there + step
            if len(members) > 1 or not (
                0 <= further < len(mentions)
                and mentions[further].is_manager
                and join(there, further).strip() == ","
            ):
                break
        members.append(there)
    return sorted(members)


def _player_group(text: str, mentions: list[_Mention], index: int) -> list[int]:
    """The indices of the player list that opens at `mentions[index]`. A
    joined name only extends it when what follows him ends or continues the
    list, so a later clause that merely opens on a player stays out."""
    if not 0 <= index < len(mentions) or mentions[index].is_manager:
        return []
    members = [index]
    while True:
        there = members[-1] + 1
        if there >= len(mentions) or mentions[there].is_manager:
            break
        if not _LIST_JOIN_RE.match(_plain(text[mentions[members[-1]].end:mentions[there].start])):
            break
        if not _PLAYER_LIST_CONTINUES_RE.match(_plain(_gap_after(text, mentions, there))):
            break
        members.append(there)
    return members


def _pairs(
    text: str, mentions: list[_Mention], managers: list[int], players: list[int],
) -> list[tuple[int, int]]:
    """Which manager the sentence ties to which player: position by position
    when the player list closes on "respectively" and the two lists match in
    length, every pairing otherwise."""
    if (
        len(players) > 1
        and len(managers) == len(players)
        and _RESPECTIVELY_RE.match(_plain(_gap_after(text, mentions, players[-1])))
    ):
        return list(zip(managers, players, strict=True))
    return [(m, p) for m in managers for p in players]


@dataclass(frozen=True)
class _RaceClaim:
    """One clause's reading: who it says won, who it says was beaten, and
    the players it says the race was for -- all indices into the mentions."""

    winners: list[int]
    beaten: list[int]
    players: list[int]


def _read_claim(text: str, mentions: list[_Mention], i: int) -> _RaceClaim | None:
    """The race claim a cue between `mentions[i]` and `mentions[i + 1]`
    makes, or None when the clause cannot be pinned to roles.

    The forms read: "Bob (was) beaten to / missed out on Isak (by Alice)",
    "Alice beat / pipped Bob to Isak", and "Bob was pipped / beat by Alice to
    Isak". The manager list before the cue is the subject; a negation there
    ("was never beaten to") drops the clause.
    """
    gap = text[mentions[i].end:mentions[i + 1].start]
    cues = list(_CUE_RE.finditer(gap))
    if not cues:
        return None
    cue = cues[-1]  # the cue nearest the names after it governs them
    subjects = _manager_group(text, mentions, i, step=-1)
    if not subjects:
        return None
    before = _plain(gap[:cue.start()])
    if (
        _CLAUSE_BREAK_RE.search(before)
        or len(before.split()) > _SUBJECT_CUE_MAX_WORDS
        or _NEGATION_RE.search(before)
    ):
        return None
    if _SINGULAR_AUXILIARY_RE.search(before):
        subjects = subjects[-1:]
    word = cue.group(1).lower()
    after = gap[cue.end():]
    by_follows = bool(_BY_RE.match(_plain(after)))
    passive = word.startswith(_PASSIVE_CUE_PREFIXES) or bool(_AUXILIARY_RE.search(before)) or by_follows

    nxt = mentions[i + 1]
    if nxt.is_manager:
        # A rival follows the cue: "beat Bob to", or "beaten by Alice to".
        if passive and not by_follows:
            return None
        if not passive and after.strip():
            return None
        rivals = _manager_group(text, mentions, i + 1, step=1)
        first_player = rivals[-1] + 1
        if (
            first_player >= len(mentions)
            or mentions[first_player].is_manager
            or not _race_link(text[mentions[rivals[-1]].end:mentions[first_player].start])
        ):
            return None
        players = _player_group(text, mentions, first_player)
        if passive:
            return _RaceClaim(winners=rivals, beaten=subjects, players=players)
        return _RaceClaim(winners=subjects, beaten=rivals, players=players)

    if not _race_link(after) or (word in _ACTIVE_ONLY_CUES and not passive):
        return None
    players = _player_group(text, mentions, i + 1)
    # "Bob was beaten to Isak by Alice": the winner trails the player list.
    winners: list[int] = []
    after_players = players[-1] + 1
    if (
        after_players < len(mentions)
        and mentions[after_players].is_manager
        and _BY_RE.match(_plain(_gap_after(text, mentions, players[-1])))
    ):
        winners = _manager_group(text, mentions, after_players, step=1)
    return _RaceClaim(winners=winners, beaten=subjects, players=players)


def check_contested_attributions(
    summary: str,
    contests: Sequence[RecapContestedClaim],
    names: RecapNames,
) -> list[str]:
    """Every race the editorial puts a manager in the wrong role of (#357).

    The "Contested players" lines are the only source for who won a player
    and who was beaten to him, and the prompt says so -- but the model has
    still folded two adjacent lines into one "respectively" clause and drawn
    the second beaten manager from the next line's winner. Both the player
    and the manager names are known strings, so the assertion is checkable
    without trusting the model to honour a rule it has already broken once.

    Deliberately narrow: a clause is read only where a race verb sits between
    a list of managers and a list of contested players, joined by race
    phrasing ("to", "for", "on", "the race for"), in a voice the sentence
    settles ("Bob was beaten to Isak", "Alice beat Bob to Isak", "Bob was
    pipped by Alice to Isak", "Bob and Cam missed out on Isak and Wood
    respectively"). A clause it cannot pin to roles -- a negation, a
    possessive, a points swing -- is left alone rather than guessed at, so
    every problem returned is a sentence that said something the data
    contradicts. A list of managers against a list of players without
    "respectively" asserts every pairing, which is what the reader takes
    from it too. Two contested players can share a name; a claim holds when
    any race under that name bears it out. `names` is `RecapNames.of()` over
    the managers the races came from. Returns one line per wrong (manager,
    player, role), in the order the editorial makes the claims.
    """
    if not contests:
        return []
    races: dict[str, list[RecapContestedClaim]] = {}
    for contest in contests:
        races.setdefault(contest["player"], []).append(contest)
    mentions = _find_mentions(summary, names, _mention_aliases(names.managers, set(races)))

    problems: list[str] = []

    def assert_role(manager: str, player: str, *, won: bool) -> None:
        candidates = races[player]
        if won and any(race["winner"] == manager for race in candidates):
            return
        if not won and any(
            manager in (c["manager_name"] for c in race["losers"]) for race in candidates
        ):
            return
        lines = " / ".join(f'"{format_contested_claim(race)}"' for race in candidates)
        problem = (
            f"the editorial says {manager} {'won' if won else 'was beaten to'} {player}, "
            f"but the contested line reads: {lines}"
        )
        if problem not in problems:
            problems.append(problem)

    for i in range(len(mentions) - 1):
        claim = _read_claim(summary, mentions, i)
        if claim is None:
            continue
        for winner, player in _pairs(summary, mentions, claim.winners, claim.players):
            assert_role(mentions[winner].name, mentions[player].name, won=True)
        for beaten, player in _pairs(summary, mentions, claim.beaten, claim.players):
            assert_role(mentions[beaten].name, mentions[player].name, won=False)
    return problems


# =============================================================================
# Reading helpers shared by the net and tie checks
# =============================================================================


def _clauses(text: str) -> list[str]:
    return [_plain(part) for part in _CLAUSE_BREAK_RE.split(text) if part.strip()]


_WORD_RE = re.compile(r"\S+")


def _window_start(text: str, end: int, count: int) -> int:
    """Where the `count` words before `end` begin."""
    return ([m.start() for m in _WORD_RE.finditer(text, 0, end)][-count:] or [end])[0]


def _window_end(text: str, start: int, count: int) -> int:
    """Where the `count` words after `start` end."""
    return ([m.end() for m in _WORD_RE.finditer(text, start)][:count] or [start])[-1]


def _words_before(text: str, end: int, count: int) -> str:
    return " ".join(text[_window_start(text, end, count):end].split())


def _words_after(text: str, start: int, count: int) -> str:
    return " ".join(text[start:_window_end(text, start, count)].split())


# =============================================================================
# Net figures (#359)
# =============================================================================

# The minus signs a model writes: a hyphen, a true minus and an en dash.
_MINUS_SIGNS = "-−–"
_SIGNS = f"+{_MINUS_SIGNS}"
# A points figure tied to the word "net": "-6 net", "6 net points", "net -6",
# "net of -6", "a net loss of 6". A figure with no sign is compared without
# one -- "lost 6 net" and "-6 net" make the same claim -- and none is ever
# read off a place or a percentage ("a net gain of 3 places").
_NET_FIGURE_RE = re.compile(
    rf"(?<![\w.])(?P<before>[{re.escape(_SIGNS)}]?\d+)\s*(?:pts?\s+|points?\s+)?net(?!\w)"
    rf"|(?<!\w)net\s+(?:(?:loss|gain|swing|return)\s+)?(?:of\s+)?(?P<after>[{re.escape(_SIGNS)}]?\d+)"
    r"(?![\w.])(?!\s*(?:places?|spots?|positions?|%))",
    re.IGNORECASE,
)
# Words that make a net figure the manager's whole gameweek rather than one
# move's, wherever they sit in its clause: "-6 net overall", "-6 across both
# transfers", "his transfers went -6 net".
_NET_OVERALL_RE = re.compile(
    r"(?<!\w)(?:overall|in total|total|all told|altogether|combined|across|both|transfers|moves"
    r"|swaps|deals|waivers|claims|signings|pickups|business|dealings)(?!\w)",
    re.IGNORECASE,
)
# ...and phrasing that does the same only beside the figure: the week ("ended
# the week -6 net") and the hit ("-6 net after the hit", the roster line's own
# words). Further off, the week is when something else happened ("-6 net,
# the worst move of the week") and the hit is the one taken to make the move
# ("paid a hit to bring in Gibbs-White for Wirtz"), which says nothing about
# whose the figure is.
_NET_BESIDE_RE = re.compile(
    r"(?<!\w)(?:week|gameweek|gw|post-hit|after hits"
    r"|(?:after|once|with|including|net of|despite) (?:the|a|his|her|their) hit)(?!\w)",
    re.IGNORECASE,
)
_NET_BESIDE_WORDS = 4
# A signed figure standing on its own -- "went -4", "(+2)" -- read as a move's
# swing. Never a hit ("-4 hit", "a -4-point hit") and never the slang for one
# ("took a -4"), which says what the manager paid rather than what a move did.
_SIGNED_FIGURE_RE = re.compile(
    rf"(?<![\w.])[{re.escape(_SIGNS)}]\d+(?![\w.])(?!\s*-?\s*(?:(?:pts?|points?)\s+)?hit(?!\w))",
)
_HIT_SLANG_BEFORE_RE = re.compile(r"(?<!\w)(?:a|an|took|take|takes|taking|paid|pay|pays|paying)\s*$", re.IGNORECASE)


def _signed_value(text: str) -> int:
    return int(text.lstrip(_SIGNS)) * (-1 if text[0] in _MINUS_SIGNS else 1)


@dataclass(frozen=True)
class _Move:
    manager: str
    player_in: str
    player_out: str
    net: int

    @property
    def label(self) -> str:
        return f"{self.player_in} in for {self.player_out}"


@dataclass(frozen=True)
class _MoverLine:
    """One manager's roster line, as the Transfers or Waivers section prints it."""

    manager: str
    moves: tuple[_Move, ...]
    count: int
    cost: int
    noun: str

    @property
    def complete(self) -> bool:
        return len(self.moves) >= self.count

    @property
    def raw(self) -> int:
        return sum(move.net for move in self.moves)

    def moves_text(self, count: int) -> str:
        return f"{count} {self.noun}" + ("" if count == 1 else "s")

    def misplaced(self, value: int, move: _Move, *, signed: bool) -> str | None:
        """Which other figure on this line `value` is, said of `move` -- the
        overall net, the swing before the hit, or another move's swing -- or
        None when it is none of them and so came from somewhere else. A
        `signed` value is matched with its sign first, so "+2" is read as the
        move that went +2 before the -2 the line's moves came to."""
        figures: list[tuple[int, str]] = []
        if self.complete:
            hit = f", -{self.cost} hit" if self.cost else ""
            figures.append((self.raw - self.cost, (
                f"{self.raw - self.cost:+d} is {self.manager}'s net for the whole gameweek "
                f"({self.moves_text(self.count)}{hit})"
            )))
        if len(self.moves) > 1:
            scope = "all" if self.complete else "the captured"
            figures.append((self.raw, (
                f"{self.raw:+d} is {self.manager}'s swing across {scope} "
                f"{self.moves_text(len(self.moves))} before the hit"
            )))
        figures.extend(
            (other.net, f"{other.net:+d} is the swing on {other.label}") for other in self.moves if other != move
        )
        exact = [fact for figure, fact in figures if signed and figure == value]
        loose = [fact for figure, fact in figures if abs(figure) == abs(value)]
        return (exact or loose or [None])[0]


def _mover_lines(managers: Sequence[RecapManagerEntry]) -> list[_MoverLine]:
    lines: list[_MoverLine] = []
    for m in managers:
        name = m["manager_name"]
        if transfers := m.get("transfers"):
            moves = tuple(_Move(name, t["player_in"], t["player_out"], t["net"]) for t in transfers)
            made = m.get("transfers_made")
            count = len(moves) if made is None else max(made, len(moves))
            lines.append(_MoverLine(name, moves, count, m.get("transfer_cost", 0), "transfer"))
        elif transactions := m.get("transactions"):
            moves = tuple(_Move(name, t["player_in"], t["player_out"], t["net"]) for t in transactions)
            lines.append(_MoverLine(name, moves, len(moves), 0, "move"))
    return lines


def check_net_attributions(summary: str, managers: Sequence[RecapManagerEntry], names: RecapNames) -> list[str]:
    """Every net figure the editorial pins on one move that belongs to the
    manager's whole gameweek or to another move (#359).

    A roster line carries the manager's net beside each move's own swing, and
    the editorial took the first for the second: "brought in Gibbs-White for
    Wirtz and watched it backfire to the tune of -6 net", where that move was
    -4 and the -6 only exists with the second transfer and the hit. Both the
    moves and the figures are known, so the substitution is checkable.

    A clause is read only when it names exactly one move -- both its players
    -- of exactly one manager, names no other manager, and carries a figure
    tied to the word "net". The figure is then the move's when the move is
    the nearest thing named before it, and the manager's when the manager is
    ("Gibbs-White for Wirtz went -4, and Alice was -6 net"). It is a problem
    only when it is the move's, is not that move's swing, and is another
    figure on the same line, so a number from anywhere else is never guessed
    at. A clause that marks the figure as the manager's ("overall", "across
    both", "after the hit") or states the move's own swing beside it passes.
    Returns one line per wrong figure, in the order the editorial states them.
    """
    lines = _mover_lines(managers)
    # A line with one move and no hit has no figure but that move's swing.
    if not any(len(line.moves) > 1 or line.cost for line in lines):
        return []
    by_manager = {line.manager: line for line in lines}
    players = {name for line in lines for move in line.moves for name in (move.player_in, move.player_out)}
    aliases = {
        alias: name for alias, (name, is_manager) in _mention_aliases(names.managers, players).items() if is_manager
    }

    problems: list[str] = []
    for clause in _clauses(summary):
        figures = list(_NET_FIGURE_RE.finditer(clause))
        if not figures or _NET_OVERALL_RE.search(clause):
            continue
        mentions = names.find(clause)
        player_mentions = [m for m in mentions if m.group(0) in players]
        named_players = {m.group(0) for m in player_mentions}
        named = [
            move for line in lines for move in line.moves
            if {move.player_in, move.player_out} <= named_players
        ]
        if not named:
            continue
        manager_mentions = [(m.start(), aliases[m.group(0)]) for m in mentions if m.group(0) in aliases]
        named_managers = {name for _, name in manager_mentions}
        owners = {move.manager for move in named}
        if named_managers - owners:
            # Another manager's net may be the figure meant.
            continue
        if len(owners) > 1:
            # Two managers made a move both its players name: read it only
            # where the clause says whose it was.
            owners &= named_managers
            named = [move for move in named if move.manager in owners]
        if len(owners) != 1 or len({(m.player_in, m.player_out) for m in named}) != 1:
            # Two of a manager's moves together may carry their combined net.
            continue
        move = named[0]
        # A clause that gives the move its own swing has kept the two figures
        # apart, whatever else it says.
        if any(
            _signed_value(m.group(0)) == move.net and not _HIT_SLANG_BEFORE_RE.search(clause[:m.start()])
            for m in _SIGNED_FIGURE_RE.finditer(clause)
        ):
            continue
        move_starts = [m.start() for m in player_mentions if m.group(0) in (move.player_in, move.player_out)]
        owner_starts = [start for start, name in manager_mentions if name == move.manager]
        for figure in figures:
            text = figure.group("before") or figure.group("after")
            value = _signed_value(text)
            # An explicit sign is part of the claim: "-2" is not a +2 move's
            # swing, whatever "lost 2" would have been.
            signed = text[0] in _SIGNS
            if value == move.net if signed else abs(value) == abs(move.net):
                continue
            beside = (
                _words_before(clause, figure.start(), _NET_BESIDE_WORDS) + " "
                + _words_after(clause, figure.end(), _NET_BESIDE_WORDS)
            )
            if _NET_BESIDE_RE.search(beside):
                continue
            # Whose figure it is: the nearer of the move and its manager
            # named before it. Named before neither, it leads into the move.
            last_move = max((s for s in move_starts if s < figure.start()), default=-1)
            last_owner = max((s for s in owner_starts if s < figure.start()), default=-1)
            if last_owner > last_move:
                continue
            fact = by_manager[move.manager].misplaced(value, move, signed=signed)
            if fact is None:
                continue
            problem = (
                f'the editorial says "{figure.group(0).strip()}" of {move.label}, but {fact}, '
                f"not that move's own swing of {move.net:+d}"
            )
            if problem not in problems:
                problems.append(problem)
    return problems


# =============================================================================
# Ties (#359)
# =============================================================================

_ORDINAL_NUMBERS = {word: n for n, word in enumerate(ORDINAL_WORDS, start=1)}
_TOP_RANK_WORDS = frozenset({"highest", "best", "top", "first"})
_BOTTOM_RANK_WORDS = frozenset({"lowest", "worst", "bottom", "last"})
# A tie word fused to a rank: "joint-lowest", "equal top", "tied for last",
# "level at the top", "shared the lowest", "joint 7th", "joint-seventh".
# "Level" counts only with a preposition, so "did his level best" is no tie.
_TIE_RE = re.compile(
    r"(?<!\w)(?:(?:joint|jointly|equal|tied|shared|sharing|shares?)(?:-|\s+)(?:(?:for|on|at|in)\s+)?"
    r"|level\s+(?:for|on|at|in)\s+)(?:the\s+)?"
    r"(?P<rank>highest|lowest|best|worst|top|bottom|last|\d+(?:st|nd|rd|th)|"
    + "|".join(_ORDINAL_NUMBERS) + r")(?!\w)",
    re.IGNORECASE,
)
# What may follow a rank for it to be one: the clause's end, the noun it
# ranks, a figure, or a word that places it ("joint-top of the table",
# "joint-seventh in the table", "tied for last on 29"). Anything else is
# another use of the word: "shared first blood", "shared the best fixture".
_RANK_FOLLOWS_RE = re.compile(
    r"^[\s-]*(?:$|[,.;:!?)]|\d|(?:places?|spots?|positions?|scores?|scoring|totals?|tally|finish"
    r"|gameweek|gw|weekly|of|in|on|at|with|among|this|for|after|again|overall|and|but|alongside)(?!\w))",
    re.IGNORECASE,
)
# "Last" and "first" also count time: "tied last week" is no rank.
_RANK_IS_TIME_RE = re.compile(
    r"^[\s-]*(?:week\w*|gameweek|gw|season|night|time|year|month|round|game|match|\d)(?!\w)", re.IGNORECASE,
)
# "joint-second lowest": a numbered rank counted from either end of the
# gameweek scores, which the prompt never states.
_RANK_DIRECTION_RE = re.compile(r"^[\s-]*(?:highest|lowest|best|worst)(?!\w)", re.IGNORECASE)
# What the tie is about. Something other than a manager's gameweek score or
# league position -- a captain, a bench, a player -- is not a tie this data
# can settle, so it is left alone; that subject is the noun the rank modifies
# ("the joint-lowest captain") or the one it is said of ("Salah was joint-top"),
# never a name further off ("the joint-lowest score of the week as Haaland
# blanked"). Whether the tie is on scores or in the table is read wider.
_TIE_OTHER_SUBJECT_RE = re.compile(
    r"(?<!\w)(?:(?:captain|armband|bench|transfer|swap|waiver|chip|signing|player|streak|haul"
    r"|scorer|goal|assist|bonus|differential)\w*|(?:moves?|picks?|claims?|fines?)(?!\w))",
    re.IGNORECASE,
)
_TIE_SUBJECT_WORDS_BEFORE = 3
_TIE_SUBJECT_WORDS_AFTER = 2
_TIE_GAMEWEEK_RE = re.compile(r"(?<!\w)(?:scor\w*|tall(?:y|ies)|week\w*|gameweek\w*|gw\d*|round)(?!\w)", re.IGNORECASE)
_TIE_TABLE_RE = re.compile(
    r"(?<!\w)(?:table|standings|league|positions?|places?|spots?|overall|season\w*|totals?|summit|rank\w*)(?!\w)",
    re.IGNORECASE,
)
_TIE_WINDOW_WORDS = 6
_TIE_NEGATION_WORDS = 5


def _competition_ranks(values: Sequence[tuple[str, int]]) -> list[tuple[str, int]]:
    """Each name's rank when higher is better, level values sharing one."""
    return [(name, 1 + sum(other > value for _, other in values)) for name, value in values]


def _rank_holders(ranks: Sequence[tuple[str, int]], rank: int | str) -> tuple[int, list[str]]:
    """The rank `rank` names ("top", "bottom" or a number) and who holds it."""
    values = [value for _, value in ranks]
    target = min(values) if rank == "top" else max(values) if rank == "bottom" else int(rank)
    return target, [name for name, value in ranks if value == target]


def check_tie_claims(summary: str, managers: Sequence[RecapManagerEntry], names: RecapNames) -> list[str]:
    """Every tie the editorial claims at a rank no two managers share (#359).

    The editorial called an outright lowest score "the joint-lowest score of
    the week" -- "joint" was live in the table, whose season totals do share
    positions, and was carried onto a gameweek score nobody shared. Who holds
    each gameweek score and each league position is known, so a tie word
    fused to a rank ("joint-lowest", "tied for last", "joint-seventh") is
    checkable against both.

    What the tie is about comes from the words around it: a gameweek score
    ("score", "week") is ranked on `gw_points`, the column the prompt's table
    prints; a league position ("table", "place") on the table's Pos, Prev and
    Total columns, any of which holding the tie bears it out -- the GW
    Standings section states all three, and a draft head-to-head table can
    rank two managers apart who are level on Total. A phrase that names both,
    or neither, passes if either reading does; one about something else (a
    captain, a bench, a player) is left alone, as is a negated one, a word
    that is not a rank where it stands ("tied last week"), and a numbered
    rank among gameweek scores ("joint-third highest"), which the prompt
    never states. Returns one line per unsupported tie, in the order the
    editorial claims them.
    """
    if len(managers) < 2:
        return []
    gameweek = _competition_ranks([(m["manager_name"], m["gw_points"]) for m in managers])
    points = {m["manager_name"]: m["gw_points"] for m in managers}
    # The table's Pos column first -- the one a "nobody is 8th" is read off --
    # then its Prev column and the ranks its Total column implies.
    tables = [
        ranks for ranks in (
            [(m["manager_name"], m.get("overall_rank") or 0) for m in managers if m.get("overall_rank")],
            [(m["manager_name"], m.get("previous_rank") or 0) for m in managers if m.get("previous_rank")],
            _competition_ranks([
                (m["manager_name"], m.get("total_points") or 0) for m in managers if "total_points" in m
            ]),
        ) if ranks
    ]
    # Each says why the tie does not hold in its reading, or None when it does
    # (or the data cannot say).
    def gameweek_fact(rank: int | str) -> str | None:
        _, holders = _rank_holders(gameweek, rank)
        if len(holders) != 1:
            return None
        which = "highest" if rank == "top" else "lowest"
        return f"the {which} gameweek score, {points_label(points[holders[0]])}, was {holders[0]}'s alone"

    def table_fact(rank: int | str) -> str | None:
        if not tables or any(len(_rank_holders(ranks, rank)[1]) > 1 for ranks in tables):
            return None
        target, holders = _rank_holders(tables[0], rank)
        if not holders:
            return f"nobody is {place_label(target)} in the table"
        return f"{place_label(target)} place in the table is {holders[0]}'s alone"

    problems: list[str] = []
    for clause in _clauses(summary):
        ties = list(_TIE_RE.finditer(clause))
        if not ties:
            continue
        # Read across the whole clause, so a manager's name the subject window
        # cuts in half is still the manager's ("Bob White" is not the player
        # "White", #379).
        player_spans = [m.span() for m in names.find(clause) if m.group(0) in names.players]
        for match in ties:
            word = match.group("rank").lower()
            rest = clause[match.end():]
            if not _RANK_FOLLOWS_RE.match(rest) or (word in ("last", "first") and _RANK_IS_TIME_RE.match(rest)):
                continue
            before = _words_before(clause, match.start(), _TIE_NEGATION_WORDS)
            if _NEGATION_RE.search(before) or "no longer" in before.lower():
                continue
            lo = _window_start(clause, match.start(), _TIE_SUBJECT_WORDS_BEFORE)
            hi = _window_end(clause, match.end(), _TIE_SUBJECT_WORDS_AFTER)
            subject = clause[lo:match.start()] + " " + clause[match.end():hi]
            if _TIE_OTHER_SUBJECT_RE.search(subject) or any(lo <= s and e <= hi for s, e in player_spans):
                continue
            window = (
                _words_before(clause, match.start(), _TIE_WINDOW_WORDS) + " "
                + _words_after(clause, match.end(), _TIE_WINDOW_WORDS)
            )
            about_week = bool(_TIE_GAMEWEEK_RE.search(window))
            about_table = bool(_TIE_TABLE_RE.search(window))
            if word in _TOP_RANK_WORDS:
                rank: int | str = "top"
            elif word in _BOTTOM_RANK_WORDS:
                rank = "bottom"
            else:
                rank = _ORDINAL_NUMBERS.get(word) or int(word[:-2])
                if rank == 1:
                    rank = "top"
                elif about_week or _RANK_DIRECTION_RE.match(rest):
                    continue
                else:
                    about_table = True
            if about_week and not about_table:
                fact = gameweek_fact(rank)
            elif about_table and not about_week:
                fact = table_fact(rank)
            else:
                week, table = gameweek_fact(rank), table_fact(rank)
                fact = f"neither reading holds: {week}, and {table}" if week and table else None
            if fact is None:
                continue
            problem = f'the editorial says "{match.group(0)}", but {fact}'
            if problem not in problems:
                problems.append(problem)
    return problems


# =============================================================================
# The registry, the retry and the warning
# =============================================================================


@dataclass(frozen=True)
class EditorialCheck:
    """One kind of claim the editorial is held to, and everything that names it."""

    # The JSON warning code a claim that survives the retry raises.
    code: str
    # What the editorial got wrong, as the warning's message opens.
    lead: str
    # What the retry is told to do about it.
    retry_instruction: str
    run: Callable[[str, LeagueRecapData, RecapNames], list[str]]


def _contested(summary: str, data: LeagueRecapData, names: RecapNames) -> list[str]:
    return check_contested_attributions(summary, contested_draft_claims(data["managers"]), names)


EDITORIAL_CHECKS: tuple[EditorialCheck, ...] = (
    EditorialCheck(
        code=RECAP_WARNING_CONTESTED_MISATTRIBUTION,
        lead="The editorial contradicts the contested waiver claims it was given",
        retry_instruction=(
            "Give every contested race its own clause, naming its winner and beaten managers "
            'exactly as its line does, and never fold two races into one clause with "respectively" '
            "or a shared list."
        ),
        run=_contested,
    ),
    EditorialCheck(
        code=RECAP_WARNING_NET_MISATTRIBUTION,
        lead="The editorial pins a figure from a manager's line on the wrong move",
        retry_instruction=(
            "Attach each move's swing only to that move and a manager's net only to the manager, "
            "called their net overall, never to one named move; the hit is the gameweek's, never "
            "one transfer's."
        ),
        run=lambda summary, data, names: check_net_attributions(summary, data["managers"], names),
    ),
    EditorialCheck(
        code=RECAP_WARNING_UNSUPPORTED_TIE,
        lead="The editorial claims a tie the scores and standings it was given do not show",
        retry_instruction=(
            'Call a score or position "joint", "tied", "level" or "shared" only where the GW '
            "Standings section lists it as shared."
        ),
        run=lambda summary, data, names: check_tie_claims(summary, data["managers"], names),
    ),
)
_CHECKS_BY_CODE = {check.code: check for check in EDITORIAL_CHECKS}


def check_recap_editorial(summary: str, data: LeagueRecapData) -> dict[str, list[str]]:
    """Every claim the editorial makes that its own data contradicts, keyed
    by the warning code of the check that caught it.

    Only a code with at least one problem is a key, so an empty answer is a
    clean editorial.
    """
    names = RecapNames.of(data["managers"])
    found = {check.code: check.run(summary, data, names) for check in EDITORIAL_CHECKS}
    return {code: problems for code, problems in found.items() if problems}


def get_recap_editorial_retry_prompt(user_prompt: str, problems: Mapping[str, Sequence[str]]) -> str:
    """The user prompt again, followed by what the last draft got wrong.

    A second roll of the identical prompt is the same gamble that already
    failed once; naming each wrong claim beside the data it contradicts, and
    saying how to avoid that kind of mistake, gives the retry the one thing
    the first attempt lacked. `problems` is `check_recap_editorial()`'s
    answer, so only the kinds of mistake actually made get an instruction.
    """
    corrections = "\n".join(f"- {problem}" for found in problems.values() for problem in found)
    instructions = " ".join(_CHECKS_BY_CODE[code].retry_instruction for code in problems)
    return (
        f"{user_prompt}\n\n"
        "Your previous draft contradicted the data it was given:\n"
        f"{corrections}\n"
        f"Write the recap again from scratch. {instructions}"
    )


def editorial_warning_message(code: str, problems: Sequence[str]) -> str:
    """The JSON warning's message: the check's lead, then one sentence per
    claim. A claim can carry its own semicolons (a quoted contested line
    does), so sentences, not a joiner, are what keep the claims apart."""
    claims = " ".join(f"{problem[:1].upper()}{problem[1:].rstrip('.')}." for problem in problems)
    return f"{_CHECKS_BY_CODE[code].lead}. {claims}"
