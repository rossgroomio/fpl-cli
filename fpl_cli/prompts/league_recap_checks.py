"""Checks the league-recap editorial against the data its prompt carried (#357, #359, #390).

The prompt states every rule these read, and the model has broken each of
them anyway: a manager named as beaten in a waiver race they won, a
manager's whole-gameweek net pinned on one transfer, a tie nobody shared.
Where the claim is about known strings -- managers, players, figures, ranks
-- it can be read back mechanically instead of trusting a rule the model
has already ignored once.

Each check is narrow by design. It reads only the clauses it can pin down
and lets everything else pass, so a problem it returns is a sentence the
data contradicts, never a guess -- a false alarm costs a retry and puts a
warning above a correct editorial. The one check that breaks that wording is
the roll call of beaten managers (#459), which fires on a true sentence:
its contract is the mechanical analogue, a count of known names in one
paragraph and never a judgement of tone. It is registered as a broken brief
rather than a contradiction, so the retry, stderr and the saved report say
the draft broke its brief instead of claiming the data contradicts it.
`EDITORIAL_CHECKS` is the one registry: each check's warning code, the prose
its JSON warning opens with, what the retry is told, and the checker itself.
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
    draft_transaction_kind_label,
    draft_transaction_kind_nets,
    format_contested_claim,
    format_move_counts,
    place_label,
    points_label,
)
from fpl_cli.utils.text import ORDINAL_WORDS

# The JSON warning code each check raises when a claim it caught survives
# the retry. Stable for scripts, like every code on that channel.
RECAP_WARNING_CONTESTED_MISATTRIBUTION = "synthesis_contested_misattribution"
RECAP_WARNING_NET_MISATTRIBUTION = "synthesis_net_misattribution"
RECAP_WARNING_KIND_NET_MISATTRIBUTION = "synthesis_kind_net_misattribution"
RECAP_WARNING_UNSUPPORTED_TIE = "synthesis_unsupported_tie"
RECAP_WARNING_CONTESTED_ROLL_CALL = "synthesis_contested_roll_call"


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
# Roll calls of the beaten (#459)
# =============================================================================

# How many of one race's beaten managers a paragraph may name before it reads
# as a list of them. The prompt allows a beaten manager whose loss is the
# story (the leader missed out, one manager lost two races), and two such
# stories in one race are plausible; three is a roll call.
ROLL_CALL_MIN_NAMED = 3
# An ATX heading is a block of its own in markdown, blank line before it or
# not, so it never runs into the paragraph above or below.
_HEADING_LINE_RE = re.compile(r"\s{0,3}#{1,6}(?:\s|$)")


def _paragraphs(text: str) -> list[str]:
    """The blocks `text` renders as: runs of lines between blank lines, with
    each heading line a block of its own. A single line break stays inside its
    paragraph, as it does in the report the editorial is saved to."""
    blocks: list[str] = []
    lines: list[str] = []
    for line in text.splitlines():
        if line.strip() and not _HEADING_LINE_RE.match(line):
            lines.append(line)
            continue
        if lines:
            blocks.append("\n".join(lines))
            lines = []
        if line.strip():
            blocks.append(line)
    if lines:
        blocks.append("\n".join(lines))
    return blocks


def check_contested_roll_calls(
    summary: str,
    contests: Sequence[RecapContestedClaim],
    names: RecapNames,
) -> list[str]:
    """Every paragraph that lists the managers beaten to one player (#459).

    The prompt says a race needs only its count and its winner and a beaten
    manager is named only when that is the story, and five rounds of prompt
    and data changes on #452 and #455 never stopped the model reciting the
    week's biggest race as a list. Unlike every other check this fires on a
    true sentence, so it holds itself to what a machine can count: the
    distinct beaten managers of one race that a paragraph naming the race's
    player also names. It never judges tone, and it does not ask why a
    manager is named.

    Managers are matched through `RecapNames` as the other checks match them,
    so a full name, or a first or last name no other manager shares, counts
    and a name read inside a longer one does not. A short form that is also
    the name of any player the data carries is left unread, as in
    `check_kind_net_scopes`: "Wood" beside a squad player of that name could
    be either, and a false alarm costs more here than a missed one.

    Three or more of one race's beaten managers in one paragraph that names
    the race's player is one problem. A paragraph that never names him is not
    about the race, however many of its managers lost it: a standings
    paragraph naming three of a five-way race's losers is no roll call. Two
    beaten managers is not one, nor is the same set spread one per
    paragraph, nor is the winner, who is never among the beaten. A beaten
    manager named in the race's paragraph for an unrelated reason counts all
    the same: the paragraph reads as a roll call to a reader too, so that is
    an accepted cost rather than a false alarm to engineer around.
    Paragraphs are `_paragraphs()`'s. Classic rows carry no lost claims,
    hence no races, and the check returns nothing there.

    Returns one line per race and count, in the order the editorial first
    reaches them. A race listed at the same count by more than one paragraph
    is one line saying so, since a line per paragraph would be the same
    sentence twice with nothing to tell the paragraphs apart.
    """
    if not contests:
        return []
    aliases = _mention_aliases(names.managers, set(names.players))
    rolls: dict[tuple[int, int], int] = {}
    for paragraph in _paragraphs(summary):
        players = {m.group(0) for m in names.find(paragraph)}
        named = {m.name for m in _find_mentions(paragraph, names, aliases) if m.is_manager}
        for index, race in enumerate(contests):
            if race["player"] not in players:
                continue
            # A winner who shares a display name with a loser is ambiguous in
            # prose, so that name is left out rather than guessed at.
            beaten = {loser["manager_name"] for loser in race["losers"]} - {race["winner"]}
            if (count := len(beaten & named)) >= ROLL_CALL_MIN_NAMED:
                rolls[index, count] = rolls.get((index, count), 0) + 1
    problems: list[str] = []
    for (index, count), paragraphs in rolls.items():
        race = contests[index]
        beaten = f"{count} of the {len(race['losers'])} managers beaten to {race['player']}"
        problems.append(
            f"one paragraph names {beaten}" if paragraphs == 1 else f"{paragraphs} paragraphs each name {beaten}"
        )
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
# What follows a figure that counts something other than points: places
# climbed, a percentage. One fragment, so every reader of a figure drops the
# same things.
_NOT_POINTS = r"\s*(?:places?|spots?|positions?|%)"
_NOT_POINTS_RE = re.compile(_NOT_POINTS, re.IGNORECASE)
# A points figure tied to the word "net": "-6 net", "6 net points", "net -6",
# "net of -6", "a net loss of 6", and the verb with a signed figure: "netted
# +3", "nets him -1". A figure with no sign is compared without one -- "lost
# 6 net" and "-6 net" make the same claim -- and none is ever read off a
# place or a percentage ("a net gain of 3 places"). The verb never takes an
# unsigned one: in a recap "Isak netted 2" is goals and "Gibbs-White netted
# 6 points" is the player's score, not a move's (#390).
_NET_FIGURE_RE = re.compile(
    rf"(?<![\w.])(?P<before>[{re.escape(_SIGNS)}]?\d+)\s*(?:pts?\s+|points?\s+)?net(?!\w)"
    rf"|(?<!\w)net\s+(?:(?:loss|gain|swing|return)\s+)?(?:of\s+)?(?P<after>[{re.escape(_SIGNS)}]?\d+)"
    rf"(?![\w.])(?!{_NOT_POINTS})"
    r"|(?<!\w)net(?:s|ted|ting)\s+(?:(?:him|her|them)\s+)?(?:(?:a|an)\s+)?"
    rf"(?P<verb>[{re.escape(_SIGNS)}]\d+)(?![\w.])(?!{_NOT_POINTS})",
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


def _net_figure_text(match: re.Match[str]) -> str:
    """The figure a `_NET_FIGURE_RE` match read, sign and all."""
    return next(text for text in match.group("before", "after", "verb") if text)


def _pick(figures: Sequence[tuple[int, str]], value: int, *, signed: bool) -> str | None:
    """The fact for the first figure `value` is: with its sign first when it
    carries one, so "+2" is read as the move that went +2 before the -2 two
    moves came to, then whichever way round. None when it is none of them."""
    exact = [fact for figure, fact in figures if signed and figure == value]
    loose = [fact for figure, fact in figures if abs(figure) == abs(value)]
    return (exact or loose or [None])[0]


@dataclass(frozen=True)
class _Move:
    manager: str
    player_in: str
    player_out: str
    net: int
    # A draft move's kind label ("waiver", "free agent"); "" for a transfer.
    kind: str = ""

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
    # A draft line's net per kind, from the helper the roster prints them
    # with, so the check and the prompt share one figure (#390); empty for
    # transfers.
    kind_nets: tuple[tuple[str, int], ...] = ()

    @property
    def complete(self) -> bool:
        return len(self.moves) >= self.count

    @property
    def raw(self) -> int:
        return sum(move.net for move in self.moves)

    def moves_text(self, count: int) -> str:
        return f"{count} {self.noun}" + ("" if count == 1 else "s")

    @property
    def kinds(self) -> dict[str, tuple[_Move, ...]]:
        """The line's moves by kind, in the order the roster prints them, for
        the kinds present; a transfer line has none."""
        return {label: tuple(m for m in self.moves if m.kind == label) for label, _ in self.kind_nets}

    def _whole_gameweek(self) -> tuple[int, str]:
        hit = f", -{self.cost} hit" if self.cost else ""
        kinds = self.kinds
        made = (
            format_move_counts([(label, len(moves)) for label, moves in kinds.items()])
            if len(kinds) > 1 else self.moves_text(self.count)
        )
        net = self.raw - self.cost
        return net, f"{net:+d} is {self.manager}'s net for the whole gameweek ({made}{hit})"

    def _kind_nets(self, *, besides: str = "") -> list[tuple[int, str]]:
        """The net of each kind of more than one move on a line that mixes
        kinds, other than `besides` -- the figures the roster prints beside
        the net overall (#390). A kind of one move nets that move's swing."""
        if len(self.kind_nets) < 2:
            return []
        kinds = self.kinds
        return [
            (net, f"{net:+d} is the net across {self.manager}'s {len(kinds[label])} {label}s")
            for label, net in self.kind_nets
            if label != besides and len(kinds[label]) > 1
        ]

    def misplaced(self, value: int, move: _Move, *, signed: bool) -> str | None:
        """Which other figure on this line `value` is, said of `move` -- the
        overall net, the swing before the hit, a kind's net, or another move's
        swing -- or None when it is none of them and so came from somewhere
        else. A `signed` value is matched with its sign first."""
        figures: list[tuple[int, str]] = []
        if self.complete:
            figures.append(self._whole_gameweek())
        if len(self.moves) > 1:
            scope = "all" if self.complete else "the captured"
            figures.append((self.raw, (
                f"{self.raw:+d} is {self.manager}'s swing across {scope} "
                f"{self.moves_text(len(self.moves))} before the hit"
            )))
        figures.extend(self._kind_nets())
        figures.extend(
            (other.net, f"{other.net:+d} is the swing on {other.label}") for other in self.moves if other != move
        )
        return _pick(figures, value, signed=signed)

    def misplaced_kind(self, value: int, kind: str, *, signed: bool) -> str | None:
        """Which figure on this line `value` is, said of the manager's moves
        of `kind` alone -- the net overall, another kind's net, or the swing
        on a move of another kind -- or None when it is none of them."""
        figures = [self._whole_gameweek(), *self._kind_nets(besides=kind)]
        figures.extend(
            (other.net, f"{other.net:+d} is the swing on {other.label} [{label}]")
            for label, moves in self.kinds.items() if label != kind for other in moves
        )
        return _pick(figures, value, signed=signed)


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
            moves = tuple(
                _Move(name, t["player_in"], t["player_out"], t["net"], draft_transaction_kind_label(t["kind"]))
                for t in transactions
            )
            lines.append(_MoverLine(
                name, moves, len(moves), 0, "move", tuple(draft_transaction_kind_nets(transactions)),
            ))
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
            text = _net_figure_text(figure)
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
# Kind-scoped nets (#390)
# =============================================================================

# The nouns a kind word may lead into, none of which names a kind itself:
# "moves", "claims" and "pickups" cover both (the roster's own lost-claims
# line says a free agent was claimed).
_SCOPE_NOUNS = (
    r"(?:moves?|transfers?|claims?|pick-?ups?|signings?|business|deals?|dealings|activity|swaps?"
    r"|additions?|acquisitions?|transactions?|work)"
)
# A draft move's kind named as what a figure is about: "his waiver moves",
# "her waivers", "the free-agent pickups", "his free agency". Only the kind
# word scopes the figure, and "the waiver wire" is the pool of unowned
# players, free agents included.
_KIND_SCOPE_RE = re.compile(
    r"(?<!\w)(?:(?P<waiver>waivers?(?![-\s]+wire))|(?P<free_agent>free[-\s]+agen(?:ts?|cy)))"
    rf"(?:[-\s]+{_SCOPE_NOUNS}|[-\s]+market)?(?!\w)",
    re.IGNORECASE,
)
_KIND_GROUP_LABELS = {"waiver": "waiver", "free_agent": "free agent"}
# The same nouns with no kind before them are every move ("his moves netted
# +3 overall"): a figure said of them keeps that subject whatever kind word
# trails after it, and a kind joined to one ("a waiver and a pickup") is no
# longer one kind.
_NEUTRAL_SCOPE_RE = re.compile(rf"(?<!\w){_SCOPE_NOUNS}(?!\w)", re.IGNORECASE)
_ANY_KIND = "*"
# Two kinds named together ("waiver and free-agent moves", "waivers, free
# agents", "one waiver and one free agent") are every move the manager made,
# which is no kind at all. Unlike a list of managers (`_LIST_JOIN_RE`), "or",
# "plus" and a determiner after the conjunction still name both; after a bare
# comma a determiner opens a new phrase ("two moves, his waivers netting +3").
_KIND_JOIN_RE = re.compile(
    r"^\s*(?:[,/]|(?:,\s*)?(?:and|or|&|plus)(?:\s+(?:a|an|the|his|her|their|its|one|two|three|\d+))?)\s*$",
    re.IGNORECASE,
)
# What may sit between a kind and the figure it scopes: the verb that carries
# one to the other and its furniture, never a word that names or counts
# anything else. "His waiver moves netted +3 overall" scopes +3 to his
# waivers; "his one waiver lost a point and his moves netted +3" does not,
# and neither does "Manzambi arrived as a free agent (+4) and left his moves
# +3", where another figure stands nearer. Before the figure, one comma may
# open a relative or participle clause ("his waiver moves, which netted +3",
# "his waiver moves, netting +3"). After it, only a word that says where the
# figure came from ("+3 from his waivers") joins; "with" and "for" open a
# clause about something else ("+3 overall with a free agent doing the heavy
# lifting").
_KIND_LINK_BEFORE = frozenset({
    "net", "nets", "netted", "netting", "went", "go", "goes", "going", "came", "come", "comes", "coming",
    "to", "out", "at", "in", "up", "off", "were", "was", "are", "is", "be", "been", "finished", "finishing",
    "ended", "ending", "totalled", "totaled", "totalling", "totaling", "added", "adding", "returned",
    "returning", "yielded", "yielding", "brought", "bringing", "delivered", "delivering", "produced",
    "producing", "earned", "earning", "gave", "giving", "left", "leaving", "made", "paid", "paying",
    "swung", "he", "she", "they", "him", "her", "them", "a", "an", "the", "of", "for", "with", "worth",
    "only", "just", "overall", "combined", "together", "altogether", "all", "told", "still", "between",
    "tune", "mere", "meagre", "meager", "paltry", "modest", "measly", "tidy", "healthy", "handy",
    "respectable", "solid",
})
_KIND_LINK_AFTER = frozenset({
    "net", "overall", "pts", "pt", "points", "point", "from", "on", "across", "over", "through", "via",
    "in", "his", "her", "their", "its", "the", "a", "all", "of", "both", "between",
})
_KIND_LINK_MAX_WORDS = 6
_KIND_LINK_BREAK_RE = re.compile(r",|\d|[–—]|\s-\s")
_KIND_LINK_COMMA_RE = re.compile(r"^\s*,\s*(?P<relative>(?:which|that)(?!\w))?", re.IGNORECASE)
_LINK_WORD_RE = re.compile(r"[a-z]+(?:['’][a-z]+)?")


@dataclass(frozen=True)
class _KindScope:
    """A run of scope words joined into one, and the kinds it names --
    `_ANY_KIND` for a neutral noun, which names every kind."""

    start: int
    end: int
    kinds: frozenset[str]

    @property
    def kind(self) -> str | None:
        """The one kind the scope names, or None when it names more."""
        return next(iter(self.kinds)) if len(self.kinds) == 1 and _ANY_KIND not in self.kinds else None


def _kind_scopes(clause: str) -> list[_KindScope]:
    """Every scope in `clause`, kinds and neutral nouns, joined runs merged."""
    words = [
        (match.start(), match.end(), _KIND_GROUP_LABELS[next(g for g in _KIND_GROUP_LABELS if match.group(g))])
        for match in _KIND_SCOPE_RE.finditer(clause)
    ]
    words.extend(
        (match.start(), match.end(), _ANY_KIND) for match in _NEUTRAL_SCOPE_RE.finditer(clause)
        if not any(start <= match.start() < end for start, end, _ in words)
    )
    scopes: list[_KindScope] = []
    for start, end, kind in sorted(words):
        if scopes and _KIND_JOIN_RE.match(_plain(clause[scopes[-1].end:start])):
            last = scopes.pop()
            scopes.append(_KindScope(last.start, end, last.kinds | {kind}))
        else:
            scopes.append(_KindScope(start, end, frozenset({kind})))
    return scopes


def _kind_link(gap: str, words: frozenset[str], *, comma: bool = False) -> bool:
    """Whether `gap` joins a scope to a figure: a few words, all from
    `words`, after one leading comma that opens a relative or participle
    clause where `comma` allows it."""
    plain = _plain(gap)
    if comma and (lead := _KIND_LINK_COMMA_RE.match(plain)):
        rest = plain[lead.end():]
        first = _LINK_WORD_RE.findall(rest.lower())[:1]
        if lead.group("relative") or (first and first[0].endswith("ing")):
            plain = rest
    if _KIND_LINK_BREAK_RE.search(plain):
        return False
    found = _LINK_WORD_RE.findall(plain.lower())
    return len(found) <= _KIND_LINK_MAX_WORDS and all(word in words for word in found)


def _figure_scope(clause: str, start: int, end: int, scopes: Sequence[_KindScope]) -> tuple[_KindScope, str] | None:
    """The scope the figure at `start`-`end` is said of, with the text from
    one to the other, or None when no scope is linked to it.

    Its subject is the scope nearest before it, when the two are linked, and
    a neutral subject keeps the figure whatever kind follows. Only a figure
    with no subject before it is read against a scope after it ("netted +3
    overall from his waivers")."""
    before = [scope for scope in scopes if scope.end <= start]
    # A figure's leading verb ("netting +3") is part of the link it makes.
    verb = re.match(r"[a-z]+", clause[start:end], re.IGNORECASE)
    if before and _kind_link(
        clause[before[-1].end:start] + (verb.group(0) if verb else ""), _KIND_LINK_BEFORE, comma=True,
    ):
        return before[-1], clause[before[-1].start:end]
    after = [scope for scope in scopes if scope.start >= end]
    if after and _kind_link(clause[end:after[0].start], _KIND_LINK_AFTER):
        return after[0], clause[start:after[0].end]
    return None


def _clause_figures(clause: str) -> list[tuple[int, int, str]]:
    """Every points figure in `clause` -- a net figure, or a signed one on
    its own -- as (start, end, figure text), in order."""
    figures = [(m.start(), m.end(), _net_figure_text(m)) for m in _NET_FIGURE_RE.finditer(clause)]
    for m in _SIGNED_FIGURE_RE.finditer(clause):
        if any(start <= m.start() < end for start, end, _ in figures) or _NOT_POINTS_RE.match(clause, m.end()):
            continue
        figures.append((m.start(), m.end(), m.group(0)))
    return sorted(figures)


def check_kind_net_scopes(summary: str, managers: Sequence[RecapManagerEntry], names: RecapNames) -> list[str]:
    """Every figure the editorial says of one kind of a draft manager's moves
    that belongs to all of them, or to the other kind (#390).

    A manager made one waiver (-1) and one free-agent pickup (+4), and the
    editorial wrote "his waiver moves netted +3 overall": the +3 is the two
    together, so the sentence hid a losing claim behind the pickup's gain.
    For a manager whose moves are all one kind the kind is every move and
    the net overall is that kind's, so only a line that mixes kinds can be
    wrong this way, and its per-kind nets and swings are known.

    A figure is read when a kind word scopes it -- "waivers", "waiver
    moves", "free agents", "free-agent pickups", joined to the figure by a
    few words of verb ("netted", "came to", "left him", ", which netted")
    with nothing between them that names or counts anything else -- and the
    scope names one kind. Kind-neutral scopes ("moves", "claims", "pickups",
    "the waiver wire", "one waiver and one free agent") pass, and so does a
    figure whose own subject is one of them, whatever kind trails after it. Whose moves they are is the one manager
    the clause names, or, in a clause that names none ("his waiver moves"),
    the one manager the nearest earlier clause of the paragraph named; a
    clause naming two managers, or following one that did, is left alone.
    The figure is then a problem only when it is not that kind's net nor the
    swing of a move of that kind, and is another figure on the manager's
    line: the net overall, another kind's net, or another kind's move's
    swing. Returns one line per wrong figure, in the order the editorial
    states them.
    """
    mixed = {line.manager: line for line in _mover_lines(managers) if len(line.kinds) > 1}
    if not mixed:
        return []
    aliases = {
        alias: name
        for alias, (name, is_manager) in _mention_aliases(names.managers, set(names.players)).items()
        if is_manager
    }

    problems: list[str] = []
    for paragraph in summary.splitlines():
        carried: str | None = None
        for clause in _clauses(paragraph):
            named = {aliases[m.group(0)] for m in names.find(clause) if m.group(0) in aliases}
            if named:
                # Two managers named: the next clause's "his" could be either's.
                carried = next(iter(named)) if len(named) == 1 else None
            manager = carried
            line = mixed.get(manager or "")
            if line is None:
                continue
            kinds = line.kinds
            kind_nets = dict(line.kind_nets)
            scopes = _kind_scopes(clause)
            for start, end, text in _clause_figures(clause):
                found = _figure_scope(clause, start, end, scopes)
                kind = found[0].kind if found else None
                if found is None or kind not in kinds:
                    continue
                quoted = found[1]
                own = kinds[kind]
                own_net = kind_nets[kind]
                value = _signed_value(text)
                signed = text[0] in _SIGNS
                allowed = {own_net, *(move.net for move in own)}
                if value in allowed if signed else abs(value) in {abs(figure) for figure in allowed}:
                    continue
                fact = line.misplaced_kind(value, kind, signed=signed)
                if fact is None:
                    continue
                problem = (
                    f'the editorial says "{" ".join(_plain(quoted).split())}" of {manager}, but {fact}, '
                    f"not the {kind}s' own net of {own_net:+d}"
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
    # False for a claim the data contradicts, which every check but one
    # reads. True for a draft that said nothing false and still broke the
    # brief (#459): the words that name it to the retry, to the reader on
    # stderr and in the saved report change with it, because "contradicted
    # the data" would not be true of it.
    broken_brief: bool = False


def _contested(summary: str, data: LeagueRecapData, names: RecapNames) -> list[str]:
    return check_contested_attributions(summary, contested_draft_claims(data["managers"]), names)


def _contested_roll_calls(summary: str, data: LeagueRecapData, names: RecapNames) -> list[str]:
    return check_contested_roll_calls(summary, contested_draft_claims(data["managers"]), names)


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
        code=RECAP_WARNING_KIND_NET_MISATTRIBUTION,
        lead="The editorial gives one kind of a manager's moves a figure that is not that kind's own",
        retry_instruction=(
            "Say a figure is a manager's waivers' or free agents' only when it is that kind's own net "
            "from the brackets of their line, or one of that kind's moves' swings; where a line mixes "
            "kinds, the net overall is all the moves together, so call it their moves' net, never their "
            "waivers' or their free agents'."
        ),
        run=lambda summary, data, names: check_kind_net_scopes(summary, data["managers"], names),
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
    EditorialCheck(
        code=RECAP_WARNING_CONTESTED_ROLL_CALL,
        lead="The editorial lists the managers beaten to one player",
        retry_instruction=(
            "A race needs only its count and its winner, so name at most two of the managers beaten "
            "to any one player, and only where the loss is their story (the leader missed out, or one "
            "manager lost two races), never as a list."
        ),
        run=_contested_roll_calls,
        broken_brief=True,
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


def split_recap_problems(problems: Mapping[str, Sequence[str]]) -> tuple[list[str], list[str]]:
    """`check_recap_editorial()`'s problems as (the claims the data
    contradicts, the briefs the draft broke), each in the order given.

    The two are told apart wherever the editorial's fault is named, since
    only the first is a sentence the data contradicts (#459). A code the
    registry does not know is read as a contradiction, as every problem was
    before the split, so naming the fault never costs a report its callout."""
    contradicted: list[str] = []
    broken: list[str] = []
    for code, found in problems.items():
        check = _CHECKS_BY_CODE.get(code)
        (broken if check is not None and check.broken_brief else contradicted).extend(found)
    return contradicted, broken


def recap_editorial_flaw(problems: Mapping[str, Sequence[str]]) -> str | None:
    """What is wrong with the editorial, completing "The editorial ...": it
    contradicts the data, breaks its brief, or both. None for a clean one."""
    contradicted, broken = split_recap_problems(problems)
    if contradicted and broken:
        return "contradicts the data and breaks the brief it was given"
    if broken:
        return "breaks the brief it was given"
    return "contradicts the data it was given" if contradicted else None


def get_recap_editorial_retry_prompt(
    user_prompt: str,
    problems: Mapping[str, Sequence[str]],
    *,
    stop_reason: str | None = None,
) -> str:
    """The user prompt again, followed by what was wrong with the last draft.

    A second roll of the identical prompt is the same gamble that already
    failed once; naming each wrong claim beside the data it contradicts, and
    saying how to avoid that kind of mistake, gives the retry the one thing
    the first attempt lacked. `problems` is `check_recap_editorial()`'s
    answer, so only the kinds of mistake actually made get an instruction.
    `stop_reason` is the provider's, set only when the last draft hit its
    token ceiling: that draft gets told it was cut off, since a clean check on
    text that stopped short proves nothing (#372). A refusal was not cut off,
    so the caller never passes one.

    A claim the data contradicts and a draft that broke the brief while
    saying nothing false (#459) are told apart: each kind of problem gets
    its own header, so the retry is never told the data contradicted a
    sentence that was true.
    """
    feedback: list[str] = []
    if stop_reason:
        feedback.append(
            f"Your previous draft was cut off before it finished (the provider stopped with {stop_reason})."
        )
    contradicted, broken = split_recap_problems(problems)
    for header, found in (
        ("Your previous draft contradicted the data it was given", contradicted),
        ("Your previous draft broke the brief it was given", broken),
    ):
        if found:
            corrections = "\n".join(f"- {problem}" for problem in found)
            feedback.append(f"{header}:\n{corrections}")
    instructions = " ".join(_CHECKS_BY_CODE[code].retry_instruction for code in problems)
    if stop_reason:
        instructions = f"{instructions} Keep it to the length asked for and finish it.".lstrip()
    feedback_text = "\n".join(feedback)
    return f"{user_prompt}\n\n{feedback_text}\nWrite the recap again from scratch. {instructions}"


def editorial_warning_message(code: str, problems: Sequence[str]) -> str:
    """The JSON warning's message: the check's lead, then one sentence per
    claim. A claim can carry its own semicolons (a quoted contested line
    does), so sentences, not a joiner, are what keep the claims apart."""
    claims = " ".join(f"{problem[:1].upper()}{problem[1:].rstrip('.')}." for problem in problems)
    return f"{_CHECKS_BY_CODE[code].lead}. {claims}"
