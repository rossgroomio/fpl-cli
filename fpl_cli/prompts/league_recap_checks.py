"""Checks the league-recap editorial against the data its prompt carried (#357, #359, #390, #486, #488, #501, #503).

The prompt states every rule these read, and the model has broken each of
them anyway: a manager named as beaten in a waiver race they won, a
manager's whole-gameweek net pinned on one transfer, a tie nobody shared, a
league position given as a gameweek finish, the week's narrow escape from
last place handed to the manager who finished there.
Where the claim is about known strings -- managers, players, figures, ranks
-- it can be read back mechanically instead of trusting a rule the model
has already ignored once.

Each check is narrow by design. It reads only the clauses it can pin down
and lets everything else pass, so a problem it returns is a sentence the
data contradicts, never a guess -- a false alarm costs a retry and puts a
warning above a correct editorial. Two checks break that wording, the roll
call of beaten managers (#459) and the forfeit written out word for word
more than once (#503), since each fires on true sentences: their contract
is the mechanical analogue, a count of known strings and never a judgement
of tone. Both are registered as a broken brief rather than a contradiction,
so the retry, stderr and the saved report say the draft broke its brief
instead of claiming the data contradicts it.
`EDITORIAL_CHECKS` is the one registry: each check's warning code, the prose
its JSON warning opens with, what the retry is told, and the checker itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from fpl_cli.cli._league_recap_types import (
    DraftMoveChain,
    LeagueRecapData,
    RecapContestedClaim,
    RecapFinePenalty,
    RecapManagerEntry,
    RecapSeasonRecord,
    competition_rank,
    contested_draft_claims,
    draft_transaction_chains,
    draft_transaction_kind_label,
    draft_transaction_kind_nets,
    format_contested_claim,
    format_move_counts,
    gameweek_rank,
    last_place_escape,
    place_label,
    points_label,
)
from fpl_cli.utils.text import ORDINAL_WORDS, join_and

# The JSON warning code each check raises when a claim it caught survives
# the retry. Stable for scripts, like every code on that channel.
RECAP_WARNING_CONTESTED_MISATTRIBUTION = "synthesis_contested_misattribution"
RECAP_WARNING_NET_MISATTRIBUTION = "synthesis_net_misattribution"
RECAP_WARNING_KIND_NET_MISATTRIBUTION = "synthesis_kind_net_misattribution"
RECAP_WARNING_UNSUPPORTED_TIE = "synthesis_unsupported_tie"
RECAP_WARNING_WRONG_PLACING = "synthesis_wrong_gameweek_placing"
RECAP_WARNING_ESCAPE_MISATTRIBUTION = "synthesis_escape_misattribution"
RECAP_WARNING_CONTESTED_ROLL_CALL = "synthesis_contested_roll_call"
RECAP_WARNING_PENALTY_REPEATED = "synthesis_penalty_repeated"


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
# A conjunction or a subject pronoun between the manager and the cue opens a
# verb phrase of its own, so that manager need not be its subject: in "Cam
# lost the Bogle race to Bob and was beaten to Giles" Bob is the first verb's
# object, and in "...to Bob, though, and he also missed out on Giles" the
# pronoun restarts the subject (#499). An aside straight after the manager,
# between commas, is still about them ("Bob, he of all people, was beaten to
# Giles"), so it is set aside first, unless it opens on a conjunction.
_NEW_VERB_PHRASE_RE = re.compile(r"(?<!\w)(?:and|but|he|she|they)(?!\w)", re.IGNORECASE)
_LEADING_ASIDE_RE = re.compile(r"^\s*,(?!\s*(?:and|but)(?!\w))[^,]*,", re.IGNORECASE)
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
    ("was never beaten to") drops the clause, and so does a verb phrase of
    its own ("lost to Bob and was beaten to", "and he missed out on"), whose
    subject may be someone the clause named earlier.
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
        or _NEW_VERB_PHRASE_RE.search(_LEADING_ASIDE_RE.sub("", before, count=1))
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
    possessive, a points swing, a verb phrase that may not be the named
    manager's ("lost to Bob and was beaten to Giles") -- is left alone
    rather than guessed at, so every problem returned is a sentence that
    said something the data
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


def _manager_aliases(names: RecapNames) -> dict[str, str]:
    """Every string a check reads as a manager, mapped to their name: the
    full name, and a first or last name no other manager or player shares."""
    return {
        alias: name
        for alias, (name, is_manager) in _mention_aliases(names.managers, set(names.players)).items()
        if is_manager
    }


@dataclass(frozen=True)
class _ReadClause:
    """One clause of an editorial, with every name in it, the managers among
    them as (start, end, name), and its antecedent: the one manager the
    nearest earlier clause of the paragraph to name anyone named, or None
    when that clause named two (the next "he" could be either's) or none
    has. `opener` is the first manager that clause named, whoever else it
    named: the subject a reader takes the next "he" for (#488). None where
    the clause opens on a list of managers ("Alice and Bob both struggled"),
    which has no one subject."""

    text: str
    mentions: list[re.Match[str]]
    named: list[tuple[int, int, str]]
    antecedent: str | None
    opener: str | None

    @property
    def manager(self) -> str | None:
        """The one manager the clause is about: the one it names, or its
        antecedent when it names none. None when it names two."""
        if not self.named:
            return self.antecedent
        distinct = {name for *_, name in self.named}
        return next(iter(distinct)) if len(distinct) == 1 else None


def _read_clauses(summary: str, names: RecapNames, aliases: Mapping[str, str]) -> Iterator[_ReadClause]:
    """Every clause of `summary`, line by line, read for its names. The one
    place a check carries a manager from one clause to a pronoun in the next."""
    for paragraph in summary.splitlines():
        antecedent: str | None = None
        opener: str | None = None
        for clause in _clauses(paragraph):
            mentions = names.find(clause)
            named = [(m.start(), m.end(), aliases[m.group(0)]) for m in mentions if m.group(0) in aliases]
            read = _ReadClause(clause, mentions, named, antecedent, opener)
            yield read
            if named:
                listed = len(named) > 1 and _LIST_JOIN_RE.match(_plain(clause[named[0][1]:named[1][0]]))
                antecedent, opener = read.manager, None if listed else named[0][2]


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
# 6 points" is the player's score, not a move's (#390). Two more ways of
# saying the same take a signed figure only, for the same reason: "was worth
# -9" and "a -9 swing" (#501), where "worth 9" is as often a player's score.
# "Worth a -4 hit", and "worth a -4" with no noun for a move after it, say
# a hit was worth taking, so neither is a move's swing (`_worth_hit_slang`).
_NET_FIGURE_RE = re.compile(
    rf"(?<![\w.])(?P<before>[{re.escape(_SIGNS)}]?\d+)\s*(?:pts?\s+|points?\s+)?net(?!\w)"
    rf"|(?<!\w)net\s+(?:(?:loss|gain|swing|return)\s+)?(?:of\s+)?(?P<after>[{re.escape(_SIGNS)}]?\d+)"
    rf"(?![\w.])(?!{_NOT_POINTS})"
    r"|(?<!\w)net(?:s|ted|ting)\s+(?:(?:him|her|them)\s+)?(?:(?:a|an)\s+)?"
    rf"(?P<verb>[{re.escape(_SIGNS)}]\d+)(?![\w.])(?!{_NOT_POINTS})"
    rf"|(?<!\w)worth\s+(?:(?P<worth_a>a|an)\s+)?(?P<worth>[{re.escape(_SIGNS)}]\d+)(?![\w.])(?!{_NOT_POINTS})"
    r"(?!\s*-?\s*(?:(?:pts?|points?)\s+)?hit(?!\w))"
    rf"|(?<!\w)(?:a|an)\s+(?P<swing>[{re.escape(_SIGNS)}]\d+)(?:[-\s]?(?:pts?|points?))?[-\s]+swing(?!\w)",
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
# words), its size given or not ("-6 after a 4-point hit", "after a
# four-point hit", #499). Further off, the week is when something else
# happened ("-6 net, the worst move of the week") and the hit is the one taken
# to make the move ("paid a hit to bring in Gibbs-White for Wirtz"), which
# says nothing about whose the figure is.
_HIT_SIZE = rf"(?:[{re.escape(_MINUS_SIGNS)}]?\d+|four|eight|twelve|sixteen|twenty)[-\s]?(?:pts?|points?)?\s+"
_NET_BESIDE_RE = re.compile(
    r"(?<!\w)(?:week|gameweek|gw|post-hit|after hits"
    rf"|(?:after|once|with|including|net of|despite) (?:the|a|his|her|their) (?:{_HIT_SIZE})?hit)(?!\w)",
    re.IGNORECASE,
)
_NET_BESIDE_WORDS = 4
# A signed figure standing on its own -- "went -4", "(+2)" -- read as a move's
# swing. Never a hit ("-4 hit", "a -4-point hit") and never the slang for one
# ("took a -4"), which says what the manager paid rather than what a move did.
# A noun for a move after "a -4" makes it that move's ("a -4 move", #499),
# unless a verb of paying leads into it: a hit is a multiple of 4, so "took a
# -4 swap to bring in Gibbs-White" is still the slang.
_SIGNED_FIGURE_RE = re.compile(
    rf"(?<![\w.])[{re.escape(_SIGNS)}]\d+(?![\w.])(?!\s*-?\s*(?:(?:pts?|points?)\s+)?hit(?!\w))",
)
_HIT_SLANG_BEFORE_RE = re.compile(
    r"(?<!\w)(?:(?P<paid>took|take|takes|taking|paid|pay|pays|paying)(?:\s+an?)?|an?)\s*$", re.IGNORECASE,
)
_MOVE_NOUN_AFTER_RE = re.compile(r"\s*(?:move|swap|switch|swing)(?!\w)", re.IGNORECASE)


def _hit_slang(clause: str, figure: re.Match[str]) -> bool:
    """Whether the signed `figure` in `clause` is the slang for a hit rather
    than a move's swing -- see `_HIT_SLANG_BEFORE_RE`."""
    slang = _HIT_SLANG_BEFORE_RE.search(clause, 0, figure.start())
    return slang is not None and (slang.group("paid") is not None or not _MOVE_NOUN_AFTER_RE.match(clause, figure.end()))


def _worth_hit_slang(clause: str, figure: re.Match[str]) -> bool:
    """Whether a "worth a -4" `figure` read by `_NET_FIGURE_RE` is the slang
    for a hit worth taking: a negative multiple of 4 after "worth a", with
    no noun for a move after it ("worth a -4 move" is the move's, #499)."""
    if figure.group("worth_a") is None:
        return False
    value = _signed_value(figure.group("worth"))
    return value < 0 and value % 4 == 0 and not _MOVE_NOUN_AFTER_RE.match(clause, figure.end())


def _signed_value(text: str) -> int:
    return int(text.lstrip(_SIGNS)) * (-1 if text[0] in _MINUS_SIGNS else 1)


def _net_figure_text(match: re.Match[str]) -> str:
    """The figure a `_NET_FIGURE_RE` match read, sign and all."""
    return next(text for text in match.group("before", "after", "verb", "worth", "swing") if text)


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
    # A draft line's chains (#501), the ones its roster line marks; empty
    # for transfers.
    chains: tuple[DraftMoveChain, ...] = ()

    @property
    def effective(self) -> tuple[_Move, ...]:
        """Each open chain's effective move as a move of its own (#501), so a
        clause that names it is read like one the roster lists."""
        return tuple(
            _Move(
                self.manager, chain.player_in, chain.player_out, chain.net,
                draft_transaction_kind_label(chain.moves[-1]["kind"]),
            )
            for chain in self.chains if not chain.closed
        )

    def _chain_facts(self, move: _Move) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        """The effective swing of each open chain on the line other than the
        one `move` is the effective move of, as (the chains `move` is a hop
        of, the rest)."""
        own: list[tuple[int, str]] = []
        rest: list[tuple[int, str]] = []
        for chain in self.chains:
            if chain.closed or (move.player_in, move.player_out) == (chain.player_in, chain.player_out):
                continue
            fact = (chain.net, (
                f"{chain.net:+d} is the effective swing of {self.manager}'s chain {chain.path}, "
                f"which belongs to {chain.player_in} in for {chain.player_out}"
            ))
            hop = any((m["player_in"], m["player_out"]) == (move.player_in, move.player_out) for m in chain.moves)
            (own if hop else rest).append(fact)
        return own, rest

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
        else. A `signed` value is matched with its sign first. A move inside
        a chain is read against the chain's effective swing first, since that
        is the figure the roster gives for it, and any other chain's after
        the moves' swings (#501)."""
        own_chain, other_chains = self._chain_facts(move)
        figures: list[tuple[int, str]] = list(own_chain)
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
        figures.extend(other_chains)
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
                tuple(draft_transaction_chains(transactions, m.get("squad", []))),
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
    tied to the word "net", or a signed one after "worth" or before "swing"
    ("was worth -9", "a -9 swing", #501). The figure is then the move's when
    the move is the nearest thing named before it, and the manager's when the
    manager is ("Gibbs-White for Wirtz went -4, and Alice was -6 net"). It is a problem
    only when it is the move's, is not that move's swing, and is another
    figure on the same line, so a number from anywhere else is never guessed
    at. A clause that marks the figure as the manager's ("overall", "across
    both", "after the hit") or states the move's own swing beside it passes.
    A draft chain's effective move is read as a move of its own, its swing
    the chain's (#501): "Penders in for Kinsky was worth -3" gives it the
    swing of a hop. A move inside the chain is read against the chain's
    effective swing first: "Penders in for Henderson was worth -9" gives a
    move that only replaced a player who passed through the swing of the
    whole chain, and the problem says which move that swing is.
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
            move for line in lines for move in (*line.moves, *line.effective)
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
            _signed_value(m.group(0)) == move.net and not _hit_slang(clause, m)
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
            if figure.group("worth") and _worth_hit_slang(clause, figure):
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
# A pronoun that opens the gap after a kind starts a clause with a new
# subject: "with Dunk's +1 on waivers he finished +7" (#465). Its object
# forms ("left him +3") carry a kind's figure on, so they are not here.
_KIND_LINK_NEW_SUBJECTS = frozenset({"he", "she", "they"})
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


def _kind_aside(clause: str, scope: _KindScope, gap: str, figures: Sequence[tuple[int, int, str]]) -> bool:
    """Whether `scope` is an aside rather than the subject of the figure
    after it, `gap` between them (#465): the kind already has a figure of
    its own ("with Dunk's +1 on waivers") and a new subject opens the gap
    ("he finished +7"), so the second figure is the manager's. Neither alone
    frees it: "on waivers he netted +7" and "his +1 on waivers left him +7"
    still say it of the kind."""
    first = _LINK_WORD_RE.findall(_plain(gap).lower())[:1]
    return bool(first) and first[0] in _KIND_LINK_NEW_SUBJECTS and any(
        figure_end <= scope.start and _kind_link(clause[figure_end:scope.start], _KIND_LINK_AFTER)
        for _, figure_end, _ in figures
    )


def _figure_scope(
    clause: str, start: int, end: int, scopes: Sequence[_KindScope], figures: Sequence[tuple[int, int, str]],
) -> tuple[_KindScope, str] | None:
    """The scope the figure at `start`-`end` is said of, with the text from
    one to the other, or None when no scope is linked to it. `figures` are
    the clause's own, from `_clause_figures`.

    Its subject is the scope nearest before it, when the two are linked and
    the scope is no aside, and a neutral subject keeps the figure whatever
    kind follows. Only a figure with no subject before it is read against a
    scope after it ("netted +3 overall from his waivers")."""
    before = [scope for scope in scopes if scope.end <= start]
    # A figure's leading verb ("netting +3") is part of the link it makes.
    verb = re.match(r"[a-z]+", clause[start:end], re.IGNORECASE)
    if before:
        gap = clause[before[-1].end:start] + (verb.group(0) if verb else "")
        if _kind_link(gap, _KIND_LINK_BEFORE, comma=True) and not _kind_aside(clause, before[-1], gap, figures):
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
    figure whose own subject is one of them, whatever kind trails after it.
    So does a figure after a kind that is only an aside: one that already
    has its own figure, with a new subject opening the gap to the next
    ("with Dunk's +1 on waivers he finished +7", #465). Whose moves they are is the one manager
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

    problems: list[str] = []
    for read in _read_clauses(summary, names, _manager_aliases(names)):
        clause = read.text
        manager = read.manager
        line = mixed.get(manager or "")
        if line is None:
            continue
        kinds = line.kinds
        kind_nets = dict(line.kind_nets)
        scopes = _kind_scopes(clause)
        figures = _clause_figures(clause)
        for start, end, text in figures:
            found = _figure_scope(clause, start, end, scopes, figures)
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
# A score ranked against the season ("the joint-highest score of the
# season") also reads against the season's record, which the League History
# section states with every score that reached it (#494).
_TIE_SEASON_RE = re.compile(r"(?<!\w)season\w*(?!\w)", re.IGNORECASE)
# The words that name the table or the season outright. "Spot", "place" and
# "position" do not: a week has a top spot too ("shared the top spot on 55
# points", #511).
_TIE_TABLE_NAMED_RE = re.compile(
    r"(?<!\w)(?:table|standings|league|overall|season\w*|totals?|summit)(?!\w)", re.IGNORECASE,
)
# The figure a tie is said of, attached to it the way a placing's figure
# is: straight before it, joined by a copula or a comma ("Bob's 65 was the
# joint-highest score of the season", "hit 65, the joint-highest"), or
# straight after it ("the joint-highest score of the season is 72", "shared
# the top spot on 55 points"). A figure further off makes some other
# comparison ("65, short of the joint-highest score of the season"), and a
# gameweek's own number ("gameweek 4 was the joint-highest") is no score.
_TIE_FIGURE_BEFORE_RE = re.compile(
    r"(?<![\w.£+−–-])(?P<figure>\d+)(?:\s*(?:pts?|points?))?"
    r"(?:\s*,|\s+(?:was|is|were|are|as|makes?|made|marks?|marked|stands?|stood)(?:\s+(?:also|now|still))?)"
    r"\s+(?:the\s+)?$",
    re.IGNORECASE,
)
_TIE_FIGURE_AFTER_RE = re.compile(
    r"(?:[\s-]+(?:gameweek\s+)?(?:scores?|totals?|tally|week|return|spot|place|position))?"
    r"(?:\s+(?:of|this|in)\s+(?:the\s+)?season\w*)?"
    r"(?:\s*[,(]|\s+(?:on|at|with|of|is|was))\s*(?P<figure>\d+)(?![\w.%])",
    re.IGNORECASE,
)
_GAMEWEEK_NUMBER_BEFORE_RE = re.compile(r"(?<!\w)(?:(?:game)?week|gw)\s*$", re.IGNORECASE)
# A figure the tie's own holders are said to share, before it: "the 46 that
# Manager B and Manager C shared for second" (#511). Read only as a gameweek
# score the tie's rank holds, never against a season record.
_TIE_HELD_FIGURE_BEFORE_RE = re.compile(
    r"(?<![\w.£+−–-])(?P<figure>\d+)(?:\s*(?:pts?|points?))?\s+(?:that|which)\s+(?:[^\s\d,;:–—]+\s+){1,8}$",
    re.IGNORECASE,
)
_TIE_WINDOW_WORDS = 6
_TIE_NEGATION_WORDS = 5


def _rank_holders(ranks: Sequence[tuple[str, int]], rank: int | str) -> tuple[int, list[str]]:
    """The rank `rank` names ("top", "bottom" or a number) and who holds it."""
    values = [value for _, value in ranks]
    target = min(values) if rank == "top" else max(values) if rank == "bottom" else int(rank)
    return target, [name for name, value in ranks if value == target]


def _tie_figures(clause: str, start: int, end: int) -> list[int]:
    """The figures attached to the tie at `start`-`end` of `clause`
    (`_TIE_FIGURE_BEFORE_RE`, `_TIE_FIGURE_AFTER_RE`), never a gameweek's own
    number or a gap."""
    figures: list[int] = []
    before = _TIE_FIGURE_BEFORE_RE.search(clause, 0, start)
    if before and not _GAMEWEEK_NUMBER_BEFORE_RE.search(clause, 0, before.start("figure")):
        figures.append(int(before.group("figure")))
    after = _TIE_FIGURE_AFTER_RE.match(clause, end)
    if after and not _NOT_A_SCORE_AFTER_RE.match(clause, after.end("figure")):
        figures.append(int(after.group("figure")))
    return figures


def check_tie_claims(
    summary: str,
    managers: Sequence[RecapManagerEntry],
    names: RecapNames,
    *,
    season_records: Mapping[str, RecapSeasonRecord] | None = None,
) -> list[str]:
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
    never states. A figure attached to the tie (`_tie_figures`) that is the
    gameweek score at its rank, held by more than one manager, bears it out
    on the week unless a word names the table or the season ("who shared
    second on 46", #511). Returns one line per unsupported tie, in the order the
    editorial claims them.

    `season_records` is the season's highest ("top") and lowest ("bottom")
    gameweek score as the League History section states them (#494). A
    highest or lowest score said of the season ("the joint-highest score of
    the season") then has a third reading, the record, which holds where
    more than one score reached it and the tie is said of no other figure
    (`_tie_figures`); the record only ever adds a reading, so a tie the week
    or the table bears out still passes.
    """
    if len(managers) < 2:
        return []
    # Both rankings come from the one definition the prompt's GW Rank column
    # is counted with (#486), so a tie means the same thing on either side.
    gameweek = [(m["manager_name"], gameweek_rank(m, managers)) for m in managers]
    points = {m["manager_name"]: m["gw_points"] for m in managers}
    totals = [(m["manager_name"], m.get("total_points") or 0) for m in managers if "total_points" in m]
    # The table's Pos column first -- the one a "nobody is 8th" is read off --
    # then its Prev column and the ranks its Total column implies.
    tables = [
        ranks for ranks in (
            [(m["manager_name"], m.get("overall_rank") or 0) for m in managers if m.get("overall_rank")],
            [(m["manager_name"], m.get("previous_rank") or 0) for m in managers if m.get("previous_rank")],
            [(name, competition_rank(total, (other for _, other in totals))) for name, total in totals],
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

    records = season_records or {}

    def season_fact(rank: str, figures: Sequence[int]) -> str | None:
        record = records[rank]
        which = "highest" if rank == "top" else "lowest"
        if len(record["scores"]) == 1:
            name, week = record["scores"][0]
            return f"the season's {which} gameweek score, {points_label(record['points'])}, was {name}'s alone, in GW{week}"
        # A shared record bears out a tie said of its own score, or of no
        # figure at all, but not one said of a score that never reached it:
        # "Bob's 65 was the joint-highest score of the season" beside a 72
        # scored twice is the claim #494 exists to stop.
        other = [figure for figure in figures if figure != record["points"]]
        if not other:
            return None
        return (
            f"the season's {which} gameweek score is {points_label(record['points'])}, "
            f"not {join_and([points_label(figure) for figure in other])}"
        )

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
            figures = _tie_figures(clause, match.start(), match.end())
            # A figure attached to the tie that is the gameweek score at its
            # rank, held by more than one manager, bears it out on the week
            # where no word names the table or the season: "who shared second
            # on 46", "shared the top spot on 55 points", "the 46 that Manager
            # B and Manager C shared for second" (#511). Any other
            # figure, or "joint-second in the table on 46", leaves the reading
            # to the words.
            _, holders = _rank_holders(gameweek, rank)
            held = _TIE_HELD_FIGURE_BEFORE_RE.search(clause, 0, match.start())
            week_figures = [*figures, int(held.group("figure"))] if held else figures
            if len(holders) > 1 and points[holders[0]] in week_figures and not _TIE_TABLE_NAMED_RE.search(window):
                continue
            if isinstance(rank, str) and rank in records and about_week and _TIE_SEASON_RE.search(window):
                readings = (gameweek_fact(rank), table_fact(rank), season_fact(rank, figures))
                fact = None if None in readings else "no reading holds: " + "; ".join(
                    reading for reading in readings if reading is not None
                )
            elif about_week and not about_table:
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
# Gameweek placings (#486)
# =============================================================================

_RANK_WORDS = r"last|bottom|top|\d{1,2}(?:st|nd|rd|th)|" + "|".join(_ORDINAL_NUMBERS)
# A placing: an ordinal ("seventh", "7th"), "last", "bottom" or "top", after a
# tie word ("joint-seventh") or "rock-" if any, and before the "-best" or
# "-highest" that ranks a score ("third-best score") or the noun a placing
# takes ("eighth place", "a second-place finish"). A rank glued to another
# word by a hyphen ("first-round", "second-last", "last-placed") is not one.
_PLACING_RE = re.compile(
    rf"(?<![\w-])(?:(?:joint|equal|tied)[-\s]+(?:for\s+)?)?(?:rock-)?(?P<rank>{_RANK_WORDS})"
    r"(?:(?P<best>[-\s]+(?:best|highest))(?:[-\s]+(?:score|week|gameweek|return|tally))?"
    r"|[-\s]+(?:place|spot|position)(?:[-\s]+finish)?)?(?![\w-])",
    re.IGNORECASE,
)
# What may follow a placing for it to be one: the clause's end, a bracket or
# a dash, or a word that places it ("eighth on 31", "third for the week",
# "last in the gameweek", "seventh this week"). Anything else counts or names
# something: "third win", "first time", "last week", "top scorer", "second
# and third".
_PLACING_FOLLOWS_RE = re.compile(
    r"\s*(?:$|[,.;:!?()–—]|-\s|(?:in|for|on|with|of|at|this|after|among)(?!\w))", re.IGNORECASE,
)
# What before a rank makes it no placing: another rank it is counted from or
# listed with ("second last", "second and third"), "at" ("at last") or "next
# to" ("next to last").
_NOT_A_PLACING_BEFORE_RE = re.compile(
    rf"(?<!\w)(?:(?:{_RANK_WORDS})\s*(?:,|and|&|or|-)?|at(?:\s+the)?|next(?:[-\s]+to)?)\s*$", re.IGNORECASE,
)
# A placing is read inside its own stretch of the clause, between the
# commas, dashes and conjunctions either side, so "Alice won the week, and
# Bob finished eighth in the table" keeps the week away from Bob's eighth.
_PLACING_STRETCH_BREAK_RE = re.compile(
    r"[,;:–—]|\s-\s|\s(?:and|but|while|whereas|though|although)\s", re.IGNORECASE,
)
# The week named as a placing's scope after it ("third on the week", "last
# in the gameweek", "bottom of the GW charts") or as its owner before it
# ("this week's third-best") makes it the week's. "This week" alone only
# dates it ("still top this week" may be the table), a plural is a span
# ("two gameweeks on top"), and a numbered one ("in GW5 too") may be another
# week than the one recapped.
_PLACING_WEEK = r"(?:the|this)\s+(?:(?:game)?week|gw)"
_PLACING_WEEK_AFTER_RE = re.compile(
    rf"(?<!\w)(?:on|for|in|of)\s+{_PLACING_WEEK}(?:['’]s)?(?![\w'’-])(?!\s+(?:before|after|prior))",
    re.IGNORECASE,
)
_PLACING_WEEK_BEFORE_RE = re.compile(rf"(?<!\w){_PLACING_WEEK}['’]s(?!\w)", re.IGNORECASE)
# Any word for the table or a season, for holding a place in it, or for a
# span longer than the week makes a placing the league's or something else's
# -- "went into this gameweek sat top", "kept top spot this week", "her
# second-best score this campaign", "the second-best of his career" -- so it
# is left alone. So is a placing a move lands on, counts from or leaves ("up
# to 4th", "from 9th to 5th", "into 7th", "second from the bottom", "slipped
# out of last place on the week", #499).
_HOLDING_WORDS = r"stay\w*|sits?|sitting|sat|kept|keeps?|keeping|holds?|held|holding"
_PLACING_TABLE_RE = re.compile(
    r"(?<!\w)(?:table|league|standings|overall|season\w*|totals?|summit|ladder"
    rf"|{_HOLDING_WORDS}|leads?|leading|leader"
    r"|career|years?|campaign|months?|run|ever|all-time|history|record|since|weeks|gameweeks)(?!\w)",
    re.IGNORECASE,
)
_PLACING_MOVE_RE = re.compile(r"(?<!\w)(?:to|into|from|out\s+of)\s+(?:the\s+)?$", re.IGNORECASE)
# A move up or down the table does the same ("slipped two places, 3rd in a
# week when 45 got you up two places"), unless the week is named straight
# after the placing: "rocketed up five places to snatch second on the week"
# ranks the week.
_PLACING_MOVE_WORDS_RE = re.compile(
    r"(?<!\w)(?:places|positions|climb\w*|rose|ris(?:e|es|en|ing)|jump\w*|leap\w*|surg\w*|soar\w*|rocket\w*"
    r"|slip\w*|slid\w*|slump\w*|drop\w*|fell|fall\w*|tumbl\w*|plummet\w*|sank|sink\w*|sunk|mov(?:e|ed|es|ing)"
    r"|(?:up|down)\s+(?:a|one|two|three|four|five|six|seven|eight|nine|ten|\d+))(?!\w)",
    re.IGNORECASE,
)
# Words before a placing, in its own stretch, that say it did not happen,
# nearly did, or belongs to someone the manager beat or escaped: "would have
# been second", "a point off top", "one better than last", and "avoided last
# place on the week" -- the prompt's own words for the manager one score
# above the lowest (#487). "Could" hedges only with "have": "could only
# manage eighth" is a finish, and so is "no better than eighth".
_PLACING_HEDGE_RE = re.compile(
    r"(?<!\w)(?:would|might|should|if|unless|near|nearly|almost|close|within|off|outside|short"
    r"|avoid\w*|escap\w*|dodg\w*|evad\w*|miss\w*|surviv\w*|spar\w*|clear|above|ahead|beat\w*|outscor\w*"
    r"|over|instead|rather|could(?=(?:\s+\w+)?\s+have(?!\w))"
    r"|(?<!no better )(?<!nothing better )(?<!no higher )than)(?!\w)|could['’]ve|['’]d(?!\w)",
    re.IGNORECASE,
)
_PLACING_HEDGE_WORDS = 5
_PLACING_NEGATION_RE = re.compile(rf"{_NEGATION_RE.pattern}|(?<!\w)no\s+longer(?!\w)", re.IGNORECASE)
# A placing nobody is named before goes back to an earlier clause's manager
# through any pronoun, the object ("it only bought him a second-place
# finish") as much as the subject; a pronoun between a name and the placing,
# in a clause naming two managers, could be either's.
_PRONOUN_RE = re.compile(r"(?<!\w)(?:he|him|his|she|her|they|them|their)(?!\w)", re.IGNORECASE)
# A person between the manager (named, or the pronoun for them) and the
# placing is whose placing it is: "Ann's brother finished last", "Ann watched
# a rival take top spot", "Her rival took third".
_PLACING_OTHER_PERSON_RE = re.compile(
    r"(?<!\w)(?:rivals?|brothers?|sisters?|mates?|friends?|partners?|wife|husband|dad|father|mum|mother|sons?"
    r"|daughters?|colleagues?|boss|winners?|losers?|runners?-up|managers?|opponents?|neighbours?|everyone"
    r"|everybody|someone|somebody|nobody|anyone|anybody|others|man|woman|guy|bloke)(?!\w)",
    re.IGNORECASE,
)
# A relative pronoun opens a clause with its own subject, which is the
# manager's only when it follows the name ("Bob, who finished eighth"): "Bob
# beat only the manager who came last" puts nobody's placing on Bob.
_RELATIVE_RE = re.compile(r"(?<!\w)(?:who|whom|whose|which)(?!\w)", re.IGNORECASE)
# A finishing verb straight before a placing makes it the manager's finish
# ("finished eighth", "came in last", "good for eighth"), which the clause's
# own gameweek score makes the week's ("scored 31 and finished eighth").
_FINISH_BEFORE_RE = re.compile(
    r"(?<!\w)(?:finish(?:ed|es|ing)?|came|comes?|coming|placed|end(?:ed|s|ing)?(?:\s+up)?|good\s+for"
    r"|enough\s+for)\s+(?:in\s+)?$",
    re.IGNORECASE,
)
# A figure where it stands: never signed, a decimal, part of a word ("GW31")
# or a percentage, nor a gap or a count of places ("31 points behind", "3
# places").
_FIGURE_RE = re.compile(r"(?<![\w.£+−–-])\d+(?![\w.%])")
_NOT_A_SCORE_AFTER_RE = re.compile(
    r"\s*(?:-?\s*(?:pts?|points?))?\s*(?:off|behind|clear|adrift|ahead|back|short|more|fewer|less|better"
    r"|worse|from|places?|spots?|positions?|net|overall|total|in\s+total)(?!\w)",
    re.IGNORECASE,
)
# A figure attached to a placing: straight after it ("eighth on 31",
# "seventh with just 31 points", "eighth (31)") or straight before it,
# joined by "for" ("scored 35 for 6th place").
_FIGURE_AFTER_PLACING_RE = re.compile(
    r"\s*,?\s*(?:(?:on|with)\s+(?:(?:a|an|just|only|mere|paltry|measly)\s+)*|\(\s*)(?P<figure>\d+)(?![\w.%])",
    re.IGNORECASE,
)
_FIGURE_BEFORE_PLACING_RE = re.compile(
    r"(?<![\w.£+−–-])(?P<figure>\d+)(?:\s*(?:pts?|points?)|-point\w*)?\s+for\s+(?:an?\s+)?$", re.IGNORECASE,
)
# A score's own rank on the week, stated straight after the figure and set
# off from it by a comma, bracket or dash or by "which was" and the like:
# "52, which was 4th best on the week", "27 points, the week's lowest", "52,
# only seventh on the week". Where it is the score's true rank, a placing
# the clause gives that manager is the table's (#518).
_WEEK_RANK_LEAD = r"(?:which|that|it|was|is|ranked|ranks|came|comes)"
_WEEK_RANK_AFTER_SCORE_RE = re.compile(
    rf"(?:\s*(?:pts?|points?))?(?:\s*(?:[,(–—]|\s-)|(?=\s+{_WEEK_RANK_LEAD}\s))\s*(?:(?:which|that|it)\s+)?"
    r"(?:(?:was|is|ranked|ranks|came|comes)\s+)?(?:(?:only|just|merely)\s+)?(?:(?:the|a)\s+)?"
    rf"(?P<owner>{_PLACING_WEEK}['’]s\s+)?(?:(?:joint|equal)[-\s]+)?"
    rf"(?P<rank>highest|lowest|best|worst|{_RANK_WORDS})(?P<best>[-\s]+(?:best|highest))?(?![\w-])"
    rf"(?(owner)|(?:[-\s]+(?:score|return|tally))?\s+{_PLACING_WEEK_AFTER_RE.pattern})",
    re.IGNORECASE,
)
# A word for the table or for holding a place before a placing gives way to
# a score attached to the placing and the week named straight after it:
# "sits 5th on 39 for the week" ranks the week. The week alone does not
# ("kept top spot for the week"), nor the week said of something else
# further on ("stays top with the captaincy call of the week").
_PLACING_WEEK_AFTER_SCORE_RE = re.compile(
    rf"(?:\s*(?:pts?|points?))?\s+{_PLACING_WEEK_AFTER_RE.pattern}", re.IGNORECASE,
)


def _claim_stretch(clause: str, start: int, end: int) -> tuple[int, int]:
    """Where the stretch of `clause` holding `start`-`end` begins and ends:
    the placing, or the escape from last place, that the checks below read."""
    lo = max((m.end() for m in _PLACING_STRETCH_BREAK_RE.finditer(clause, 0, start)), default=0)
    following = _PLACING_STRETCH_BREAK_RE.search(clause, end)
    return lo, following.start() if following else len(clause)


def _states(text: str, value: int) -> bool:
    """Whether `text` states `value` as a figure of its own, not a gap."""
    return any(
        int(m.group(0)) == value and not _NOT_A_SCORE_AFTER_RE.match(text, m.end())
        for m in _FIGURE_RE.finditer(text)
    )


def _claim_negated(text: str, start: int, stretch_start: int, finished: bool) -> bool:
    """Whether a negation in the words before the claim at `start` -- a
    placing, or an escape from last place -- is said of it.

    A negation reaches past a stretch break ("wasn't, as some feared,
    last"), but one with a verb of its own before that break, when a
    finishing verb or an escape leads into the claim (`finished`), is that
    verb's: in "didn't escape, finishing 15th" it is the escape that did not
    happen.
    One with nothing of its own before the break ("didn't, in the end,
    finish last") still reaches the placing, as one in its own stretch
    does ("didn't finish last").
    """
    for negation in _PLACING_NEGATION_RE.finditer(text, _window_start(text, start, _PLACING_HEDGE_WORDS), start):
        if finished and negation.end() <= stretch_start:
            closed = _PLACING_STRETCH_BREAK_RE.search(text, negation.end(), stretch_start)
            if closed and _WORD_RE.search(text, negation.end(), closed.start()):
                continue
        return True
    return False


def _claim_subject(
    clause: _ReadClause, start: int, stretch_start: int, *, opener: bool = False,
) -> tuple[str, int] | None:
    """Who the claim at `start` -- a placing, or an escape from last place --
    is said of, and where the name that says so ends (0 for one carried from
    an earlier clause), or None when the clause cannot say. The one reading
    of a claim's subject, so the placing and escape checks carry a pronoun
    the same way (#488).

    It is the manager named nearest before it; where the clause names two or
    more before it, only the one its own stretch names ("Alice won the week,
    and Bob was third on the week"). A claim nobody is named before goes to
    the clause's antecedent when a pronoun before it points back ("He
    finished eighth on 31", "it left him eighth on 31", "He escaped the
    wooden spoon"), or, with `opener`, to the first manager that clause
    named even where it named two. A pronoun between the name and the claim
    in a clause naming two managers could be either's, so it says neither.
    """
    text = clause.text
    before = [mention for mention in clause.named if mention[1] <= start]
    if not before:
        carried = clause.opener if opener else clause.antecedent
        if carried is not None and _PRONOUN_RE.search(text[:start]):
            return carried, 0
        return None
    if len({who for *_, who in before}) > 1:
        before = [mention for mention in before if mention[0] >= stretch_start]
        if len({who for *_, who in before}) != 1:
            return None
    _, end, name = before[-1]
    if len({who for *_, who in clause.named}) > 1 and _PRONOUN_RE.search(text[end:start]):
        return None
    return name, end


def check_gameweek_placings(
    summary: str, managers: Sequence[RecapManagerEntry], names: RecapNames,
) -> list[str]:
    """Every placing on the week the editorial gives a manager whose
    gameweek score does not earn it (#486).

    The GW Standings table runs in league order, and the editorial read a
    manager's last-row Pos beside their GW Pts of 31 as "finished eighth on
    31" for a manager seventh on the week (#483). The table now carries a GW
    Rank column and the prompt a rule tying each placing to its column, and
    the week's runner-up was still called "third on the week". Every manager's
    gameweek placing is known -- `gameweek_rank()`, the GW Rank column's own
    -- so the claim is checkable.

    A placing is an ordinal ("seventh", "7th", "third-best"), "last", "bottom"
    or "top" standing where a placing does ("finished eighth on 31", "third
    for the week", "a second-place finish in the week"), never one that
    counts ("his third win", "last week", "second and third"). It is read as
    the week's when its own stretch of the clause says so -- the week named
    as its scope after it ("on the week", "in the gameweek") or its owner
    before it ("this week's third-best") -- or when the manager's gameweek
    score is attached to it ("eighth on 31", "35 for 6th place") or a
    finishing verb leads into it in a clause that gives that score and names
    nobody else ("scored 31 and finished eighth"): the #483 shape, which the
    prompt forbids whatever the placing was meant as. A score clause that
    also gives the manager's season total, or the score's own rank on the
    week ("first with 52, which was 4th best on the week", #518), is about
    the table, unless a finishing verb leads into a placing that score is
    attached to ("finished fifth on 27, the week's lowest"). A stretch with any word for the table, a season, holding a place or a span longer than
    the week ("in the league", "kept top spot", "this campaign") is left
    alone, unless the word comes before the placing and its score and the
    week straight after it ("sits 5th on 39 for the week"). So is a placing a move lands
    on, counts from or leaves ("up to 4th", "second from the bottom", "climbed
    out of last place") and one beside
    a move up or down the table, unless the week is named straight after it
    ("rocketed up five places to snatch second on the week"). So is a
    placing that is hedged, negated or escaped ("would have been second",
    "avoided last place on the week"), though a negation on a verb of its
    own does not reach past a finishing verb ("didn't escape, finishing
    15th"), and one said near a player, another subject in its own stretch
    or another person ("his captain", "her rival", "the manager who came
    last"), and one in a "respectively" list. Whose it is comes from `_claim_subject()`. "Last" and "bottom"
    hold for every manager on the week's lowest score and "top" for every one
    on its highest; a numbered placing is the manager's gameweek rank, level
    scores sharing one, and a "-best" is also allowed its rank among the
    distinct scores ("the second-best score" after a shared top one). Returns
    one line per wrong placing, in the order the editorial gives them.
    """
    if len(managers) < 2:
        return []
    # Keyed as the names are read: stripped, as `_mention_aliases` keys them.
    by_name = {m["manager_name"].strip(): m for m in managers}
    ranks = {name: gameweek_rank(m, managers) for name, m in by_name.items()}
    scores = sorted({m["gw_points"] for m in managers}, reverse=True)
    bottom_of_table = max((m.get("overall_rank") or 0 for m in managers), default=0)
    # In a league's first gameweek every total is that week's score, so a
    # figure that is both is the score; any other week it could be either.
    totals_are_scores = all(m.get("total_points") == m["gw_points"] for m in managers)

    def is_score(figure: int, manager: RecapManagerEntry) -> bool:
        return figure == manager["gw_points"] and (totals_are_scores or figure != manager.get("total_points"))

    def holders(score: int) -> str:
        return join_and(sorted(name for name, m in by_name.items() if m["gw_points"] == score))

    def fact(name: str, word: str, claimed: int | None) -> str:
        manager = by_name[name]
        rank = ranks[name]
        level = sorted(other for other, r in ranks.items() if r == rank and other != name)
        said = f"{name}'s {points_label(manager['gw_points'])} was {place_label(rank)} on the week"
        if level:
            said += f", level with {join_and(level)}"
        if word in ("last", "bottom"):
            said += f"; the lowest gameweek score was {points_label(scores[-1])}, held by {holders(scores[-1])}"
            if manager.get("overall_rank") == bottom_of_table:
                said += f", and {name} is bottom of the table"
        elif word == "top":
            said += f"; the highest gameweek score was {points_label(scores[0])}, held by {holders(scores[0])}"
            if manager.get("overall_rank") == 1:
                said += f", and {name} is top of the table"
        elif claimed is not None and manager.get("overall_rank") == claimed:
            said += f"; {place_label(claimed)} is {name}'s place in the table"
        return said

    def holds(name: str, word: str, *, best: bool) -> bool:
        """Whether `name`'s gameweek score holds the rank `word` names: the
        top or bottom score for a word for either end, else their gameweek
        rank, or with a "-best" their rank among the distinct scores too."""
        score = by_name[name]["gw_points"]
        if word in _TOP_RANK_WORDS or word in _BOTTOM_RANK_WORDS:
            return score == (scores[0] if word in _TOP_RANK_WORDS else scores[-1])
        claimed = _ORDINAL_NUMBERS.get(word) or int(word[:-2])
        return ranks[name] == claimed or (best and scores.index(score) + 1 == claimed)

    def states_week_rank(text: str, name: str) -> bool:
        """Whether `text` follows `name`'s gameweek score with its true rank
        on the week (`_WEEK_RANK_AFTER_SCORE_RE`). A rank the score does not
        hold says nothing about the placing beside it."""
        score = by_name[name]["gw_points"]
        if not is_score(score, by_name[name]):
            return False
        for figure in _FIGURE_RE.finditer(text):
            stated = _WEEK_RANK_AFTER_SCORE_RE.match(text, figure.end()) if int(figure.group(0)) == score else None
            if stated and holds(name, stated.group("rank").lower(), best=bool(stated.group("best"))):
                return True
        return False

    aliases = _manager_aliases(names)
    problems: list[str] = []
    for clause in _read_clauses(summary, names, aliases):
        text = clause.text
        if "respectively" in text.lower():
            continue
        first_player = min(
            (m.start() for m in clause.mentions if m.group(0) in names.players and m.group(0) not in aliases),
            default=len(text),
        )
        for match in _PLACING_RE.finditer(text):
            start, end = match.span()
            if not _PLACING_FOLLOWS_RE.match(text, end) or _NOT_A_PLACING_BEFORE_RE.search(text[:start]):
                continue
            lo, hi = _claim_stretch(text, start, end)
            before, after = text[lo:start], text[end:hi]
            figure_after = _FIGURE_AFTER_PLACING_RE.match(text, end, hi)
            week_after_score = bool(figure_after and _PLACING_WEEK_AFTER_SCORE_RE.match(text, figure_after.end(), hi))
            if (
                (_PLACING_TABLE_RE.search(before) and not week_after_score)
                or _PLACING_TABLE_RE.search(after)
                or _PLACING_MOVE_RE.search(before)
            ):
                continue
            # The week named straight after the placing is its scope.
            scoped = bool(_PLACING_WEEK_AFTER_RE.match(after.lstrip()))
            if not scoped and (_PLACING_MOVE_WORDS_RE.search(before) or _PLACING_MOVE_WORDS_RE.search(after)):
                continue
            # A hedge is read in the placing's own stretch; a negation, as the
            # tie check reads one, across the words before it in the clause
            # (`_claim_negated()`).
            if _PLACING_HEDGE_RE.search(_words_before(before, len(before), _PLACING_HEDGE_WORDS)) or _claim_negated(
                text, start, lo, bool(_FINISH_BEFORE_RE.search(before)),
            ):
                continue
            subject = _claim_subject(clause, start, lo)
            if subject is None or first_player < start:
                continue
            name, name_end = subject
            # Another subject is looked for in the placing's own stretch: in
            # "made no moves and submitted no claims, finishing 6th" the
            # placing is the manager's.
            window = text[max(lo, _window_start(text, start, _TIE_SUBJECT_WORDS_BEFORE)):start]
            if _TIE_OTHER_SUBJECT_RE.search(window) or _PLACING_OTHER_PERSON_RE.search(text[name_end:start]):
                continue
            relatives = list(_RELATIVE_RE.finditer(text, name_end, start))
            if relatives and (name_end == 0 or text[name_end:relatives[-1].start()].strip(" ,")):
                continue
            manager = by_name[name]
            if scoped or _PLACING_WEEK_AFTER_RE.search(after) or _PLACING_WEEK_BEFORE_RE.search(before):
                reading = "for the week"
            else:
                total = manager.get("total_points")
                if total is not None and not totals_are_scores and _states(text, total):
                    continue
                attached = _FIGURE_AFTER_PLACING_RE.match(text, end) or _FIGURE_BEFORE_PLACING_RE.search(text[:start])
                attached_score = bool(
                    attached and is_score(int(attached.group("figure")), manager)
                    and not _NOT_A_SCORE_AFTER_RE.match(text, attached.end("figure"))
                )
                # So is one that states the score's own rank on the week,
                # unless a finishing verb leads into a placing the score is
                # attached to: "finished fifth on 27, the week's lowest" is
                # still #483's shape.
                if states_week_rank(text, name) and not (attached_score and _FINISH_BEFORE_RE.search(before)):
                    continue
                if attached_score:
                    reading = "beside their gameweek score"
                elif (
                    _FINISH_BEFORE_RE.search(before)
                    and {who for *_, who in clause.named} <= {name}
                    and not (_PLACING_TABLE_RE.search(text) or _PLACING_MOVE_WORDS_RE.search(text))
                    and is_score(manager["gw_points"], manager)
                    and _states(text, manager["gw_points"])
                ):
                    reading = "beside their gameweek score"
                else:
                    continue
            word = match.group("rank").lower()
            if holds(name, word, best=bool(match.group("best"))):
                continue
            claimed = None if word in ("last", "bottom", "top") else _ORDINAL_NUMBERS.get(word) or int(word[:-2])
            problem = f'the editorial places {name} "{match.group(0)}" {reading}, but {fact(name, word, claimed)}'
            if problem not in problems:
                problems.append(problem)
    return problems


# =============================================================================
# Escapes from last place (#488)
# =============================================================================

# The verbs that say a manager got away from somewhere, in every form: one
# list, so the "missed <an escape>" reading below knows every word a cue does.
_ESCAPE_STEMS = (
    r"escap(?:e|es|ed|ing)|avoid(?:s|ed|ing)?|dodg(?:e|es|ed|ing)|evad(?:e|es|ed|ing)|surviv(?:e|es|ed|ing)"
    r"|sidestep(?:s|ped|ping)?|swerv(?:e|es|ed|ing)"
)
# The cues: those verbs, "was spared", "missed" ("missed the dreaded last
# place") and "clear of" ("a point clear of the foot of the table"), and the
# noun ("a narrow escape from the bottom"). "Missed out" says the manager went
# without something, so it is none of them.
_ESCAPE_CUE_RE = re.compile(
    rf"(?<!\w)(?:(?P<miss>miss(?:es|ed|ing)?(?!\w)(?!\s+out(?!\w)))|{_ESCAPE_STEMS}|spared|clear\s+of)(?!\w)",
    re.IGNORECASE,
)
# An escape word after "missed" says the escape is what was missed: "missed
# the bottom-of-the-week dodge", "missed avoiding it".
_ESCAPE_WORD_RE = re.compile(
    rf"(?<!\w)(?:{_ESCAPE_STEMS}|avoidance|survival|spar(?:e|es|ed|ing)|repriev(?:e|es|ed))(?!\w)", re.IGNORECASE,
)
# So does a noun for a way out that the phrase naming where from runs
# straight on into, a possessive at most between: "missed last-place
# misery's get-out clause", "missed the wooden-spoon let-off" (#499).
# Further off it describes the escape that happened ("missed the wooden
# spoon by a point, a real let-off"), so it is read nowhere else.
_ESCAPE_WAY_OUT_AFTER_RE = re.compile(
    r"(?:\s+[\w-]+['’]s)?\s+(?:get[-\s]outs?|let[-\s]offs?|lifelines?)(?!\w)", re.IGNORECASE,
)
# A base form says nothing happened ("cannot escape", "struggled to avoid",
# "Did he escape?") unless the escape is the noun after a determiner ("a
# narrow escape", "his escape") or an infinitive that says it was achieved
# ("enough to avoid", "managed to escape", "held on to avoid"). A gerund after
# a preposition is the same ("a point from escaping", "shy of avoiding", "a
# week after escaping"); one that opens a participle clause ("finished
# seventh, escaping last place") is an escape.
_ESCAPE_BASE_FORMS = frozenset({"escape", "avoid", "dodge", "evade", "survive", "sidestep", "swerve", "miss"})
_ESCAPE_NOUN_BEFORE_RE = re.compile(
    r"(?:(?<!\w)(?:a|an|the|his|her|their|its|this|that|whose)|['’]s)(?:\s+[\w-]+)?\s+$", re.IGNORECASE,
)
_ESCAPE_ACHIEVED_BEFORE_RE = re.compile(
    r"(?<!\w)(?:enough|managed|manages|managing|able|sufficient|time|(?:held|hung|clung|holding|hanging|clinging)"
    r"\s+on)\s+to\s+$",
    re.IGNORECASE,
)
_ESCAPE_GERUND_PREP_RE = re.compile(
    r"(?<!\w)(?:from|of|off|at|about|without|for|on|in|into|after|before|to|towards?|than|like|despite|upon)"
    r"(?:\s+\w+ly)?\s+$",
    re.IGNORECASE,
)
# Where the manager got away from: last place, the bottom, the wooden spoon,
# the worst week or the lowest score, the foot of the table or the basement,
# with the scope named straight after it ("on the week", "of the table") so
# the quote carries it, hyphenated or not ("the bottom-of-the-week
# dishonour"). A bare "last", "bottom", "foot" or "basement" can
# modify something else, so what follows one must end the phrase or carry it
# on ("by a point", "this week", "again"): "last week", "the bottom half",
# "the bottom corner", "a foot injury", "the last of his points", "the bottom
# of the barrel" and "second-last" are none of them.
_ESCAPE_SCOPE = r"[-\s]+(?:of|on|in|for)[-\s]+the[-\s]+(?:week|gameweek|gw|table|league|standings|pile)(?![\w-])"
_ESCAPE_TARGET_RE = re.compile(
    r"(?<![\w-])(?:rock[-\s]+)?(?:"
    r"(?P<phrase>(?:last|bottom)[-\s]+(?:place|spot|position|slot|rung)(?:[-\s]+(?:finish|fine))?|wooden[-\s]+spoon"
    rf"|(?P<superlative>(?:worst|lowest)[-\s]+(?:week|gameweek|gw|score|return|tally|total)))(?:{_ESCAPE_SCOPE})?"
    rf"(?![\w-])|(?:last|bottom|foot|basement)(?:{_ESCAPE_SCOPE}"
    r"|(?=\s*(?:$|[,.;:!?()'‘’\"“”–—]|-\s|(?:by|this|again|too|and|but|while|as|despite|though|although|thanks"
    r"|courtesy|after|when|because|so|just|only|narrowly|comfortably|once|yet)(?!\w)))))",
    re.IGNORECASE,
)
# "Of" straight after a whole phrase ranks it over something else: "the
# worst week of his FPL life", "last place of the season".
_ESCAPE_OF_AFTER_RE = re.compile(r"\s+of(?!\w)", re.IGNORECASE)
# What may sit between an escape and where it was from: the furniture of the
# phrase ("the dreaded", "the dubious honour of", "finishing in"), never a
# word that names or counts anything else. "Survived a red card to finish
# bottom" is no escape from the bottom.
_ESCAPE_LINK_WORDS = frozenset({
    "the", "a", "an", "his", "her", "their", "its", "this", "week's", "gameweek's", "weekly", "of", "from",
    "being", "finishing", "coming", "ending", "up", "in", "on", "at", "outright", "sole", "lonely", "dreaded",
    "dubious", "unwanted", "unenviable", "ignominious", "ignominy", "indignity", "shame", "honour", "honor",
    "humiliation", "embarrassment", "spectre", "specter", "prospect", "threat", "fate",
})
# The passive, where the target leads and the manager follows: "The wooden
# spoon was avoided by Gus Peel", "Last place was narrowly avoided by him".
_ESCAPE_PASSIVE_RE = re.compile(r"(?<!\w)(?:was|were|been|being|got|gets|is|are)(?:\s+\w+ly)?\s+$", re.IGNORECASE)
_ESCAPE_AGENT_RE = re.compile(r"\s+by\s+", re.IGNORECASE)
_OBJECT_PRONOUN_RE = re.compile(r"(?:him|her|them)(?!\w)", re.IGNORECASE)
# Words before an escape, in its own stretch, that say it did not happen or
# only nearly did: "would have escaped", "came close to avoiding", "no escape
# from", "had escaped" (before this week), and "missed" straight before it
# ("missed avoiding last place", "missed the escape from it"), where the
# escape is what was missed (#499). A "missed" further back is about
# something else ("missed a penalty yet escaped"). "Only just escaped" is an
# escape.
_ESCAPE_HEDGE_RE = re.compile(
    r"(?<!\w)(?:would|might|should|if|unless|nearly|almost|close\s+to|within|short\s+of|away\s+from|all\s+but"
    r"|miss(?:es|ed|ing)?(?=(?:\s+out\s+on)?(?:\s+(?:the|a|an|his|her|their|its))?\s*$)"
    r"|no|nothing|cannot|fail\w*|unable|tr(?:y|ies|ied|ying)|hop(?:e|es|ed|ing)|need\w*|bid|attempt\w*|chance"
    r"|wish\w*|want\w*|aim\w*|look\w*|seem\w*|appear\w*|thought|could(?=(?:\s+\w+)?\s+have(?!\w))"
    r"|had(?=(?:\s+\w+ly)?\s*$))(?!\w)|could['’]ve|['’]d(?!\w)",
    re.IGNORECASE,
)
# Whose last place an escape is from is read off the words that name where
# from: a word for the table makes it the table's ("clear of the foot of the
# table", "avoided last place in the league", "...overall"), and a word for
# the week the week's ("the worst week", "last place on the week"). A table
# word anywhere else is about something else: "escaped last place despite
# sitting bottom of the table", "by a single league point". With neither it is
# the week's, the escape the GW Standings line states, but a verb for holding
# a place straight before it ("stays clear of the bottom", "held on to avoid
# it"), or a copula before "clear of" ("is a point clear of the wooden
# spoon"), lets it be the table's too, so it is wrong only if both are. A span
# longer than the week ("for the first time this season") or another week
# ("last week", "in GW5", "a week after he escaped") anywhere in the stretch is
# neither, so it is left alone.
_ESCAPE_TABLE_RE = re.compile(
    r"(?<!\w)(?:table|league|standings|overall|totals?|ladder|relegation|pile|foot|basement)(?!\w)", re.IGNORECASE,
)
_ESCAPE_TABLE_AFTER_RE = re.compile(
    r"\s*(?:(?:in|of|on|at)\s+(?:the\s+)?(?:\w+\s+)?(?:table|league|standings|ladder)|overall"
    r"|on\s+(?:season\s+)?totals?)(?!\w)",
    re.IGNORECASE,
)
_ESCAPE_WEEK_RE = re.compile(r"(?<!\w)(?:week|gameweek|gw|score)(?:['’]s)?(?!\w)", re.IGNORECASE)
_ESCAPE_WEEK_AFTER_RE = re.compile(r"\s*(?:on|for|in|of)\s+the\s+(?:game)?week(?!\w)", re.IGNORECASE)
_ESCAPE_HOLD_RE = re.compile(rf"(?<!\w)(?:{_HOLDING_WORDS}|remain\w*)(?:\s+\S+){{0,2}}\s*$", re.IGNORECASE)
_ESCAPE_STATE_RE = re.compile(r"(?<!\w)(?:is|are|was|were)(?:\s+\S+){0,2}\s*$", re.IGNORECASE)
_ESCAPE_SPAN_RE = re.compile(
    r"(?<!\w)(?:season\w*|campaign|weeks|gameweeks|since|run|record|history|ever|all-time|years?|months?|career"
    r"|streak|previously|twice|thrice|(?:gw|gameweek)\s*\d+|(?:last|previous|prior|next)\s+(?:week|gameweek|gw)"
    r"|(?:week|gameweek)\s+(?:before|ago|earlier)"
    r"|(?:a|an|one|two|three|\d+)\s+(?:week|gameweek|gw)s?\s+(?:before|after|ago|earlier|later))(?!\w)",
    re.IGNORECASE,
)
_QUOTE_MARKS = ("'", "‘", "’", '"', "“", "”")


def _escape_asserted(before: str, word: str) -> bool:
    """Whether the escape word `word`, with `before` the text leading up to
    it in its stretch, says the escape happened -- see `_ESCAPE_BASE_FORMS`."""
    word = word.lower()
    if word.endswith("ing"):
        return not _ESCAPE_GERUND_PREP_RE.search(before)
    if word in _ESCAPE_BASE_FORMS:
        return bool(
            _ESCAPE_ACHIEVED_BEFORE_RE.search(before) or (word == "escape" and _ESCAPE_NOUN_BEFORE_RE.search(before))
        )
    return True


def _escape_from(
    clause: _ReadClause, cue: re.Match[str], lo: int,
) -> tuple[re.Match[str], tuple[str, int] | None, int, bool] | None:
    """Where the escape at `cue` was from, whose it is, where the text after
    it starts, and whether the manager was named before it (the active
    voice), or None when it names nowhere. The active voice ("He escaped the
    wooden spoon") takes its manager from `_claim_subject()`, carrying a
    pronoun to the first manager the clause before named, the one a reader
    takes it for; the passive ("The wooden spoon was avoided by him") from
    the name or pronoun after "by"."""
    text = clause.text
    target = _ESCAPE_TARGET_RE.search(text, cue.end())
    if target is not None and _kind_link(text[cue.end():target.start()], _ESCAPE_LINK_WORDS):
        return target, _claim_subject(clause, cue.start(), lo, opener=True), target.end(), True
    aux = _ESCAPE_PASSIVE_RE.search(text, lo, cue.start())
    agent = _ESCAPE_AGENT_RE.match(text, cue.end())
    if aux is None or agent is None:
        return None
    leading = list(_ESCAPE_TARGET_RE.finditer(text, lo, aux.start()))
    if not leading or text[leading[-1].end():aux.start()].strip():
        return None
    named = next(((who, end) for start, end, who in clause.named if start == agent.end()), None)
    if named is None and (pronoun := _OBJECT_PRONOUN_RE.match(text, agent.end())):
        # A "him" goes to the one manager the clause named before it, as in
        # `_claim_subject()`, and only with nobody named to the clause before.
        earlier = {who for start, _, who in clause.named if start < agent.end()}
        carried = next(iter(earlier)) if len(earlier) == 1 else None if earlier else clause.opener
        named = (carried, pronoun.end()) if carried is not None else None
    return leading[-1], named, named[1] if named else agent.end(), False


def check_escape_claims(summary: str, managers: Sequence[RecapManagerEntry], names: RecapNames) -> list[str]:
    """Every escape from last place the editorial gives a manager who
    finished there (#488).

    The GW Standings section names who avoided last place on the week and by
    how much (#487), and the editorial tells that narrow escape in nearly
    every run -- but it has handed it to the manager who finished last,
    through a pronoun in the sentence after their fine ("He escaped the
    wooden spoon by one cruel point over B"), and once set it against the
    foot of the table for a manager bottom of the league. Every manager's
    gameweek score and league position is known, so the claim is checkable,
    which lets the prompt's margin rule tell the story without a clause
    forcing names on every sentence that uses a gap.

    An escape is a word that says the manager got away ("escaped",
    "avoided", "dodged", "survived", "spared", "missed", "clear of") joined
    by a few words of furniture to where from: last place, the bottom, the
    wooden spoon, the worst week, the lowest score, the foot of the table.
    Whose escape it is comes from `_escape_from()`: a name before it, a
    pronoun carried from the first manager the clause before named (the
    subject a reader takes it for, even where that clause named two), or the
    name or pronoun after a passive's "by". It is the week's unless the words
    naming where from name the table, the table's too where a holding verb or
    a copula leads into it, and wrong when every reading it allows is: the
    week's when that manager's gameweek score was the week's lowest, the
    table's when they are bottom of the table. An escape that says nothing
    happened -- hedged, negated, a base form or a gerund after a preposition
    ("nearly escaped", "cannot escape", "struggled to avoid", "a point from
    escaping", "no escape from last place") -- is left alone, as is one whose
    escape is what was missed ("missed the bottom-of-the-week dodge", "missed
    avoiding last place"), one
    said over another week or a longer span ("last week", "in GW5", "for the
    first time this season"), a survival of the worst week (which reads as
    enduring it), one said of another person, subject or player ("her rival",
    "his captain"), and one in a "respectively" list. Returns one line per
    wrong escape, in the order the editorial gives them, each saying who
    finished last and who avoided it.
    """
    if len(managers) < 2:
        return []
    # Keyed as the names are read: stripped, as `_mention_aliases` keys them.
    # The scores and positions are read off every row, so a display name two
    # managers share cannot hide one of them.
    by_name = {m["manager_name"].strip(): m for m in managers}
    lowest = min(m["gw_points"] for m in managers)
    ranks = [m.get("overall_rank") or 0 for m in managers]
    # Bottom of the table is read only where every manager has a position.
    bottom_of_table = max(ranks) if all(ranks) else None
    escaped = last_place_escape(managers) or "every manager scored it, so nobody avoided last place on the week"

    def week_fact(name: str) -> str:
        level = sorted(m["manager_name"].strip() for m in managers if m["gw_points"] == lowest and m is not by_name[name])
        said = f"{name}'s {points_label(lowest)} was the lowest gameweek score"
        return said + (f", level with {join_and(level)}" if level else "")

    aliases = _manager_aliases(names)
    problems: list[str] = []
    for clause in _read_clauses(summary, names, aliases):
        text = clause.text
        if "respectively" in text.lower():
            continue
        for cue in _ESCAPE_CUE_RE.finditer(text):
            lo = _claim_stretch(text, cue.start(), cue.end())[0]
            found = _escape_from(clause, cue, lo)
            if found is None:
                continue
            target, subject, after, active = found
            hi = _claim_stretch(text, min(cue.start(), target.start()), max(after, target.end()))[1]
            before = text[lo:cue.start()]
            if (
                subject is None
                or _ESCAPE_SPAN_RE.search(text, lo, hi)
                or _ESCAPE_OF_AFTER_RE.match(text, target.end())
                or (cue.group(0).lower().startswith("surviv") and target.group("superlative"))
                or (cue.group("miss") and (
                    _ESCAPE_WORD_RE.search(text, cue.end(), hi) or _ESCAPE_WAY_OUT_AFTER_RE.match(text, target.end(), hi)
                ))
                or not _escape_asserted(before, cue.group(0))
                or _ESCAPE_HEDGE_RE.search(_words_before(before, len(before), _PLACING_HEDGE_WORDS))
                or _claim_negated(text, cue.start(), lo, True)
            ):
                continue
            name, name_end = subject
            # Another subject, person or player between the manager and the
            # escape is whose it is: "his captain dodged", "her rival
            # escaped", "Gus Peel's Salah missed the bottom corner".
            window = text[max(lo, _window_start(text, cue.start(), _TIE_SUBJECT_WORDS_BEFORE)):cue.start()]
            relatives = list(_RELATIVE_RE.finditer(text, name_end, cue.start()))
            if active and (
                _TIE_OTHER_SUBJECT_RE.search(window)
                or _PLACING_OTHER_PERSON_RE.search(text[name_end:cue.start()])
                or any(
                    name_end <= m.start() < cue.start() and m.group(0) in names.players and m.group(0) not in aliases
                    for m in clause.mentions
                )
                or (relatives and (name_end == 0 or text[name_end:relatives[-1].start()].strip(" ,")))
            ):
                continue
            # Each reading the escape allows, and what contradicts it; one read
            # both ways is wrong only when both are.
            where = text[cue.end() if active else target.start():target.end()]
            table_scoped = bool(_ESCAPE_TABLE_RE.search(where) or _ESCAPE_TABLE_AFTER_RE.match(text, after, hi))
            week_scoped = bool(_ESCAPE_WEEK_RE.search(where) or _ESCAPE_WEEK_AFTER_RE.match(text, after, hi))
            holding = bool(
                _ESCAPE_HOLD_RE.search(before)
                or (cue.group(0).lower().startswith("clear") and _ESCAPE_STATE_RE.search(before))
            )
            week = week_scoped or not table_scoped
            readings: list[str | None] = []
            if week:
                readings.append(week_fact(name) if by_name[name]["gw_points"] == lowest else None)
            if table_scoped or holding:
                bottom = bottom_of_table is not None and by_name[name].get("overall_rank") == bottom_of_table
                readings.append(f"{name} is bottom of the table in {place_label(bottom_of_table or 0)}" if bottom else None)
            if None in readings:
                continue
            said = "last place" if len(readings) > 1 else "last place on the week" if week else "the bottom of the table"
            # A quote mark the phrase opened ("the dubious honour of 'worst
            # week'") is closed in the quote.
            start, end = (cue.start(), target.end()) if active else (target.start(), after)
            if text[end:end + 1] in _QUOTE_MARKS and any(mark in text[start:target.start()] for mark in _QUOTE_MARKS):
                end += 1
            quoted = " ".join(_plain(text[start:end]).split())
            facts = ", and ".join(fact for fact in readings if fact)
            problem = f'the editorial says {name} avoided {said} ("{quoted}"), but {facts}; {escaped}'
            if problem not in problems:
                problems.append(problem)
    return problems


# =============================================================================
# The forfeit written out word for word (#503)
# =============================================================================

# What may sit between two of a forfeit's words and still be the forfeit
# written out: spacing, emphasis and the punctuation inside a phrase, never
# a sentence's end. "Pint on **video**" is the forfeit; "a pint. On video,
# ..." is two sentences. The exception is a break the forfeit itself carries
# ("Pint on video. Tag the group"), which is matched as written.
_SENTENCE_BREAKS = ".!?;:"
_PENALTY_WORD_GAP = rf"[^\w{re.escape(_SENTENCE_BREAKS)}\n]+"
_PENALTY_EDGE_GAP = rf"[^\w{re.escape(_SENTENCE_BREAKS)}\n]*"
_PENALTY_TOKEN_RE = re.compile(r"(\w+)|(\W+)")
# Fewer words than this and the forfeit is an ordinary word too: a league
# whose forfeit is "Shot" would have "a shot at the title" counted against it.
_PENALTY_MIN_WORDS = 2
# Shorter words are left in the number they were written in: taking the "s"
# off "is" or "us" leaves a different word.
_PENALTY_STEM_MIN_LETTERS = 4


def _penalty_word(word: str) -> str:
    """One word of a forfeit in either number, whichever it was configured
    in: "pint" matches "pints", and "pints" matches "pint"."""
    stems = {word}
    if len(word) >= _PENALTY_STEM_MIN_LETTERS and word[-1] in "sS" and word[-2] not in "sS":
        stems.add(word[:-1])
        if word[-2] in "eE":
            stems.add(word[:-2])
    alternatives = "|".join(re.escape(stem) for stem in sorted(stems, key=len, reverse=True))
    return f"(?:{alternatives})(?:e?s)?"


def _penalty_gap(separator: str) -> str:
    """What may stand for the text between two of a forfeit's words."""
    breaks = "".join(c for c in separator if c in _SENTENCE_BREAKS)
    if not breaks:
        return _PENALTY_WORD_GAP
    return f"{_PENALTY_EDGE_GAP}{re.escape(breaks)}{_PENALTY_EDGE_GAP}"


def _penalty_pattern(penalty: str) -> re.Pattern[str] | None:
    """The forfeit's words in order, each in either number, or None for a
    forfeit too short to tell from ordinary prose. "Pints on video" is still
    the forfeit written out; "a pint on camera" is a phrasing of it."""
    parts: list[str] = []
    words = 0
    separator = ""
    for match in _PENALTY_TOKEN_RE.finditer(penalty):
        if match.group(1) is None:
            separator += match.group(2)
            continue
        if words:
            parts.append(_penalty_gap(separator))
        parts.append(_penalty_word(match.group(1)))
        separator = ""
        words += 1
    if words < _PENALTY_MIN_WORDS:
        return None
    return re.compile(rf"(?<!\w){''.join(parts)}(?!\w)", re.IGNORECASE)


def restates_forfeit(phrasing: str, penalty: str) -> bool:
    """Whether `phrasing` writes `penalty` out word for word, which would
    make it no other way of saying it: listed as one, it would tell the
    editorial to write out the very words this check counts."""
    pattern = _penalty_pattern(penalty)
    return pattern is not None and pattern.search(phrasing) is not None


def check_penalty_repetitions(summary: str, penalties: Sequence[RecapFinePenalty]) -> list[str]:
    """Every forfeit the editorial writes out word for word more than once.

    The brief allows each once, headline included, and every other mention in
    other words. A count of one known string, like the roll call's count of
    known names: it never judges whether a reworded mention kept the
    forfeit's meaning, and a forfeit of one word is left alone, since it
    cannot be told from the same word used for something else.
    """
    problems: list[str] = []
    for entry in penalties:
        pattern = _penalty_pattern(entry["penalty"])
        if pattern is None:
            continue
        found = [match.group(0) for match in pattern.finditer(summary)]
        if len(found) < 2:
            continue
        times = "twice" if len(found) == 2 else f"{len(found)} times"
        quoted = ", ".join(f'"{text}"' for text in found)
        problems.append(
            f'the editorial writes the forfeit "{entry["penalty"]}" out word for word {times} '
            f"({quoted}), where the brief allows it once",
        )
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


def _ties(summary: str, data: LeagueRecapData, names: RecapNames) -> list[str]:
    records: dict[str, RecapSeasonRecord] = {}
    if (highest := data.get("season_highest_score")) is not None:
        records["top"] = highest
    if (lowest := data.get("season_lowest_score")) is not None:
        records["bottom"] = lowest
    return check_tie_claims(summary, data["managers"], names, season_records=records)


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
            "Standings section lists it as shared, or, for the season's highest or lowest score, "
            "where a League History season-record line says it was scored more than once."
        ),
        run=_ties,
    ),
    EditorialCheck(
        code=RECAP_WARNING_WRONG_PLACING,
        lead="The editorial gives a manager a placing on the week their gameweek score does not earn",
        retry_instruction=(
            "Take a manager's placing for the week only from the GW Standings section's GW Rank column "
            "and their place in the league only from its Pos column, and say which one a placing is; "
            "never set a league position beside a gameweek score as that score's finish."
        ),
        run=lambda summary, data, names: check_gameweek_placings(summary, data["managers"], names),
    ),
    EditorialCheck(
        code=RECAP_WARNING_ESCAPE_MISATTRIBUTION,
        lead="The editorial says a manager avoided a last place they finished in",
        retry_instruction=(
            "Say a manager avoided, escaped or dodged last place on the week only if their gameweek score "
            "was above the lowest, and in the table only if they are not bottom of it, and say which one it "
            "is; when you tell the escape the GW Standings section's lowest line states, name the manager "
            "who made it, never a pronoun that could point back at the one who finished last."
        ),
        run=lambda summary, data, names: check_escape_claims(summary, data["managers"], names),
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
    EditorialCheck(
        code=RECAP_WARNING_PENALTY_REPEATED,
        lead="The editorial writes a fine's forfeit out word for word more than once",
        retry_instruction=(
            'Write each forfeit in the exact words of its "Naming the forfeit" line at most once, '
            "headline included, and say it in other words everywhere else, using only the league's "
            "own phrasings where that line gives them, without changing what the forfeit is."
        ),
        run=lambda summary, data, names: check_penalty_repetitions(summary, data.get("fine_penalties", [])),
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
