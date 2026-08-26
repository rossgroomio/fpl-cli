"""Prompts for league-recap LLM summaries."""

from __future__ import annotations

from fpl_cli.cli._league_recap_types import LeagueRecapData
from fpl_cli.services.league_history_fines import SeasonFinesTally, format_fine_breakdown
from fpl_cli.services.league_history_notes import NotesPack, NoteSurface
from fpl_cli.utils.gameweek import is_opening_gameweek

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
- A League History entry phrased as an observed count over a span (e.g. "3 in the last 11, with 8 not recorded") must be repeated that way, never simplified to "in a row" or "consecutive" unless the section itself already uses that phrasing
- If fines were triggered, make them a highlight
- The "## Season Fines" section is optional colour, not a required beat. Use it when a season total sharpens what already happened this gameweek ("Bob's fourth last-place of the season"), and leave it out entirely when it adds nothing - do not open or close on the season table, do not list it out, and never pad the recap with it. A gameweek where nobody was fined rarely needs it at all
- When you do use it, take its numbers verbatim and only from that section. NEVER add up fines yourself from the "## Fines" section, which covers this gameweek alone, and never present a total the Season Fines section qualifies as incomplete as though it were final - repeat its qualification alongside it or leave the number out
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
    """Format standings with movement for the LLM prompt."""
    managers = data.get("managers", [])
    if not managers:
        return "No standings data."

    is_classic = data.get("fpl_format") == "classic"
    lines = ["| Pos | Prev | Manager | GW Pts | Total |", "|-----|------|---------|--------|-------|"]
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
            # Same helper the console block uses, so the wording the model is
            # given and the wording the user reads cannot drift apart.
            lines.append(
                f"- {manager.manager_name}: {manager.total} "
                f"({format_fine_breakdown(manager)})",
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
