"""Prompts for league-recap LLM summaries."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from fpl_cli.cli._league_recap_types import (
    LeagueRecapData,
    PriorSeasonsSummary,
    RecapContestedClaim,
    RecapDraftLostClaim,
    contested_draft_claims,
    draft_transaction_kind_counts,
    draft_transaction_kind_label,
    format_contested_claim,
    format_move_counts,
    recap_title,
)
from fpl_cli.services.league_history_fines import SeasonFinesTally, format_fine_breakdown
from fpl_cli.services.league_history_notes import NotesPack, NoteSurface
from fpl_cli.utils.gameweek import format_gameweek_list, is_opening_gameweek
from fpl_cli.utils.markdown import fence_flags, parse_heading, unwrap_emphasis
from fpl_cli.utils.text import ordinal_word

# =============================================================================
# SYNTHESIS PROMPT (Stage 2: League-wide editorial)
# =============================================================================

RECAP_SYNTHESIS_SYSTEM_PROMPT = """You are writing a gameweek recap newsletter for a Fantasy Premier League mini-league.

<context>
Your audience is every member of this league. They want entertainment first, information second. Write with personality - name names, call out embarrassing decisions, celebrate great picks.
</context>

<tone>
- Newsletter columnist voice: opinionated, fun, a bit cheeky
- Name specific managers when praising or roasting
- Reference specific decisions (captain picks, bench choices, and transfers or waiver moves where that data is provided)
- Use the data to tell a story, not just list stats
- Frame the recap around the season phase named in the "## League History" section: an opener (GW1) sets an early-season tone - and where a "## Prior Seasons" section is present, its returning-manager records are the opener's natural colour - a finale may reflect on the whole campaign using that section's season-spanning facts, and a midpoint or run-in gameweek should stay proportionate to where the season actually is - don't manufacture stakes the data doesn't support
- Brief - 300-400 words max. Punchy paragraphs, not walls of text
</tone>

<output_format>
- The report already carries its title above your editorial - "# Gameweek N Recap: <league name>", with the league's name exactly as configured - so never write a title or a "# " heading of your own
- Open with one headline as a "## " heading: a hook for what happened this gameweek, in your own voice. It is a headline, not a title, so it never carries the gameweek number, the word "recap" or the league's name - the title above already has all three, and the league's name is not yours to restyle
- Then the prose, in punchy paragraphs
</output_format>

<rules>
- NEVER give advice or recommendations. This is a recap, not a preview
- NEVER speculate about future gameweeks
- Stick to what happened this gameweek, with two exceptions: a historical claim (a streak, trend, or season-arc fact spanning more than this gameweek) is permitted only when it appears in the "## League History" section, stated using that section's own wording for counts, spans, and holds; and a manager's FPL seasons before this one are permitted only as the "## Prior Seasons" rule below allows. A streak, trend, or season-arc fact not listed there is forbidden to mention, however obvious it might seem. Do NOT infer history from the Awards or GW Standings sections - they are compressed and can misrepresent what actually happened over time
- Every League History entry is about ONE named manager only. NEVER combine two managers into a shared record, streak, or "club" - phrasing like "joined by X", "joins Y in that club", or "the two of them share" is forbidden unless a single League History entry explicitly names both managers together. Two managers who each had a one-off gameweek in a different week (e.g. one finished last in GW1, a different one finished last in GW2) do not form a joint record for either of them - each stays a separate, single-gameweek fact, and under the previous rule a single gameweek's worth of an event is not itself a reportable streak at all
- A claim that a manager "topped the table", "was previously top", "led before this gameweek", or "fell from the top/first place" must match the "Previous gameweek's leader" statement at the top of the GW Standings section exactly - never infer the previous leader yourself from the size of a fall, the Prev column, or anything else. If that statement names no leader, make no such claim about anyone
- A League History entry phrased as an observed count over a span (e.g. "3 in the last 11, with 8 not recorded") must be repeated that way, never simplified to "in a row" or "consecutive" unless the section itself already uses that phrasing
- A League History season-count line (e.g. "4 gameweek wins this season") is optional colour in the Season Fines mould: use one when it sharpens something that happened this gameweek ("Bob's fourth gameweek win of the season"), or - when the section carries the season's full counts, at the halfway boundary and the finale - to ground a season retrospective. Take the count verbatim, repeat its "not judged" qualifier alongside it or leave the line out, and never derive or extrapolate a season total yourself from the weekly sections
- A manager's FPL seasons before this one - how many they have played, how they finished last season, their best season, whether they are "returning", a "veteran" or a "newcomer", "entering their Nth season" - may be described only when the "## Prior Seasons" section is present and says so of that manager, with its season names, points, ranks, percentages and counts repeated verbatim. That record is FPL-wide, never this league's: write "their 11th season of FPL", never "their 11th season in this league", "a founder member" or "back for another year in this league" - the section cannot say when anyone joined this league, and neither can you. A manager it lists under "No prior FPL seasons on record" is in their first recorded season and has no past to describe; one it lists under "could not be fetched" gets no claim about their past in either direction. Never derive a trajectory the section does not state ("improving", "declining", "off the pace they set last year") - one gameweek's points against a full season's total is no comparison at all. Without a "## Prior Seasons" section, mention nobody's earlier seasons
- If fines were triggered, make them a highlight
- The "## Season Fines" section is optional colour, not a required beat. Use it when a season total sharpens what already happened this gameweek ("Bob's fourth last-place of the season"), and leave it out entirely when it adds nothing - do not open or close on the season table, do not list it out, and never pad the recap with it. A gameweek where nobody was fined rarely needs it at all
- When you do use it, take its numbers verbatim and only from that section. NEVER add up fines yourself from the "## Fines" section, which covers this gameweek alone, and never present a total the Season Fines section qualifies as incomplete as though it were final - repeat its qualification alongside it or leave the number out
- NEVER call a fine or a last-place finish a manager's "second" (or third, or any later count), and never reach for "again", "another", "twice", "back-to-back", "in a row" or "still" about one, unless a section says so about that manager in those words. The "## Fines" section places each of this gameweek's fines in its manager's season for you - if it says the fine is their first, it is their first, whatever the rest of the data seems to suggest. A manager whose Season Fines total is 1 has been fined once, this gameweek, and never before. Two managers each on 1 are two separate first offences, not a repeat for either; and the league-wide "N fine(s) recorded in total" is the league's number, never one manager's
- The biggest bench haul is always funny - lean into it
- If a manager played a chip, that's a big narrative hook. A chip that flopped deserves mockery; a chip that paid off deserves grudging respect. When referencing chip users, treat the "Chips Played" section as the source of truth — it includes an explicit total count; use that number verbatim. Do NOT count tags in the standings table. Do not name a subset as "the X wildcards" — either name all users of that chip or none.
- When referencing captain choices, treat the "## Captains" section as the source of truth. It lists every manager grouped by their intended captain pick, with an explicit total count. Use those counts verbatim. NEVER name a captain "outlier", "dissenter", or "the manager(s) who picked Y" unless they appear under that captain in the section. If you describe N managers as picking the modal captain, it must match the section's group size for that player. Do NOT infer captain choices from the awards or standings — they are compressed and miss managers whose pick was neither the best nor the worst.
- When referencing transfers, hits, or moves in and out, treat the "## Transfers" section as the source of truth. It lists every manager who made a transfer with each move and its points swing, the hit they paid, an explicit count of movers, and the managers who made none - use those counts verbatim. NEVER say a manager transferred, took a hit, or stood still unless that section says so of them, never describe a move it does not list, and where it says a manager's moves or net are unknown, supply neither. Do NOT infer transfer activity from the Awards section - it names only the single best and single worst mover, so it never tells you how many managers transferred or what anyone else did. If there is no "## Transfers" section, no "## Waivers and Free Agents" section and no transfers note, do not mention transfers, waivers, hits, or moves in and out at all - absence of transfer data means there is nothing to report, not licence to invent one.
- In draft, the "## Waivers and Free Agents" section is the source of truth for waiver claims and free-agent signings, the same way. It lists every manager who made a move with each move as it was made, its points swing and its kind tag - [waiver] or [free agent] - an explicit count of movers, and the managers who made none - use those counts verbatim. NEVER say a manager claimed, signed, dropped, or stood still unless that section says so of them, never describe a move it does not list, and never call a move tagged [free agent] a waiver claim or a move tagged [waiver] a free-agent signing - the tag is the move's kind. Draft has no transfer hits, so never mention one. Do NOT infer waiver activity from the Awards section - Waiver Genius and Waiver Disaster name only the single best and single worst mover, and Most Contested only the player the most managers claimed, so they never tell you how many managers moved, how many races there were, or what anyone else did.
- A waiver is a competition, so a manager can be busy and still have no move to show for it. The "## Waivers and Free Agents" section separates the two cases and the distinction is not optional: only the managers it lists as having made no moves AND submitted no claims sat the waiver wire out. A manager it lists as having claimed a player and lost him to a rival WAS active - they spent a claim, at the priority the line states, and were beaten to the player. Say they tried and missed, never that they did nothing, "sat it out", "stayed put", "didn't bother", "kept their powder dry" or "showed restraint", and never assign a motive - discipline, laziness, apathy - to an absence from the movers list. The same goes for a mover's "also claimed and lost" tail: it is extra activity, not a move they made.
- The "Contested players" lines at the end of that section are the only source for who else wanted a player. Each names one player, how many managers claimed him, who won him and who was beaten to him, with the priority each beaten manager gave the claim - their own ranking of the claims they submitted that week, not the league's waiver order. Use the count verbatim; never call a player contested, "in demand" or "wanted by half the league" unless a line lists him, and never say a manager wanted, chased or missed out on a player unless the line names them. Where the beaten managers on a line carry no (free agent) or (other move) tag, it was a waiver race and the winner won by standing higher in the league's waiver order - you may say that, and nothing else about anyone's waiver position, which the data does not state. A line whose beaten managers are tagged (free agent) was first-come-first-served, so say nothing about waiver order there at all. Where a line says the winner could not be identified, name nobody as having won him.
- Each "Contested players" line is one race, and its roles are fixed: the manager it says won him won him, and only the managers it says were beaten to him were beaten to him. Write every race as its own clause, naming its winner and beaten managers in exactly those roles. NEVER fold two lines into one clause - no "respectively", no "X and Y were beaten to A and B", no list of managers set against a list of players - because a manager who won one race and lost the next is then easily put in the wrong role. One beaten manager may be named once against several players ("beaten to both A and B") only when every one of those lines names them as beaten.
- NEVER claim a manager's bench outscored their team unless bench points are strictly greater than their GW points. Use the exact numbers provided.
- NEVER alter player or manager names. Use the exact spelling provided in the data.
- NEVER state a club for a player other than the club given for them in this data - the "## Player Clubs" section, or the club printed beside a name elsewhere. Players change clubs in the transfer windows and your own knowledge of who plays where goes a season out of date, so that section is the only authority. A player it does not list has no club you can state: name them alone ("Haaland's 2 points") rather than supplying one from memory.
</rules>"""


def get_recap_synthesis_prompt(
    gw: int,
    league_name: str,
    fpl_format: str,
    awards_text: str,
    standings_text: str,
    fines_text: str,
    research_summary: str | None = None,
    *,
    season_fines_text: str = "",
    captains_text: str = "",
    chips_text: str = "",
    transfers_text: str = "",
    waivers_text: str = "",
    player_clubs_text: str = "",
    league_history_text: str = "",
    prior_seasons_text: str = "",
    is_bgw: bool = False,
    is_dgw: bool = False,
    season_length: int = 38,
) -> tuple[str, str]:
    """Build the synthesis prompt for league recap. Returns (system, user)."""
    sections = [
        # Named, not written as a heading: the model used to mirror the "# "
        # line that opened this prompt, restyle it, or skip it (#349).
        f'Title: "{recap_title(gw, league_name)}" (already written above your'
        " editorial - do not repeat it)",
        f"League: {league_name}",
        f"Format: {fpl_format}",
        f"Season progress: GW{gw} of {season_length}",
    ]

    if is_bgw:
        sections.append("**This was a BLANK GAMEWEEK** - not all teams had fixtures. Factor this into your analysis of low scores.")
    if is_dgw:
        sections.append("**This was a DOUBLE GAMEWEEK** - some teams had two fixtures. Factor this into your analysis of high scores.")

    if fpl_format == "draft":
        sections.append("Note: Draft format has NO captaincy. Do not mention captains.")

    if fpl_format == "classic" and is_opening_gameweek(gw):
        sections.append(
            "**No transfers were made this gameweek** - GW1 squads are built before the "
            "deadline, so the game records no transfers and no hits for anyone. Do not "
            "mention transfers, hits, or moves in and out."
        )

    sections.extend([
        "",
        "## Awards",
        awards_text,
        "",
        "## GW Standings",
        standings_text,
    ])

    if captains_text:
        sections.extend(["", "## Captains", captains_text])

    if chips_text:
        sections.extend(["", "## Chips Played", chips_text])

    if transfers_text:
        sections.extend(["", "## Transfers", transfers_text])

    if waivers_text:
        sections.extend(["", "## Waivers and Free Agents", waivers_text])

    if player_clubs_text:
        sections.extend(["", "## Player Clubs", player_clubs_text])

    if fines_text:
        sections.extend(["", "## Fines", fines_text])

    if season_fines_text:
        sections.extend(["", "## Season Fines", season_fines_text])

    if league_history_text:
        sections.extend(["", "## League History", league_history_text])

    if prior_seasons_text:
        sections.extend(["", "## Prior Seasons", prior_seasons_text])

    if research_summary:
        sections.extend(["", "## GW Context (from research)", research_summary])

    user_prompt = "\n".join(sections)
    user_prompt += "\n\nWrite the recap newsletter for this gameweek."

    return RECAP_SYNTHESIS_SYSTEM_PROMPT, user_prompt



def get_recap_attribution_retry_prompt(user_prompt: str, problems: Sequence[str]) -> str:
    """The user prompt again, followed by what the last draft got wrong (#357).

    A second roll of the identical prompt is the same gamble that already
    failed once; naming each wrong claim beside the line it contradicts
    gives the retry the one thing the first attempt lacked.
    """
    corrections = "\n".join(f"- {problem}" for problem in problems)
    return (
        f"{user_prompt}\n\n"
        "Your previous draft put managers in the wrong role of a contested race:\n"
        f"{corrections}\n"
        "Write the recap again from scratch. Give every contested race its own clause, "
        "naming its winner and beaten managers exactly as its line does, and never fold "
        'two races into one clause with "respectively" or a shared list.'
    )

# =============================================================================
# Editorial shape
# =============================================================================

# The furniture a title is made of, as the model has written it, joined to the
# rest of a heading by a colon, a pipe, or a dash set off by whitespace (an en
# or em dash needs none). A bare hyphen with no space around it is a compound
# word ("Recap-worthy"), and whitespace alone is not a join: "Sunday League
# Falls Apart" is a headline that happens to open with the league's name, and
# stays one. At the front the token is "GW4", "Gameweek 4" or either with
# "Recap"; at the tail it must carry "Recap", since "Bob Never Learns -
# Gameweek 7" is a callback the hook is entitled to, not a title. Everything
# here is a match against a fixed shape, never an edit to the prose beside it.
_TITLE_JOIN = r"(?:\s*[:|]\s*|\s+-\s+|\s*[\u2013\u2014]\s*)"
_GAMEWEEK_TOKEN = r"(?:gameweek|gw)\s*\d+"
_LEADING_TITLE_RE = re.compile(
    rf"^{_GAMEWEEK_TOKEN}(?:\s+recap)?(?:{_TITLE_JOIN}|$)", re.IGNORECASE,
)
_TRAILING_TITLE_RE = re.compile(
    rf"(?:{_TITLE_JOIN}|^){_GAMEWEEK_TOKEN}\s+recap$", re.IGNORECASE,
)
_WORD_RE = re.compile(r"\w")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _headline(text: str, league_name: str) -> str:
    """What is left of a heading once the title's parts are removed.

    Empty when the heading was the title and nothing else -- "GW4 Recap:
    Sunday League", "Sunday League: GW4 Recap", "Gameweek 4 Recap" -- and
    the hook alone when the model wrote both: "Gameweek 4 Recap: Chaos,
    Chips, and a Captain Called Isak" gives "Chaos, Chips, and a Captain
    Called Isak". Emphasis is unwrapped on every pass, so a title the model
    bolded only in part ("GW4 Recap: **Sunday League**") still reads as the
    title once the rest is peeled.
    """
    name = re.escape(league_name.strip())
    league_res = (
        [
            re.compile(rf"^(?:the\s+)?{name}(?:{_TITLE_JOIN}|$)", re.IGNORECASE),
            re.compile(rf"(?:{_TITLE_JOIN}|^)(?:the\s+)?{name}$", re.IGNORECASE),
        ]
        if name else []
    )
    core = text.strip()
    previous = None
    while previous != core:  # "Sunday League: GW4 Recap" peels a part per pass
        previous = core
        core = unwrap_emphasis(core)
        for pattern in (_LEADING_TITLE_RE, _TRAILING_TITLE_RE, *league_res):
            core = pattern.sub("", core).strip()
    return core if _WORD_RE.search(core) else ""


def normalise_recap_editorial(summary: str, *, league_name: str) -> str:
    """Hold the editorial to the shape beneath the report's own title (#349).

    The saved report opens with `recap_title()` as its H1 and the prompt asks
    for one "## " headline and no title, but an instruction is not a
    contract: the model has mirrored the title, restyled it, replaced the
    league's name with an invention, and left the heading out altogether, so
    every recap landed under a different H1. Every heading outside a fenced
    block is held to the same rule, wherever it sits: one that only restates
    the title is dropped, the opening one is the "## " headline whatever
    level it was written at, and a later "# " is demoted to "## " -- the
    model's heading is never the document's. Prose is left as it is.
    """
    lines = summary.strip().splitlines()
    out: list[str] = []
    opening = True
    for line, fenced in zip(lines, fence_flags(lines), strict=True):
        parsed = None if fenced else parse_heading(line)
        if parsed is None:
            out.append(line)
            if line.strip():
                opening = False
            continue
        depth, text = parsed
        headline = _headline(text, league_name)
        if not headline:
            continue  # the title restated -- the report already carries it
        marks = "##" if opening else "#" * max(depth, 2)
        out.append(f"{marks} {headline}")
        opening = False
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(out)).strip()


# =============================================================================
# Editorial checks
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
_CLAUSE_BREAK_RE = re.compile(r"[.;:!?\n]")
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


def _find_mentions(text: str, names: dict[str, tuple[str, bool]]) -> list[_Mention]:
    """Every name in `text`, in order. A player in the possessive ("King's
    bonus") is not the player a race was for, so he is not read as one."""
    if not names:
        return []
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?!\w)",
    )
    mentions: list[_Mention] = []
    for match in pattern.finditer(text):
        canonical, is_manager = names[match.group(0)]
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
    manager_names: Sequence[str],
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
    any race under that name bears it out. Returns one line per wrong
    (manager, player, role), in the order the editorial makes the claims.
    """
    if not contests:
        return []
    races: dict[str, list[RecapContestedClaim]] = {}
    for contest in contests:
        races.setdefault(contest["player"], []).append(contest)
    mentions = _find_mentions(summary, _mention_aliases(manager_names, set(races)))

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
# Context formatting
# =============================================================================


def format_recap_awards_context(data: LeagueRecapData) -> str:
    """Format awards into text for the LLM prompt."""
    awards = data.get("awards", {})
    lines = []

    for key in (
        "gw_winner", "gw_loser", "biggest_bench_haul",
        "best_captain", "worst_captain",
        "transfer_genius", "transfer_disaster",
        "waiver_genius", "waiver_disaster", "most_contested",
    ):
        award = awards.get(key)
        if award:
            label = key.replace("_", " ").title()
            lines.append(f"- **{label}:** {award['detail']}")

    return "\n".join(lines) if lines else "No notable awards."


# Sort-only sentinel for a manager with no derivable league position --
# larger than any real rank, so they sort after every ranked manager.
_UNRANKED = float("inf")


def format_recap_standings_context(data: LeagueRecapData) -> str:
    """Format standings with movement for the LLM prompt.

    Leads with an explicit "previous leader" fact (issue #189) so the model
    never has to infer who topped the table last gameweek from the table's
    Prev column or from the size of someone's fall -- the editorial wrongly
    credited the manager who fell furthest with having previously led,
    when neither their Prev rank nor anyone else's not-1 Prev rank supported
    that. Stating the answer outright makes it checkable the same way the
    Captains and Chips sections' explicit totals are.

    `previous_rank` is competition ranking, not ordinal (see
    `derive_point_in_time_positions`): two or more managers level on points
    genuinely share rank 1, since nothing in the ledger records the API's
    own tie-break. All of them are named, jointly, rather than picking one
    arbitrarily and telling the model to deny the others' equally valid claim.
    """
    managers = data.get("managers", [])
    if not managers:
        return "No standings data."

    is_classic = data.get("fpl_format") == "classic"
    if any(m.get("previous_rank") is not None for m in managers):
        leaders = [m for m in managers if m.get("previous_rank") == 1]
        if len(leaders) == 1:
            leader_line = (
                f"Previous gameweek's leader (Prev rank 1): {leaders[0]['manager_name']}. This is "
                "the only manager who may be described as having previously led or topped the "
                "table -- never attribute a prior top spot to anyone else, including whoever fell "
                "furthest this gameweek."
            )
        elif leaders:
            names = ", ".join(m["manager_name"] for m in leaders)
            leader_line = (
                f"Previous gameweek's leaders (Prev rank 1, tied): {names}. These are the only "
                "managers who may be described as having previously led or topped the table -- "
                "never attribute a prior top spot to anyone else, including whoever fell furthest "
                "this gameweek, and never credit just one of them alone with sole leadership."
            )
        else:
            leader_line = "Previous gameweek's leader could not be determined -- do not name one."
    else:
        leader_line = (
            "No previous gameweek's standings exist for this league (season opener, or the first "
            "gameweek captured) -- do not describe any manager as having previously led, topped the "
            "table, or fallen from the top."
        )

    lines = [
        leader_line,
        "",
        "| Pos | Prev | Manager | GW Pts | Total |",
        "|-----|------|---------|--------|-------|",
    ]
    # Sentinel matches report.py's standings block: a manager with no
    # derivable position sorts after every ranked one, so the prompt table
    # and the rendered report agree on order in a mixed cohort.
    for m in sorted(managers, key=lambda x: x.get("overall_rank") or _UNRANKED):
        prev = m.get("previous_rank", "?")
        curr = m.get("overall_rank", "?")
        name = m["manager_name"]
        movement = ""
        if isinstance(prev, int) and isinstance(curr, int) and prev != curr:
            diff = prev - curr
            movement = f" (↑{diff})" if diff > 0 else f" (↓{abs(diff)})"
        chip = m.get("active_chip") if is_classic else None
        chip_tag = f" [{chip}]" if chip else ""
        total = m.get("total_points", "?")
        lines.append(
            f"| {curr} | {prev} | {name}{chip_tag}{movement} | {m['gw_points']} | {total} |"
        )
    return "\n".join(lines)


def _pts(points: int) -> str:
    """A points figure with its unit: "1 pt", "0 pts", "12 pts"."""
    return f"{points} pt" if points == 1 else f"{points} pts"


_CHIP_LABEL = {
    "WC": "Wildcard",
    "FH": "Free Hit",
    "BB": "Bench Boost",
    "TC": "Triple Captain",
}


def format_recap_chips_context(data: LeagueRecapData) -> str:
    """Format chip usage as an explicit roster so the narrative doesn't have to count tags.

    Empty for draft format (no chips) or when no one played a chip.
    """
    if data.get("fpl_format") != "classic":
        return ""

    managers = data.get("managers", [])
    sorted_managers = sorted(managers, key=lambda m: -m.get("gw_points", 0))
    by_chip: dict[str, list[str]] = {}
    for m in sorted_managers:
        chip = m.get("active_chip")
        if not chip:
            continue
        by_chip.setdefault(chip, []).append(f"{m['manager_name']} ({_pts(m['gw_points'])})")

    if not by_chip:
        return ""

    lines = []
    total = 0
    for code in ("WC", "FH", "BB", "TC"):
        users = by_chip.get(code)
        if not users:
            continue
        label = _CHIP_LABEL[code]
        lines.append(f"- **{label}** ({len(users)}): {', '.join(users)}")
        total += len(users)
    lines.insert(0, f"Total chips played this GW: {total}")
    return "\n".join(lines)


def format_recap_transfers_context(data: LeagueRecapData) -> str:
    """Per-manager transfer roster: every mover with each move, the hit they
    paid and the post-hit net, plus the managers who made none (issue #71).

    Mirrors the captains/chips enumerate-and-lock shape. The Awards section
    names only the single best and single worst mover, and it was the only
    transfer data the model ever saw -- so nothing stopped it inventing moves
    for the ten managers in between, or reading "only two managers made
    transfers" off a section that was never a roster. Listing every mover,
    and naming who stood still, gives each transfer claim a line to be
    checked against, the way a captain "outlier" is checked against the
    Captains section.

    The move count is the API's own `transfers_made` where the collector
    recorded it, because the captured list is best-effort: a manager whose
    transfer fetch failed still made their transfers, so they are listed as
    having moved with the moves themselves marked unknown rather than filed
    with the managers who stood still. A list that came back short is listed
    with the moves it has and no net at all: `transfer_cost` is charged for
    the whole gameweek, the moves the list is missing included, so a partial
    swing minus the whole hit is not a figure the model should repeat -- the
    line gives the swing across the captured moves before the hit and says
    the gameweek's net is unknown.

    Net is post-hit (raw swing minus `transfer_cost`), the same figure the
    two transfer awards rank on. Fully captured movers are sorted by it
    best-first; the incompletely captured follow them by name, since no net
    places them. Empty for draft (waivers, not transfers) and when nobody
    transferred -- GW1 carries its own note for that.
    """
    if data.get("fpl_format") != "classic":
        return ""

    managers = data.get("managers", [])
    if not managers:
        return ""

    captured: list[tuple[int, str, str]] = []  # (post-hit net, name, line)
    partial: list[tuple[str, str]] = []  # (name, line)
    uncaptured: list[tuple[str, str]] = []  # (name, line)
    stayed: list[str] = []
    for m in managers:
        name = m["manager_name"]
        moves = m.get("transfers") or []
        made = m.get("transfers_made")
        count = len(moves) if made is None else max(made, len(moves))
        if not count:
            stayed.append(name)
            continue

        cost = m.get("transfer_cost", 0)
        chip = m.get("active_chip")
        label = f"**{name}**" + (f" [{chip}]" if chip else "")
        plural = "transfer" if count == 1 else "transfers"
        hit = f"-{cost} hit" if cost > 0 else "no hit"

        if not moves:
            uncaptured.append((name, (
                f"- {label} ({count} {plural}, {hit}): the moves themselves were not "
                "captured, so who came in and who went out is unknown - name nobody"
            )))
            continue

        moves_text = "; ".join(
            f"{t['player_in']} ({_pts(t['player_in_points'])}) in for "
            f"{t['player_out']} ({_pts(t['player_out_points'])}), {t['net']:+d}"
            for t in moves
        )
        raw = sum(t["net"] for t in moves)
        if len(moves) < count:
            # The hit was charged for moves this list cannot see, so a net
            # built from it would be a confident figure for an unknown week.
            partial.append((name, (
                f"- {label} ({count} {plural}, {hit}; only {len(moves)} of the {count} "
                f"moves were captured, {raw:+d} across those before the hit, so the "
                f"gameweek's net is unknown): {moves_text}"
            )))
            continue

        true_net = raw - cost
        net = f"net {true_net:+d}" + (" after the hit" if cost > 0 else "")
        captured.append((true_net, name, f"- {label} ({count} {plural}, {hit}, {net}): {moves_text}"))

    if not captured and not partial and not uncaptured:
        return ""

    movers = len(captured) + len(partial) + len(uncaptured)
    lines = [f"Total managers who made transfers: {movers} of {len(managers)}"]
    captured.sort(key=lambda entry: (-entry[0], entry[1]))
    lines.extend(line for _, _, line in captured)
    for group in (partial, uncaptured):
        group.sort(key=lambda entry: entry[0])
        lines.extend(line for _, line in group)
    if stayed:
        lines.append(f"Made no transfers ({len(stayed)}): {', '.join(sorted(stayed))}")
    return "\n".join(lines)


def _format_lost_claims(claims: list[RecapDraftLostClaim]) -> str:
    """Render a manager's lost claims as "Elanga [waiver, priority 1]".

    The priority is the point of printing them: three managers spending a
    first-choice claim on the same player is the week's story, and a claim
    made at priority 5 is a different thing from one made at priority 1.
    Omitted for a free-agent pickup, which has no priority to spend.
    """
    return ", ".join(
        f"{c['player_in']} [{draft_transaction_kind_label(c['kind'])}"
        + (f", priority {c['priority']}" if c["priority"] is not None else "")
        + "]"
        for c in claims
    )


def format_recap_waivers_context(data: LeagueRecapData) -> str:
    """Per-manager waiver and free-agent roster: every draft mover with each
    move as it was made, tagged by kind, plus the managers who claimed and
    lost and the managers who did neither (issues #301, #329), then every
    player more than one manager claimed and who won him (issue #330) -- the
    draft half of `format_recap_transfers_context`.

    Same enumerate-and-lock shape, simpler mechanics. Draft moves come from
    the league-wide transactions endpoint, already filtered to this gameweek,
    so the list is complete: no `transfers_made` to cross-check, no mover
    whose moves went uncaptured, no hit and no chip.

    Movers and non-movers are not the whole cohort, which is why the outbid
    group exists. A waiver is a competition, and a manager who loses one ends
    the gameweek with no accepted move -- identical, in the moves list, to a
    manager who submitted nothing. Told only "made no moves", the editorial
    read that as inactivity and assigned a motive for it ("discipline or
    laziness") to someone who had gone in at priority 1 and been beaten to
    the player. So the three groups are stated separately and the roster
    never lets absence from the movers stand for absence of intent.

    Each move is listed raw rather than chain-contracted. A manager who
    brought B in for A and then C in for B made two moves, and the awards
    contract them to one (`_contract_draft_txn_chains`) only so their
    Best/Worst line never names a player the manager did not end the
    gameweek with -- a display compression, not a record of activity. The
    net is the same either way (an intermediate cancels algebraically), so
    it is the figure the two waiver awards rank on.

    Every move carries its kind, so the editorial can tell a waiver claim
    from a free-agent pickup instead of calling every move a waiver (#146),
    using the same labels the awards print. Empty for classic (transfers,
    not waivers) and when nobody moved.

    The contested block closes the section: every race, in the sentence the
    Most Contested award prints for the biggest, so the editorial can say
    who else wanted a player -- the outbid group says a manager lost a race,
    and this says which one and to whom. Every race reaches it, where the
    award waits for a pile-up (`MOST_CONTESTED_MIN_CLAIMANTS`): a two-way
    race is colour the editorial may use, not a headline. It can only be
    present when the section is: a race needs a lost claim, and a lost claim
    puts its manager in the outbid group or on a mover's tail.
    """
    if data.get("fpl_format") != "draft":
        return ""

    managers = data.get("managers", [])
    if not managers:
        return ""

    movers: list[tuple[int, str, str]] = []  # (net, name, line)
    outbid: list[str] = []
    stayed: list[str] = []
    for m in managers:
        name = m["manager_name"]
        moves = m.get("transactions") or []
        lost = m.get("lost_claims") or []
        if not moves:
            if lost:
                outbid.append(f"{name} (claimed {_format_lost_claims(lost)})")
            else:
                stayed.append(name)
            continue

        summary = format_move_counts(draft_transaction_kind_counts(moves))
        moves_text = "; ".join(
            f"{t['player_in']} ({_pts(t['player_in_points'])}) in for "
            f"{t['player_out']} ({_pts(t['player_out_points'])}), {t['net']:+d} "
            f"[{draft_transaction_kind_label(t['kind'])}]"
            for t in moves
        )
        net = sum(t["net"] for t in moves)
        line = f"- **{name}** ({summary}, net {net:+d}): {moves_text}"
        if lost:
            line += f" | also claimed and lost: {_format_lost_claims(lost)}"
        movers.append((net, name, line))

    if not movers and not outbid:
        return ""

    movers.sort(key=lambda entry: (-entry[0], entry[1]))
    lines = [f"Total managers who made waiver or free-agent moves: {len(movers)} of {len(managers)}"]
    lines.extend(line for _, _, line in movers)
    if outbid:
        lines.append(
            f"Claimed a player but lost him to a rival, so ended with no move "
            f"({len(outbid)}): {'; '.join(sorted(outbid))}"
        )
    if stayed:
        lines.append(
            f"Made no moves and submitted no claims ({len(stayed)}): {', '.join(sorted(stayed))}"
        )
    contests = contested_draft_claims(managers)
    if contests:
        lines.append(
            f"Contested players ({len(contests)}) - claimed by more than one manager, "
            f"so only one could have him:"
        )
        lines.extend(f"- {format_contested_claim(c)}" for c in contests)
    return "\n".join(lines)


def collect_player_clubs(data: LeagueRecapData) -> dict[str, str]:
    """Map player name -> full club name across every player in the recap data.

    Squads, transfers and lost claims carry the club resolved at collection
    time, off the player's `team_id`, so nothing is reconstructed here -- this
    only regroups them by the name the recap prose actually uses.

    That regrouping is what forces the one judgement call: the recap names
    players by name alone, and most seasons have two players sharing a
    web_name. When the data gives one name two clubs, neither can be attributed
    to a mention of it, so the name is dropped and the prompt's rules then
    forbid stating a club for it -- absent beats wrong.
    """
    resolved: dict[str, str] = {}
    ambiguous: set[str] = set()

    def record(name: str | None, club: str | None) -> None:
        if not name or not club or name in ambiguous:
            return
        seen = resolved.get(name)
        if seen is None:
            resolved[name] = club
        elif seen != club:
            del resolved[name]
            ambiguous.add(name)

    for manager in data.get("managers", []):
        for player in manager.get("squad", []):
            record(player.get("name"), player.get("team_name"))
        for move in [
            *(manager.get("transfers") or []),
            *(manager.get("transactions") or []),
            # A lost claim can name a player nobody's squad or accepted move
            # does -- the manager who wanted him still has his old player, and
            # the rival who won him may not be in this league.
            *(manager.get("lost_claims") or []),
        ]:
            record(move.get("player_in"), move.get("player_in_team_name"))
            record(move.get("player_out"), move.get("player_out_team_name"))

    return resolved


def format_recap_player_clubs_context(player_clubs: dict[str, str]) -> str:
    """Roster of every player the recap can name, with the club they play for.

    Without it the prompt carries no club at all, and the model fills the gap
    from training data that goes a season stale at every transfer window (#150)
    -- so a summer signing gets written up at the club they left. Built from the
    same squads and transfers the other sections are computed from, so anything
    the recap can name is something this section covers.
    """
    if not player_clubs:
        return ""

    lines = [
        "The club each player plays for this season. This is the only source for a"
        " player's club - do not use your own knowledge of where they play.",
    ]
    lines.extend(f"- {name}: {club}" for name, club in sorted(player_clubs.items()))
    return "\n".join(lines)


def format_recap_captains_context(
    data: LeagueRecapData, player_clubs: dict[str, str] | None = None,
) -> str:
    """Per-manager captain roster grouped by intended pick.

    Mirrors the chips section: prevents the synthesis LLM from hallucinating
    captain outliers when the modal pick crowds out the per-award detail.
    Suppressed for draft (no captaincy).

    Captains are the most-named players in a recap, so their club is printed
    inline rather than left to the Player Clubs lookup -- grounding a claim
    beats supplying a table to check it against.
    """
    if data.get("fpl_format") != "classic":
        return ""

    managers = data.get("managers", [])
    if not managers:
        return ""

    by_captain: dict[str, list[tuple[str, str]]] = {}
    for m in managers:
        captain = m.get("captain") or ""
        if not captain:
            continue
        if m.get("captain_played"):
            annotation = _pts(m["captain_points"])
        else:
            vc_name = m.get("vice_captain") or "?"
            vc_pts = m.get("vice_captain_points", 0)
            annotation = f"dnp; vice {vc_name} scored {_pts(vc_pts)}"
        by_captain.setdefault(captain, []).append((m["manager_name"], annotation))

    if not by_captain:
        return ""

    clubs = player_clubs or {}
    groups = sorted(by_captain.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines = [f"Total captains: {sum(len(v) for v in by_captain.values())}"]
    for player, entries in groups:
        entries.sort(key=lambda e: e[0])
        joined = ", ".join(f"{name} ({ann})" for name, ann in entries)
        club = clubs.get(player)
        label = f"{player} ({club})" if club else player
        lines.append(f"- **{label}** (×{len(entries)}): {joined}")
    return "\n".join(lines)


def format_recap_league_history_context(pack: NotesPack | None) -> str:
    """Season phase, streaks, and coverage -- the only permitted source of
    cross-gameweek claims (R14).

    Mirrors the captains/chips enumerate-and-lock shape: an explicit count
    ahead of the list it counts, so the synthesis LLM never has to infer
    completeness. Only prompt-surfaced streak entries are listed (KTD8) --
    a run below its condition's minimum is real but not yet notable, and is
    withheld from the model the same way it is from console and report.
    Coverage/negative-context entries are always listed, honouring the same
    "state absence explicitly" rule U9 applies to the report and console --
    but under their own "Coverage:" label, mirroring the report template's
    separate "## Streaks" heading, so the streak count's scope stays
    unambiguous and the model can't mistake a coverage caveat for one of
    the counted streak facts.

    Never returns an empty string: even a total capture failure (`pack is
    None`) says so explicitly, rather than leaving the section absent --
    an absent section invites the same invention an absent rule would
    (KTD9), so the "nothing to report" case is stated, not omitted.
    """
    if pack is None:
        return "No league history is available for this recap."

    prompt_entries = [entry for entry in pack.entries if NoteSurface.PROMPT in entry.surfaces]
    lines = [
        f"Season phase: {pack.season_phase_entry.text}",
        f"Total League History streak entries: {len(prompt_entries)}",
    ]
    for entry in prompt_entries:
        lines.append(f"- {entry.text}")
    # Season counts (issue #164): the pack surfaces these per the
    # registry's own per-condition CountSurfacePolicy -- on an ordinary
    # week, only increments that fired their condition (a round-number
    # total, an unbroken drought at a run milestone, a second-half first)
    # plus that condition's qualifying ride-alongs, so the model gets
    # "their sixth gameweek win of the season" for the win that just
    # happened rather than a season ledger to pad the recap out of; at the
    # two season milestones the whole nonzero set, the season-spanning
    # facts the retrospective framing calls for. The explicit total keeps
    # the enumerate-and-lock shape the other sections use.
    count_entries = [
        entry for entry in pack.season_count_entries if NoteSurface.PROMPT in entry.surfaces
    ]
    lines.append(f"Total season-count entries: {len(count_entries)}")
    for entry in count_entries:
        lines.append(f"- {entry.text}")
    if pack.coverage_entries:
        lines.append("")
        lines.append("Coverage:")
        for entry in pack.coverage_entries:
            lines.append(f"- {entry.text}")
    return "\n".join(lines)


def format_recap_prior_seasons_context(summary: PriorSeasonsSummary | None) -> str:
    """Each manager's FPL record before this season, FPL-wide (issue #131).

    Enumerate-and-lock, like Captains and Chips: an explicit count of the
    managers with a record, one line each carrying every figure the API
    gave, and the managers with no record and no answer named rather than
    omitted. The League History section is the league's own memory, and at
    the season opener it has none -- this is the one section that can say a
    returning manager finished in the top 4% last year, or that another is
    in their first recorded season.

    The preamble says what the tenure is *not*. Entry IDs and league IDs
    are reissued every season, so the API cannot say when anyone joined
    this league, and "their 11th season" invites exactly that reading; it
    is pinned here as well as in the system prompt because the section is
    what the model quotes from.

    Empty when nothing was fetched -- every gameweek but the league's opener,
    and every draft recap -- so the section is absent and the system
    prompt's fallback ("mention nobody's earlier seasons") applies.
    """
    if summary is None:
        return ""
    lines = [
        "Each manager's FPL record before this season, from the game's own history. "
        "Seasons and finishes are FPL-wide - played anywhere in FPL, ranked against every "
        "FPL manager - and say nothing about when anyone joined this league.",
        f"Total managers with prior FPL seasons on record: {len(summary.lines)} of "
        f"{summary.total_managers}",
    ]
    lines.extend(f"- {line}" for line in summary.lines)
    # The absence statements are the report's own sentences, verbatim, so
    # the two surfaces cannot name the same manager's gap two ways; the one
    # instruction the model needs about them follows rather than replaces.
    coverage = summary.coverage_lines
    if coverage:
        lines.extend(coverage)
        lines.append(
            "A manager named in the statements above has no past for you to describe - "
            "say nothing about their earlier seasons either way"
        )
    return "\n".join(lines)


def _season_fine_placements(
    data: LeagueRecapData, tally: SeasonFinesTally | None,
) -> list[str]:
    """One sentence per fine, placing it in that manager's season (issue #233).

    Only stated where the ledger can prove it, on two counts.

    The tally must already hold a fine against that manager in this very
    gameweek, which it does whenever the recap's own capture reached them and
    the store took the row. A store failure that cost the tally, a manager
    the capture missed, or a fine ruled outside the ledger leaves that fine
    unplaced rather than counted from a total that is missing it.

    And the ordinal itself is only an ordinal where every earlier gameweek
    actually ruled that rule against them. A gameweek nobody captured, one
    that could not be read, one recorded before fine rulings were, or one at
    the coarse tier that structurally could not rule `red-card` all leave a
    real fine unrecorded -- so a total of 1 built over a span with a hole in
    it does not make this fine their first. Where the span holds one, the
    line names it and forbids the ordinal outright instead of asserting a
    number the ledger cannot stand behind. Either way the model is left
    unable to write "second" off its own arithmetic, which is the point.

    Matched on `manager_key`, with a display-name fallback only when the name
    is unique in the tally: two managers sharing a name is the whole reason
    the key exists, and placing a fine against the wrong one of them would be
    worse than not placing it at all.
    """
    fines = data.get("fines", [])
    gameweek = data.get("gameweek")
    if tally is None or gameweek is None or not fines:
        return []

    by_key = {manager.manager_key: manager for manager in tally.managers}

    lines: list[str] = []
    seen: set[tuple[int, str]] = set()
    for fine in fines:
        key = fine.get("manager_key")
        manager = by_key.get(key) if key is not None else None
        if manager is None:
            named = [m for m in tally.managers if m.manager_name == fine["manager_name"]]
            manager = named[0] if len(named) == 1 else None
        if manager is None:
            continue
        rule_type = fine["rule_type"]
        if (manager.manager_key, rule_type) in seen:
            continue
        # Proof the tally counted *this* gameweek's ruling against them, and
        # so that its total already includes the fine being placed.
        if gameweek not in manager.fined_gameweeks:
            continue
        count = manager.counts.get(rule_type, 0)
        if count < 1:
            continue
        seen.add((manager.manager_key, rule_type))
        name = manager.manager_name
        blind = tally.unruled_gameweeks_for(manager, rule_type, before=gameweek)
        if blind:
            plural = "" if count == 1 else "s"
            lines.append(
                f"- {name}: the ledger records {count} {rule_type} fine{plural} against "
                f"{name} this season, this one included, but {format_gameweek_list(blind)} "
                f"never ruled {rule_type} against {name}, so an earlier one is not ruled "
                f"out. Do not number this fine.",
            )
        elif count == 1:
            lines.append(
                f"- {name}: this gameweek's {rule_type} fine is {name}'s first of the "
                f"season. No earlier gameweek carries a {rule_type} fine against {name}.",
            )
        else:
            lines.append(
                f"- {name}: this gameweek's {rule_type} fine is {name}'s "
                f"{ordinal_word(count)} of the season, this one included.",
            )
    return lines


def format_recap_fines_context(
    data: LeagueRecapData, tally: SeasonFinesTally | None = None,
) -> str:
    """Format this gameweek's fines for the LLM prompt, each one placed in
    the fined manager's own season (issue #233).

    The fine is a one-line fact; where it *sits* in the season is the
    arithmetic the editorial kept getting wrong. Handed a week's fine against
    one manager and a Season Fines section reading `1` against each of two
    different managers, two of three generated editorials wrote the week's
    loser up as finishing last "twice in a row" -- reading the league-wide
    total, or the other manager's 1, as a running count of theirs. The rules
    already forbade deriving that; what they could not supply is the fact
    that settles it. So the ordinal is stated here as a finished sentence,
    beside the fine it describes, rather than left as a sum over two
    sections -- the same "give the model the sentence, not the sum" call the
    standings section's explicit previous-leader line makes.
    """
    fines = data.get("fines", [])
    if not fines:
        return ""
    lines = [f"- {f['manager_name']}: {f['message']}" for f in fines]
    placements = _season_fine_placements(data, tally)
    if placements:
        lines.append("")
        lines.append(
            "Where each of these sits in that manager's season (already counted from "
            "the season table -- use these words rather than counting fines yourself):",
        )
        lines.extend(placements)
    return "\n".join(lines)


def format_recap_season_fines_context(tally: SeasonFinesTally | None) -> str:
    """Format the season-long fine tally for the LLM prompt (issue #136).

    Empty for a league that has never configured a fine rule -- the section
    is then omitted entirely rather than rendered as a header over nothing.

    Handed over every gameweek, unlike the console and report tables, which
    wait for a season milestone. The asymmetry is deliberate: a table every
    week is wallpaper, but a *sentence* every week is the kind of detail
    that makes a recap feel like it has a memory -- and the model can only
    write "Bob's fourth last-place of the season" for totals it was actually
    given, since the system prompt forbids inferring history it was not
    handed. The same prompt makes the section optional, so a week where the
    total adds nothing simply goes unmentioned.

    The coverage qualifiers are carried through verbatim, and every manager
    the ledger holds is named on one side or the other, so the model can
    reference the season table without ever having to count fines itself
    from this gameweek's section.
    """
    if tally is None or not tally.is_reportable:
        return ""

    lines = [
        f"Season fine totals, GW{tally.start_gameweek} through GW{tally.through_gameweek} "
        f"({tally.total_fines} fine(s) recorded in total):",
    ]
    fined = tally.fined_managers
    if fined:
        for manager in fined:
            # The breakdown itself is the console block's helper, so the
            # counts the model is given and the counts the user reads cannot
            # drift apart -- but the line as a whole is deliberately not the
            # console's any more. The fined gameweeks travel with the total
            # here only (issue #233): a bare "1" beside another manager's
            # bare "1" is what the editorial read as one manager's running
            # count, and naming the gameweeks leaves "second" with nowhere
            # to come from. The console shows a human the same table a
            # sentence later, and needs no such scaffolding.
            fined_in = format_gameweek_list(manager.fined_gameweeks)
            provenance = f"; fined in {fined_in}" if fined_in else ""
            lines.append(
                f"- {manager.manager_name}: {manager.total} "
                f"({format_fine_breakdown(manager)}{provenance})",
            )
    else:
        lines.append("- Nobody has been fined this season.")

    unfined = [manager.manager_name for manager in tally.managers if not manager.total]
    if unfined and fined:
        lines.append(f"Not fined so far: {', '.join(unfined)}")

    if tally.qualifiers:
        lines.append("")
        lines.append("Coverage:")
        lines.extend(f"- {line}" for line in tally.qualifiers)
    return "\n".join(lines)
