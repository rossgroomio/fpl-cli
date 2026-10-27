"""Prompts for league-recap LLM summaries."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from fpl_cli.cli._league_recap_data import recap_manager_key
from fpl_cli.cli._league_recap_types import (
    LeagueRecapData,
    PriorSeasonsSummary,
    RecapDraftLostClaim,
    RecapManagerEntry,
    contested_draft_claims,
    draft_transaction_kind_counts,
    draft_transaction_kind_label,
    draft_transaction_kind_nets,
    format_contested_claim,
    format_move_counts,
    gameweek_rank,
    last_place_escape,
    place_label,
    points_label,
    recap_title,
    standout_blanks_heading,
    standout_players_lines,
    standout_players_note,
)
from fpl_cli.services.league_history_fines import SeasonFinesTally, format_fine_breakdown
from fpl_cli.services.league_history_notes import NotesPack, NoteSurface
from fpl_cli.utils.gameweek import format_gameweek_list, is_opening_gameweek
from fpl_cli.utils.markdown import fence_flags, parse_heading, unwrap_emphasis
from fpl_cli.utils.text import join_and, ordinal_word

# =============================================================================
# SYNTHESIS PROMPT (Stage 2: League-wide editorial)
# =============================================================================

RECAP_SYNTHESIS_SYSTEM_PROMPT = """You are writing a gameweek recap newsletter for a Fantasy Premier League mini-league.

<context>
Your audience is every member of this league. They want entertainment first, information second. Write with personality - name names, call out embarrassing decisions, celebrate great picks.
The editorial is pasted into the league's WhatsApp group and read on phones. Short paragraphs, bold only for the two or three things a reader should spot first (the week's winner, the fine, the bench haul); bold on every name or on figures turns the message into a wall of asterisks; no tables and no headings beyond the one headline asked for below: on the way into the chat headings turn into plain bold, rules are dropped and tables do not render, and nobody reads a wall of text in a group.
</context>

<tone>
- Newsletter columnist voice: opinionated, fun, a bit cheeky
- Name specific managers when praising or roasting
- Reference specific decisions (captain picks, bench choices, and transfers or waiver moves where that data is provided)
- Use the data to tell a story, not just list stats
- Frame the recap around the season phase named in the "## League History" section: an opener (GW1) sets an early-season tone - and where a "## Prior Seasons" section is present, its returning-manager records are the opener's natural colour - a finale may reflect on the whole campaign using that section's season-spanning facts, and a midpoint or run-in gameweek should stay proportionate to where the season actually is - don't manufacture stakes the data doesn't support
- Short: a column, not a report. Readers skim it on their phones between matches, so say the few things worth saying this week and stop. Punchy paragraphs, not walls of text
</tone>

<output_format>
- The report already carries its title above your editorial - "# Gameweek N Recap: <league name>", with the league's name exactly as configured - so never write a title or a "# " heading of your own
- Open with one headline as a "## " heading: a hook for what happened this gameweek, in your own voice. It is a headline, not a title, so it never carries the gameweek number, the word "recap" or the league's name - the title above already has all three, and the league's name is not yours to restyle
- Then the prose, in punchy paragraphs
- Tell the three or four best stories of the week. The sections are a fact sheet to draw from, not a checklist: leaving most managers and most sections out is normal
</output_format>

<rules>
- This is a recap, not a preview: no advice, no recommendations, and nothing about future gameweeks
- Stick to what happened this gameweek. The only facts spanning more than this gameweek you may state are the ones a section hands you, with its counts, spans and qualifiers kept exactly as it states them: streaks, season counts and the season's highest and lowest gameweek scores from the "## League History" section, a manager's FPL seasons before this one as the "## Prior Seasons" rule below allows, season fine totals from the "## Season Fines" section, where this gameweek's fine sits in a manager's season from the "## Fines" section, and who led before this gameweek from the GW Standings section's previous-leader statement. A streak, trend, total or season-arc fact none of them states is not yours to mention, however obvious it looks. Beyond that statement, the Awards and GW Standings sections describe one gameweek and are never a source of history
- Every League History entry is about one named manager only. Never combine two managers into a shared record, streak, or "club" - phrasing like "joined by X", "joins Y in that club", or "the two of them share" is forbidden unless a single League History entry explicitly names both managers together. Two managers who each had a one-off gameweek in a different week (e.g. one finished last in GW1, a different one finished last in GW2) do not form a joint record for either of them - each stays a separate, single-gameweek fact, and under the previous rule a single gameweek's worth of an event is not itself a reportable streak at all
- A claim that a manager "topped the table", "was previously top", "led before this gameweek", or "fell from the top/first place" must match the "Previous gameweek's leader" statement at the top of the GW Standings section exactly - never infer the previous leader yourself from the size of a fall, the Prev column, or anything else. If that statement names no leader, make no such claim about anyone
- "Joint", "tied", "level", "shared", "equal" and every other tie word may only be used where the data states the tie. The GW Standings section names the gameweek's highest and lowest scores, says whether each was one manager's alone, and lists every gameweek score and every league position (Pos, and Prev where any is shared) more than one manager holds, plus any season totals that are level but placed apart by the league's tie-break: a score or position it does not list as shared belonged to one manager, so it is "the lowest", never "the joint-lowest". A tie on season totals in the table's Pos column is not a tie on gameweek scores, and a tie on gameweek scores is not one in the table. Managers level on points but placed apart are "level on points", never "joint" in position. The same goes for superlatives: "the highest" or "the lowest" score of the week belongs only to the manager(s) that section names for it. A "## League History" season-record line says how many times the season's highest or lowest score was scored: one scored more than once is shared, and one scored once is that score's alone
- A placing ("finished seventh", "came last", "second-best week") is either for the week or in the league, and the sentence says which. Take a gameweek placing from the GW Standings section's GW Rank column and a league placing from its Pos column, never one from the other: the table is in league order, so the last row is the league's bottom manager, not the week's, and a Pos never sits beside a gameweek score as that score's finish
- A gap between two managers' gameweek scores ("won by N", "N points clear", "N adrift at the bottom") is quoted only as the GW Standings section states it: the highest score's gap to the next-best and the lowest score's gap to the next-lowest. Never work a gap out of the GW Pts column yourself: any other comparison of two gameweek scores keeps both scores side by side and leaves the gap unsaid. Where the highest or lowest score is shared, its holders are level with each other and the stated gap is each one's to the next score, never one holder's margin over another. The section states no gap between two managers' season totals ("N points behind the leader", "N clear at the top", "within N of top spot"), so never work one out of the Total column either: two totals compared sit side by side, and the gap between them goes unsaid
- A League History entry keeps its meaning when you use it, in your own sentence: a count stated over a span (e.g. "3 in the last 11 (GW1-GW11). Of the rest, 8 were not recorded.") stays a count over a span with its unrecorded gameweeks mentioned, never simplified to "in a row" or "consecutive" unless the section itself says so. The figures, the span and the qualifier are the facts; the sentence around them is yours
- A League History season-count line (e.g. "4 gameweek wins this season") is optional colour in the Season Fines mould: use one when it sharpens something that happened this gameweek ("Bob's fourth gameweek win of the season"), or - when the section carries the season's full counts, at the halfway boundary and the finale - to ground a season retrospective. Keep the count exact, and carry its qualifier for gameweeks that could not be judged, in your own words, or leave the line out, and never derive or extrapolate a season total yourself from the weekly sections
- A gameweek score is "the week of the season", "the season's best", "a season low" or any other best or worst of the season only when a "## League History" season-record line names it, and a manager's week is the best or worst of their own season only when that line names that week as theirs. Where the line names scores from other gameweeks and says no score this gameweek equalled it, this week's highest or lowest was not the season's, however big or small it looks. Carry the line's qualifier where it has one. No section ranks a margin, a bench haul or a pick against the rest of the season, so never call one the season's biggest, narrowest or best
- A manager's FPL seasons before this one - how many they have played, how they finished last season, their best season, whether they are "returning", a "veteran" or a "newcomer", "entering their Nth season" - may be described only when the "## Prior Seasons" section is present and says so of that manager, with its season names, points, ranks, percentages and counts kept exact. That record is FPL-wide, never this league's: write "their 11th season of FPL", never "their 11th season in this league", "a founder member" or "back for another year in this league" - the section cannot say when anyone joined this league, and neither can you. A manager it lists under "No prior FPL seasons on record" is in their first recorded season and has no past to describe; one it lists under "could not be fetched" gets no claim about their past in either direction. Never derive a trajectory the section does not state ("improving", "declining", "off the pace they set last year") - one gameweek's points against a full season's total is no comparison at all. Without a "## Prior Seasons" section, mention nobody's earlier seasons
- If fines were triggered, make them a highlight
- A fine's forfeit is the league's own rule, so it never changes in your telling: never more of it, a different drink or forfeit, or a part of it dropped, unless a line in the data says it doubles or escalates. The "## Fines" section names each forfeit under "Naming the forfeit". Write a forfeit word for word at most once in the whole editorial, headline included; every other mention says the same forfeit in other words. Where that section gives the league's own phrasings for it, every other mention in the prose that names the forfeit is one of those, varied rather than repeated, and nothing else, and a bare "the forfeit" is such a mention. A short aside that only alludes to it - what it involves, where it happens or who sees it - may be in your own words, as long as it keeps the forfeit as it is. The headline is the one place you may play on the forfeit in words of your own, as long as it stays the same forfeit: never more of it than one per fine (a single fine is never a pub crawl or a long night at the bar), and never a different act in its place, such as paying, buying or standing a round
- The "## Season Fines" section is optional colour, not a required beat. Use it when a season total sharpens what already happened this gameweek ("Bob's fourth last-place of the season"), and leave it out entirely when it adds nothing - do not open or close on the season table, do not list it out, and never pad the recap with it. A gameweek where nobody was fined rarely needs it at all
- Season fine totals come from the "## Season Fines" section exactly, never from adding up the "## Fines" section, which covers this gameweek alone, and a total that section qualifies as incomplete carries its qualification or stays out. Where this gameweek's fine sits in a manager's season - their first, their third, their "another" or "again" - is stated for you under the "## Fines" section; use that placement and no other, and never count a fine ("second", "twice", "back-to-back", "in a row", "still") beyond what it says. A bare total of 1 is one fine, this gameweek: two managers each on 1 are two first offences, not a repeat, and the league-wide total is nobody's running count
- Call a manager "the only one", "the only manager", "alone in" or "the one exception", or say "nobody else" did what they did, only where a section says so of them outright, or the group it puts them in holds that one manager, and that goes for every section, this gameweek's and the season's alike. The "## Fines" section counts the managers fined this gameweek and names the ones who were not, and the "## Season Fines" section names who holds the season's most fines and who has none: "the only one without a fine" needs one of those not-fined lists to hold that manager alone, and "the only manager on three fines" needs the most-fines line to name them alone. Never count a section's list yourself to make a manager the only one in it; where the group holds more than one manager, say they were one of the number the section gives, or leave the claim out
- The biggest bench haul is always funny - lean into it
- If a manager played a chip, that's a big narrative hook. A chip that flopped deserves mockery; a chip that paid off deserves grudging respect. When referencing chip users, treat the "Chips Played" section as the source of truth - it includes an explicit total count; use that number verbatim. Do not count tags in the standings table. Do not name a subset as "the X wildcards" - either name all users of that chip or none.
- When referencing captain choices, treat the "## Captains" section as the source of truth. It lists every manager grouped by their intended captain pick, with an explicit total count. Use those counts verbatim. Never name a captain "outlier", "dissenter", or "the manager(s) who picked Y" unless they appear under that captain in the section. If you describe N managers as picking the modal captain, it must match the section's group size for that player. Do not infer captain choices from the awards or standings - they are compressed and miss managers whose pick was neither the best nor the worst.
- When referencing transfers, hits, or moves in and out, treat the "## Transfers" section as the source of truth. It lists every manager who made a transfer with each move and its points swing, the hit they paid, an explicit count of movers, and the managers who made none - use those counts verbatim. Never say a manager transferred, took a hit, or stood still unless that section says so of them, never describe a move it does not list, and where it says a manager's moves or net are unknown, supply neither. Do not infer transfer activity from the Awards section - it names only the single best and single worst mover, so it never tells you how many managers transferred or what anyone else did. If there is no "## Transfers" section, no "## Waivers and Free Agents" section and no transfers note, do not mention transfers, waivers, hits, or moves in and out at all - absence of transfer data means there is nothing to report, not licence to invent one.
- In draft, the "## Waivers and Free Agents" section is the source of truth for waiver claims and free-agent signings, the same way. It lists every manager who made a move with each move as it was made, its points swing and its kind tag - [waiver] or [free agent] - an explicit count of movers, and the managers who made none - use those counts verbatim. Never say a manager claimed, signed, dropped, or stood still unless that section says so of them, never describe a move it does not list, and never call a move tagged [free agent] a waiver claim or a move tagged [waiver] a free-agent signing - the tag is the move's kind. Draft has no transfer hits, so never mention one. Do not infer waiver activity from the Awards section - Waiver Genius and Waiver Disaster name only the single best and single worst mover, and Most Contested only the player the most managers claimed, so they never tell you how many managers moved, how many races there were, or what anyone else did.
- A waiver is a competition, so a manager can be busy and still have no move to show for it. The "## Waivers and Free Agents" section separates the two cases and the distinction is not optional: only the managers it lists as having made no moves and submitted no claims sat the waiver wire out. A manager it lists as having claimed a player and lost him to a rival was active - they spent a claim, at the priority the line states, and were beaten to the player. Say they tried and missed, never that they did nothing, "sat it out", "stayed put", "didn't bother", "kept their powder dry" or "showed restraint", and never assign a motive - discipline, laziness, apathy - to an absence from the movers list. The same goes for a mover's "also claimed and lost" tail: it is extra activity, not a move they made.
- The "Contested players" lines at the end of that section are the only source for who else wanted a player: each names one player, how many managers claimed him, who won him and who was beaten to him, with the priority each beaten manager gave the claim (their own ranking of the claims they submitted that week, not the league's waiver order). A player is contested, "in demand" or "wanted by half the league" only when a line lists him, and a manager wanted, chased or missed out on him only when the line names them. A race needs only its count and its winner; name a beaten manager only when that is the story (the leader missed out, or one manager lost two races), never as a roll call. Each "Contested players" line is one race with fixed roles, so give every race you write its own clause, and never fold two lines into one clause ("X and Y were beaten to A and B respectively"): a manager who won one race and lost the next is then put in the wrong role. A manager was "beaten to both A and B" only when both lines name them as beaten. Where the beaten managers carry no (free agent) or (other move) tag the race was won on the league's waiver order, which is all you may say about anyone's waiver position; a race tagged (free agent) was first come first served, so say nothing about order; and where the line says the winner could not be identified, name no winner
- A "## Transfers" or "## Waivers and Free Agents" line holds two kinds of figure, and they are never interchangeable. The figures in its brackets - the move count, the hit and the net overall - describe the manager's whole gameweek; the swing after each move describes that move alone. Attach a move's swing only to that move, and the overall net only to the manager or to their moves taken together ("-6 net overall", "-6 across both transfers after the hit") - never to one named move, because on a line with more than one move or a hit it is a different number. A draft line whose moves are of more than one kind also gives each kind's net in its brackets ("waivers -1, free agents +4"), which is that kind's moves alone: say a figure is a manager's waivers' or free agents' only when it is that kind's own, so on such a line the net overall is their moves' net ("their moves netted +3 overall"), never their waivers' or their free agents'. The hit is charged for the gameweek's transfers together, never for any one of them: say a manager took a hit, never that they took it "to bring in", "for" or "on" a named player.
- Never claim a manager's bench outscored their team unless bench points are strictly greater than their GW points. Use the exact numbers provided.
- Spell every player and manager exactly as the data does: the league knows its own names, and a misspelling reads as a mistake about the person
- Never state a club for a player other than the club given for them in this data - the "## Player Clubs" section, or the club printed beside a name elsewhere. Players change clubs in the transfer windows and your own knowledge of who plays where goes a season out of date, so that section is the only authority. A player it does not list has no club you can state: name them alone ("Haaland's 2 points") rather than supplying one from memory.
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
    standout_players_text: str = "",
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

    if standout_players_text:
        sections.extend(["", "## Standout Players", standout_players_text])

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


# The table is in league order, so its last row is the league's bottom
# manager, not the week's: the editorial read Pos 8 beside a GW Pts of 31 as
# "finished eighth on 31" for a manager seventh on the week (#483). Naming
# each placing's column lets "Nth for the week" be read rather than counted.
_STANDINGS_COLUMNS_NOTE = (
    "Columns: Pos is the league position after this gameweek and Prev the league position "
    "before it; GW Rank is the placing on this gameweek's points alone (GW Pts), highest first, "
    "shared where scores tie. The rows are in league order, not gameweek order, so a gameweek "
    "placing is read from GW Rank, never from Pos."
)

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
        *_standings_tie_lines(managers),
        _STANDINGS_COLUMNS_NOTE,
        "",
        # No line states a gap between two totals, and the margin rule says
        # so: the editorial subtracted one itself and put third place "50
        # points behind" a leader 55 clear (#493).
        "| Pos | Prev | Manager | GW Pts | GW Rank | Total |",
        "|-----|------|---------|--------|---------|-------|",
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
            f"| {curr} | {prev} | {name}{chip_tag}{movement} | {m['gw_points']} "
            f"| {gameweek_rank(m, managers)} | {total} |"
        )
    return "\n".join(lines)


def _standings_tie_lines(managers: Sequence[RecapManagerEntry]) -> list[str]:
    """Which gameweek scores and league positions are shared, said outright (#359).

    The table holds both, and only the Pos column ever repeats a value the
    eye can see: the editorial carried a three-way tie on season totals onto
    a gameweek score nobody shared and called it "the joint-lowest score of
    the week". So the extremes are named with whether each was one manager's
    alone, and every shared score and position is listed, which gives the
    tie rule a line to hold "joint" to -- the same enumerate-and-lock shape
    as the previous-leader statement above them. Each extreme also states its
    gap to the nearest score, which the margin rule quotes instead of
    subtracting (#484).
    """
    by_points: dict[int, list[str]] = {}
    for m in sorted(managers, key=lambda m: m["manager_name"]):
        by_points.setdefault(m["gw_points"], []).append(m["manager_name"])

    def scored(points: int) -> str:
        return ", ".join(by_points[points])

    # The table is in league order, so the week's runner-up is rarely the
    # winner's neighbour: the editorial found it by scanning GW Pts and wrote
    # "won by four clear points" for a three-point win (#484). Each extreme
    # carries its gap to the nearest score, stated rather than subtracted.
    def extreme(highest: bool) -> str:
        label, gap = ("Highest", "clear of the next-best score") if highest else ("Lowest", "behind the next-lowest score")
        points, *rest = sorted(by_points, reverse=highest)
        shared = len(by_points[points]) > 1
        line = f"{label} gameweek score: {points_label(points)}, "
        line += f"shared by {scored(points)}" if shared else f"{scored(points)} alone"
        if not rest:
            return line
        # Holders level with each other have no margin between them, so a
        # shared extreme's gap is said to be each one's, never one's over another.
        each = "level with each other, each " if shared else ""
        nearest = rest[0]
        margin = points_label(abs(points - nearest))
        line = f"{line} - {each}{margin} {gap}, {points_label(nearest)} ({scored(nearest)})"
        if highest:
            return line
        # Who escaped is the story the editorial tells with this gap, and it
        # once gave the escape to the manager who finished last ("He escaped
        # the bottom spot") and once called it "clear of the foot of the
        # table" for a manager bottom of the league (#487). So it is stated,
        # by name and as the week's, in the one wording `check_escape_claims()`
        # also hands the retry (#488).
        return f"{line}, so {last_place_escape(managers)}"

    lines = [extreme(highest=True), extreme(highest=False)]
    shared_scores = [
        f"{points_label(points)} ({', '.join(names)})"
        for points, names in sorted(by_points.items(), reverse=True) if len(names) > 1
    ]
    lines.append(
        f"Gameweek scores shared by more than one manager ({len(shared_scores)}): "
        f"{'; '.join(shared_scores)} - every other gameweek score was one manager's alone"
        if shared_scores else
        "Gameweek scores shared by more than one manager: none - every gameweek score was one manager's alone"
    )

    by_name = sorted(managers, key=lambda m: m["manager_name"])
    positions = _shared_positions(by_name, "overall_rank")
    if positions is None:
        return lines
    lines.append(
        f"League positions shared in the Pos column ({len(positions)}): {'; '.join(positions)} - every "
        "other position is one manager's alone, and a shared position is a tie on season totals, "
        "not on gameweek scores"
        if positions else
        "League positions shared in the Pos column: none - every position is one manager's alone"
    )
    # Only where they exist: each is a tie the table shows that the Pos line
    # above does not, and saying "none" twice more is noise.
    if previous := _shared_positions(by_name, "previous_rank"):
        lines.append(f"Previous positions shared in the Prev column ({len(previous)}): {'; '.join(previous)}")
    # A head-to-head draft league scores league points, where ties are
    # common and the league breaks them on points scored: the Total column
    # shows the tie the Pos column has already split.
    by_total: dict[int, list[RecapManagerEntry]] = {}
    for m in by_name:
        if "total_points" in m and m.get("overall_rank"):
            by_total.setdefault(m["total_points"], []).append(m)
    split = []
    for total, level in sorted(by_total.items(), reverse=True):
        if len(level) > 1 and len({m.get("overall_rank") for m in level}) > 1:
            placed = ", ".join(f"{m['manager_name']} {place_label(m.get('overall_rank') or 0)}" for m in level)
            split.append(f"{total} ({placed})")
    if split:
        lines.append(
            f"Level on season total but placed apart by the league's tie-break ({len(split)}): "
            f"{'; '.join(split)} - they are level on points, not joint in position"
        )
    return lines


def _shared_positions(managers: Sequence[RecapManagerEntry], column: str) -> list[str] | None:
    """Every position in `column` ("overall_rank", "previous_rank") more than
    one manager holds, as "7th (A, B, all on 340)"; None when nobody has one."""
    by_rank: dict[int, list[RecapManagerEntry]] = {}
    for m in managers:
        if rank := m.get(column):
            by_rank.setdefault(rank, []).append(m)
    if not by_rank:
        return None
    shared = []
    for rank, holders in sorted(by_rank.items()):
        if len(holders) < 2:
            continue
        totals = {m.get("total_points") for m in holders}
        on = f", all on {totals.pop()}" if column == "overall_rank" and len(totals) == 1 and None not in totals else ""
        shared.append(f"{place_label(rank)} ({', '.join(m['manager_name'] for m in holders)}{on})")
    return shared


def format_recap_standout_players_context(data: LeagueRecapData) -> str:
    """The week's top scorers and costliest blanks, each with the managers
    he scored for, as sentences (issue #455).

    The editorial had no player points to work with beyond the bench haul
    and the swing on each move, so whatever it said about a manager's week
    it said with the waiver race, the bench and the fine, and read like a
    ledger however the rules were worded. Asking for star players without
    the data would have invited invented numbers. This hands it a few
    players worth writing about, each tied to the managers he counted for,
    so it can name a player who scored or blanked for a named manager and
    keep the figure right.

    The section owns its facts the way every other one does, so it carries
    no instructions: one line says what the figures are, and every player
    line is the sentence the report prints, written for a reader because the
    model lifts what it is given (audit finding 20). Empty when the data
    carries no section, or one with nobody in it.
    """
    standouts = data.get("standout_players")
    if not standouts:
        return ""
    scorers, blanks = standout_players_lines(standouts)
    if not scorers and not blanks:
        return ""
    fpl_format = data.get("fpl_format", "classic")
    lines = [standout_players_note(fpl_format, standouts)]
    if scorers:
        lines.append("Top scorers:")
        lines.extend(f"- {line}" for line in scorers)
    if blanks:
        lines.append(f"{standout_blanks_heading(standouts)}:")
        lines.extend(f"- {line}" for line in blanks)
    return "\n".join(lines)


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
        by_chip.setdefault(chip, []).append(f"{m['manager_name']} ({points_label(m['gw_points'])})")

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
            f"{t['player_in']} ({points_label(t['player_in_points'])}) in for "
            f"{t['player_out']} ({points_label(t['player_out_points'])}), {t['net']:+d}"
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
        # "Overall" because the bracket is the manager's gameweek, never one
        # of the moves after it, whose own swing is a different number (#359).
        net = f"net {true_net:+d} overall" + (" after the hit" if cost > 0 else "")
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
    using the same labels the awards print. A manager whose moves mix kinds
    also gets each kind's net beside the net overall ("waivers -1, free
    agents +4, net +3 overall"): given only the combined figure, the
    editorial said it of the waivers (#390). Empty for classic (transfers,
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
            f"{t['player_in']} ({points_label(t['player_in_points'])}) in for "
            f"{t['player_out']} ({points_label(t['player_out_points'])}), {t['net']:+d} "
            f"[{draft_transaction_kind_label(t['kind'])}]"
            for t in moves
        )
        net = sum(t["net"] for t in moves)
        # A manager whose moves mix kinds gets each kind's net too, or the
        # editorial has only the combined figure to say of their waivers and
        # pins it there ("his waiver moves netted +3 overall", #390).
        kind_nets = draft_transaction_kind_nets(moves)
        by_kind = (
            "; " + ", ".join(f"{label}s {kind_net:+d}" for label, kind_net in kind_nets)
            if len(kind_nets) > 1 else ""
        )
        line = f"- **{name}** ({summary}{by_kind}, net {net:+d} overall): {moves_text}"
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
            annotation = points_label(m["captain_points"])
        else:
            vc_name = m.get("vice_captain") or "?"
            vc_pts = m.get("vice_captain_points", 0)
            annotation = f"dnp; vice {vc_name} scored {points_label(vc_pts)}"
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
    """Season phase, streaks, season counts and records, and coverage -- the
    only permitted source of cross-gameweek claims (R14).

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
        lines.append(f"- {entry.text_for(NoteSurface.PROMPT)}")
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
        lines.append(f"- {entry.text_for(NoteSurface.PROMPT)}")
    # Season records (issue #494): the season's highest and lowest gameweek
    # score, the facts "the week of the season" reaches for, counted like the
    # blocks above so a total of 0 on the league's first gameweek is a stated
    # absence the system prompt's season-record rule can be held to.
    record_entries = [
        entry for entry in pack.season_record_entries if NoteSurface.PROMPT in entry.surfaces
    ]
    lines.append(f"Total season-record entries: {len(record_entries)}")
    lines.extend(f"- {entry.text_for(NoteSurface.PROMPT)}" for entry in record_entries)
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

    The count of managers fined and the names of those who were not are
    stated the same way (#502), and on a week the rules were ruled on and
    nobody was caught the section is that statement alone.
    """
    fines = data.get("fines", [])
    if not fines:
        # A week the rules were ruled on and nobody was caught is a fact of
        # its own, stated so "the only one" has a count to be held to (#502).
        if not data.get("fine_rules_evaluated"):
            return ""
        return "\n".join(_fines_roll_call(data))
    roll_call = _fines_roll_call(data)
    lines = roll_call[:1]
    lines.extend(f"- {f['manager_name']}: {f['message']}" for f in fines)
    lines.extend(roll_call[1:])
    placements = _season_fine_placements(data, tally)
    if placements:
        lines.append("")
        lines.append(
            "Where each of these sits in that manager's season (already counted from "
            "the season table -- use these words rather than counting fines yourself):",
        )
        lines.extend(placements)
    penalties = _fine_penalty_lines(data)
    if penalties:
        lines.append("")
        lines.append("Naming the forfeit:")
        lines.extend(penalties)
    return "\n".join(lines)


def _fines_roll_call(data: LeagueRecapData) -> list[str]:
    """How many managers this gameweek's fines reached, then who they did
    not (#502): the count first, the rest after the fines themselves.

    The Transfers and Waivers sections' enumerate-and-lock shape. Given only
    the fined, the model worked out the rest itself: with 11 of 19 fined it
    miscounted, and called one of the 8 left over "the only one in the league"
    without a forfeit. So the count and the names are handed over finished.

    A manager whose own evaluation raised was never ruled on, so they are
    named apart, neither fined nor cleared, rather than listed as clean.
    Empty when there are no managers to count.
    """
    managers = data.get("managers", [])
    if not managers:
        return []
    fines = data.get("fines", [])
    fined_keys = {f["manager_key"] for f in fines if "manager_key" in f}
    # A hand-built ruling carries no key, which leaves the name to match on.
    fined_names = {f["manager_name"] for f in fines if "manager_key" not in f}
    ruled = data.get("fines_ruled_manager_keys")
    ruled_keys = None if ruled is None else set(ruled)

    fined: list[str] = []
    clear: list[str] = []
    unruled: list[str] = []
    for m in managers:
        key = recap_manager_key(m)
        name = m["manager_name"]
        if key in fined_keys or name in fined_names:
            fined.append(name)
        elif ruled_keys is not None and key not in ruled_keys:
            unruled.append(name)
        else:
            clear.append(name)

    read = len(managers)
    scope = _unread_members_line(data, read)
    lines: list[str] = []
    if fined:
        of = f"{read}" if scope is None else f"the {read} this recap read"
        lines.append(f"Total managers fined this gameweek: {len(fined)} of {of}")
        if clear:
            lines.append(f"Not fined this gameweek ({len(clear)}): {', '.join(sorted(clear))}")
    elif unruled:
        lines.append(f"No manager ruled on was fined this gameweek (0 of {len(clear)})")
    elif scope is None:
        lines.append(f"Nobody was fined this gameweek (0 of {read} managers)")
    else:
        lines.append(f"Nobody this recap read was fined this gameweek (0 of {read})")
    if unruled:
        lines.append(
            f"Not ruled on this gameweek, so neither fined nor cleared ({len(unruled)}): "
            f"{', '.join(sorted(unruled))}",
        )
    if scope is not None:
        lines.append(scope)
    return lines


def _unread_members_line(data: LeagueRecapData, read: int) -> str | None:
    """What the fines count leaves out when the recap did not read the whole
    league, or None when it did (#502).

    Classic standings come one page of 50 at a time and only the first is
    read, and a member whose picks never came back is in the table but not
    in `managers`. Either way nobody past the managers read was ruled on, so
    "11 of 50" must not pass for the league and "nobody was fined" must not
    acquit members it never looked at. The same test the Standout Players
    section uses before it says "all" or "everyone but".
    """
    cohort = data.get("standings_cohort")
    truncated = data.get("standings_truncated", False)
    if not truncated and (cohort is None or read >= len(cohort)):
        return None
    size = data.get("league_size") or (None if truncated or cohort is None else len(cohort))
    if size is not None and size > read:
        return (
            f"This recap read {read} of the league's {size} members, so these counts cover "
            f"those {read} alone and say nothing of the other {size - read}"
        )
    return (
        f"This recap read only {read} of the league's members, so these counts cover "
        f"those {read} alone and say nothing of the rest"
    )


def _fine_penalty_lines(data: LeagueRecapData) -> list[str]:
    """One line per forfeit this gameweek's fines carry: the words the
    editorial may write out once, and the league's own others (#503).

    The forfeit is printed on every fine above, so by GW8 one league's
    editorials were writing it out two and three times a week as filler. Said
    once here, beside the league's phrasings where it set any, so the model
    varies the wording from a list the league chose rather than one it makes
    up -- and a forfeit nobody gave phrasings for is still named, so the
    once-only rule has a string to hold to.

    "No others" names its loopholes because the A/B round found them: with
    phrasings set, 11 of 20 editorials still reached outside the list, in
    headline puns, asides about the camera or the group chat, and a bare
    "the forfeit". A bare "the forfeit" counts as a mention. Short asides
    about the camera or a glass held at 5 in 20 across two rounds of wording,
    and none changed the forfeit, so the league accepts them in the model's
    own words. The headline alone keeps its wordplay, which the league wants
    there -- though a second round saw that wordplay grow the forfeit ("a
    pub crawl") or swap it ("pay the bar tab"), so the line says it may do
    neither.

    Both are quoted as JSON strings, the phrasings as a JSON list: they are
    the league's free text, and a quote or a semicolon inside one must not
    read as the end of it when "no others" depends on the list being exact.
    """
    lines: list[str] = []
    for entry in data.get("fine_penalties", []):
        penalty = json.dumps(entry["penalty"], ensure_ascii=False)
        line = f"- {penalty}: write these exact words at most once in the editorial"
        if entry["phrasings"]:
            phrasings = json.dumps(entry["phrasings"], ensure_ascii=False)
            line += (
                f". Every other mention in the prose that names it uses one of this league's "
                f'own phrasings for it, varied rather than repeated, and no others, and a bare '
                f'"the forfeit" counts; a short aside alluding to it may be in your own words, '
                f"and only the headline may play on it in words of its own, never into more "
                f"of it or a different act: {phrasings}"
            )
        else:
            line += ". Every other mention says the same forfeit in your own words"
        lines.append(line)
    return lines


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
            # drift apart. The fined gameweeks are deliberately left off:
            # the editorial copied the list whole ("fined in GW1, GW3 and
            # GW6"), and by the run-in it would run to a dozen gameweeks.
            # Issue #233's "second fine" is held off instead by the Fines
            # section, which places each of this gameweek's fines in its
            # manager's season, and by the system prompt's rule that a bare
            # total of 1 is one fine.
            lines.append(
                f"- {manager.manager_name}: {manager.total} "
                f"({format_fine_breakdown(manager)})",
            )
        # The season's top total and every manager on it, so "the only
        # manager with three fines" is read off a line rather than a scan of
        # the totals above: with two managers level on 3, 2 of 10 drafts in
        # the A/B for #507 still gave the 3 to one of them alone (#502).
        most = max(manager.total for manager in fined)
        holders = [manager.manager_name for manager in fined if manager.total == most]
        held = f"{holders[0]} alone" if len(holders) == 1 else (
            f"shared by {len(holders)} managers, {join_and(holders)}"
        )
        lines.append(f"Most fines this season: {most}, {held}")
    else:
        lines.append("- Nobody has been fined this season.")

    unfined = [manager.manager_name for manager in tally.managers if not manager.total]
    if unfined and fined:
        lines.append(f"Not fined so far ({len(unfined)}): {', '.join(unfined)}")

    if tally.qualifiers:
        lines.append("")
        lines.append("Coverage:")
        lines.extend(f"- {line}" for line in tally.qualifiers)
    return "\n".join(lines)
