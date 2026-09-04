"""Prompts for league-recap LLM summaries."""

from __future__ import annotations

from fpl_cli.cli._league_recap_types import LeagueRecapData
from fpl_cli.services.league_history_fines import (
    ManagerFineTally,
    SeasonFinesTally,
    format_fine_breakdown,
    format_fine_ordinal,
)
from fpl_cli.services.league_history_notes import NotesPack, NoteSurface
from fpl_cli.utils.gameweek import format_gameweek_list, is_opening_gameweek

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
- Reference specific decisions (captain picks, bench choices, and transfers where transfer data is provided)
- Use the data to tell a story, not just list stats
- Frame the recap around the season phase named in the "## League History" section: an opener (GW1) sets an early-season tone, a finale may reflect on the whole campaign using that section's season-spanning facts, and a midpoint or run-in gameweek should stay proportionate to where the season actually is - don't manufacture stakes the data doesn't support
- Brief - 300-400 words max. Punchy paragraphs, not walls of text
</tone>

<rules>
- NEVER give advice or recommendations. This is a recap, not a preview
- NEVER speculate about future gameweeks
- Stick to what happened this gameweek, with one exception: a historical claim (a streak, trend, or season-arc fact spanning more than this gameweek) is permitted only when it appears in the "## League History" section, stated using that section's own wording for counts, spans, and holds. A streak, trend, or season-arc fact not listed there is forbidden to mention, however obvious it might seem. Do NOT infer history from the Awards or GW Standings sections - they are compressed and can misrepresent what actually happened over time
- Every League History entry is about ONE named manager only. NEVER combine two managers into a shared record, streak, or "club" - phrasing like "joined by X", "joins Y in that club", or "the two of them share" is forbidden unless a single League History entry explicitly names both managers together. Two managers who each had a one-off gameweek in a different week (e.g. one finished last in GW1, a different one finished last in GW2) do not form a joint record for either of them - each stays a separate, single-gameweek fact, and under the previous rule a single gameweek's worth of an event is not itself a reportable streak at all
- A claim that a manager "topped the table", "was previously top", "led before this gameweek", or "fell from the top/first place" must match the "Previous gameweek's leader" statement at the top of the GW Standings section exactly - never infer the previous leader yourself from the size of a fall, the Prev column, or anything else. If that statement names no leader, make no such claim about anyone
- A League History entry phrased as an observed count over a span (e.g. "3 in the last 11, with 8 not recorded") must be repeated that way, never simplified to "in a row" or "consecutive" unless the section itself already uses that phrasing
- A League History season-count line (e.g. "4 gameweek wins this season") is optional colour in the Season Fines mould: use one when it sharpens something that happened this gameweek ("Bob's fourth gameweek win of the season"), or - when the section carries the season's full counts, at the halfway boundary and the finale - to ground a season retrospective. Take the count verbatim, repeat its "not judged" qualifier alongside it or leave the line out, and never derive or extrapolate a season total yourself from the weekly sections
- If fines were triggered, make them a highlight
- The "## Season Fines" section is optional colour, not a required beat. Use it when a season total sharpens what already happened this gameweek ("Bob's fourth last-place of the season"), and leave it out entirely when it adds nothing - do not open or close on the season table, do not list it out, and never pad the recap with it. A gameweek where nobody was fined rarely needs it at all
- When you do use it, take its numbers verbatim and only from that section. NEVER add up fines yourself from the "## Fines" section, which covers this gameweek alone, and never present a total the Season Fines section qualifies as incomplete as though it were final - repeat its qualification alongside it or leave the number out
- NEVER describe a manager's fine, last place, or any other event as their "second" (or any higher ordinal), "another", "again", "twice", "both", "back-to-back", "two weeks running" or "in a row" unless a "## Season Fines" or "## League History" line says so about that named manager in those words, or gives them an ordinal above 1. Two things in that section are NOT a manager's running count: the league-wide "N fine(s) recorded in total", which sums every manager, and another manager's total sitting beside theirs. A manager whose own total reads 1 has been fined exactly once, this gameweek included, and an earlier fine belongs to whoever the section names against it. Where "## Season Fines" spells out this gameweek's fines in season context, that ordinal is the answer - use it verbatim rather than working one out
- The biggest bench haul is always funny - lean into it
- If a manager played a chip, that's a big narrative hook. A chip that flopped deserves mockery; a chip that paid off deserves grudging respect. When referencing chip users, treat the "Chips Played" section as the source of truth — it includes an explicit total count; use that number verbatim. Do NOT count tags in the standings table. Do not name a subset as "the X wildcards" — either name all users of that chip or none.
- When referencing captain choices, treat the "## Captains" section as the source of truth. It lists every manager grouped by their intended captain pick, with an explicit total count. Use those counts verbatim. NEVER name a captain "outlier", "dissenter", or "the manager(s) who picked Y" unless they appear under that captain in the section. If you describe N managers as picking the modal captain, it must match the section's group size for that player. Do NOT infer captain choices from the awards or standings — they are compressed and miss managers whose pick was neither the best nor the worst.
- Only reference transfers that appear explicitly in the Awards section or in a transfers note. If no transfer information is given, do not mention transfers, hits, or moves in and out at all - absence of transfer data means there is nothing to report, not licence to invent one.
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
    player_clubs_text: str = "",
    league_history_text: str = "",
    is_bgw: bool = False,
    is_dgw: bool = False,
    season_length: int = 38,
) -> tuple[str, str]:
    """Build the synthesis prompt for league recap. Returns (system, user)."""
    sections = [
        f"# Gameweek {gw} Recap: {league_name}",
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

    if player_clubs_text:
        sections.extend(["", "## Player Clubs", player_clubs_text])

    if fines_text:
        sections.extend(["", "## Fines", fines_text])

    if season_fines_text:
        sections.extend(["", "## Season Fines", season_fines_text])

    if league_history_text:
        sections.extend(["", "## League History", league_history_text])

    if research_summary:
        sections.extend(["", "## GW Context (from research)", research_summary])

    user_prompt = "\n".join(sections)
    user_prompt += "\n\nWrite the recap newsletter for this gameweek."

    return RECAP_SYNTHESIS_SYSTEM_PROMPT, user_prompt


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
        "waiver_genius", "waiver_disaster",
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
        by_chip.setdefault(chip, []).append(f"{m['manager_name']} ({m['gw_points']} pts)")

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


def collect_player_clubs(data: LeagueRecapData) -> dict[str, str]:
    """Map player name -> full club name across every player in the recap data.

    Squads and transfers carry the club resolved at collection time, off the
    player's `team_id`, so nothing is reconstructed here -- this only regroups
    them by the name the recap prose actually uses.

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
        for move in [*(manager.get("transfers") or []), *(manager.get("transactions") or [])]:
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
            annotation = f"{m['captain_points']} pts"
        else:
            vc_name = m.get("vice_captain") or "?"
            vc_pts = m.get("vice_captain_points", 0)
            annotation = f"dnp; vice {vc_name} scored {vc_pts} pts"
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


def format_recap_fines_context(data: LeagueRecapData) -> str:
    """Format fines for the LLM prompt."""
    fines = data.get("fines", [])
    if not fines:
        return ""
    return "\n".join(f"- {f['manager_name']}: {f['message']}" for f in fines)


def _this_week_fine_lines(manager: ManagerFineTally, gameweek: int) -> list[str]:
    """One manager's fines *this* gameweek, placed in their own season.

    Written as finished sentences carrying the ordinal already worked out,
    in the style `NotesPackEntry.text` uses, because the arithmetic is the
    part that goes wrong: handed "1" beside another manager's "1" and a
    league total of "2", the editorial has twice reported this week's loser
    as picking up their second fine of the season when it was their first
    and the earlier one belonged to somebody else (issue #233).

    The ordinal counts *gameweeks* the rule was ruled against them in, not
    rulings, because the claim it guards -- "their second last-place finish"
    -- is about weeks. A manager fined the same rule twice inside one
    gameweek would make the two differ, so the all-rules line states the
    ruling count separately rather than folding it in.
    """
    name = manager.manager_name
    lines: list[str] = []
    for rule_type, gameweeks in sorted(manager.fined_gameweeks_by_rule.items()):
        if gameweek not in gameweeks:
            continue
        earlier = [gw for gw in gameweeks if gw < gameweek]
        ordinal = format_fine_ordinal(len(earlier) + 1)
        line = (
            f"- {name}: this GW{gameweek} '{rule_type}' fine is their {ordinal} "
            f"'{rule_type}' fine of the season."
        )
        if earlier:
            line += f" Their earlier ones were in {format_gameweek_list(earlier)}."
        else:
            # Scoped to the rule, not to fines in general: a manager fined a
            # different rule earlier in the season has been fined before, and
            # a blanket "no earlier fine" here would be the mirror image of
            # the error this block exists to stop.
            line += (
                f" {name} has no earlier '{rule_type}' fine, so do not call it a second "
                f'\'{rule_type}\', "another", "again", "twice", "back-to-back" or "in a row" '
                f"-- an earlier '{rule_type}' fine against a different manager is that "
                f"manager's, never {name}'s."
            )
        if manager.total > manager.counts.get(rule_type, 0):
            line += (
                f" Across all rules {name} has {manager.total} fine(s) this season, in "
                f"{format_gameweek_list(manager.fined_gameweeks)}."
            )
        lines.append(line)
    return lines


def _format_this_week_in_season_context(tally: SeasonFinesTally) -> list[str]:
    """The derived block that turns the season table into sentences.

    Nothing here is new data -- it is the same tally, read for the one
    question the table cannot answer at a glance and the editorial keeps
    answering wrong: is this week's fine that manager's first, or another
    one? Empty on a gameweek where nobody was fined, which is most of them.
    """
    gameweek = tally.through_gameweek
    entries: list[str] = []
    for manager in tally.fined_managers:
        entries.extend(_this_week_fine_lines(manager, gameweek))
    if not entries:
        return []
    return [
        f"GW{gameweek}'s own fines, placed in the season (these ordinals are already worked out "
        f"-- use them verbatim and never count fines yourself):",
        *entries,
    ]


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
        f"({tally.total_fines} fine(s) recorded in total across the whole league -- this is a "
        f"league-wide sum, never any one manager's count):",
    ]
    fined = tally.fined_managers
    if fined:
        for manager in fined:
            # Same helper the console block uses, so the wording the model is
            # given and the wording the user reads cannot drift apart -- with
            # the gameweeks appended, which the console reader can already
            # see in the table beside it (issue #233).
            lines.append(
                f"- {manager.manager_name}: {manager.total} "
                f"({format_fine_breakdown(manager, with_gameweeks=True)})",
            )
    else:
        lines.append("- Nobody has been fined this season.")

    unfined = [manager.manager_name for manager in tally.managers if not manager.total]
    if unfined and fined:
        lines.append(f"Not fined so far: {', '.join(unfined)}")

    this_week = _format_this_week_in_season_context(tally)
    if this_week:
        lines.append("")
        lines.extend(this_week)

    if tally.qualifiers:
        lines.append("")
        lines.append("Coverage:")
        lines.extend(f"- {line}" for line in tally.qualifiers)
    return "\n".join(lines)
