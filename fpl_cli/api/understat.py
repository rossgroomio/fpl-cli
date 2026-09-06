"""Understat client for fetching xG and other underlying statistics."""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import date
from functools import lru_cache
from typing import Any

import httpx

from fpl_cli.season import get_season_year, season_label, understat_season
from fpl_cli.utils.text import strip_diacritics

logger = logging.getLogger(__name__)

BASE_URL = "https://understat.com"

# Map FPL team names to Understat team names (the current season's 20 clubs)
TEAM_NAME_MAP = {
    "Arsenal": "Arsenal",
    "Aston Villa": "Aston Villa",
    "Bournemouth": "Bournemouth",
    "Brentford": "Brentford",
    "Brighton": "Brighton",
    "Chelsea": "Chelsea",
    "Coventry City": "Coventry",
    "Crystal Palace": "Crystal Palace",
    "Everton": "Everton",
    "Fulham": "Fulham",
    "Hull City": "Hull",
    "Ipswich Town": "Ipswich",
    "Leeds": "Leeds",
    "Liverpool": "Liverpool",
    "Man City": "Manchester City",
    "Man Utd": "Manchester United",
    "Newcastle": "Newcastle United",
    "Nott'm Forest": "Nottingham Forest",
    "Spurs": "Tottenham",
    "Sunderland": "Sunderland",
}

# Characters that stand in for an apostrophe or a hyphen inside a name and so
# have to separate its words rather than vanish. FPL serves the ASCII pair
# (`O'Shea` is U+0027); the rest are what an editorial pipeline serves in their
# place, and decoding an entity is exactly how one arrives -- `&rsquo;` and
# `&#8217;` both decode to U+2019, not U+0027 (#263 review).
#
# Deleting one instead of separating on it is not a smaller failure than the
# entity was: `Dara O’Shea` collapses to the single word `oshea`, which fails
# the exact tier, fails all-words (FPL's `o` is not among its words) and fails
# prefix (nothing starts with `shea`) -- the same silent no-match, for the same
# player. `strip_diacritics` cannot help: U+2019 has no decomposition and is
# not a combining mark, so it reaches the punctuation rule intact.
_NAME_SEPARATORS = ".-'\u2019\u2018\u02bc\u00b4`"
_NAME_SEPARATOR_RE = re.compile(f"[{re.escape(_NAME_SEPARATORS)}]")

# Map Understat position tokens to FPL positions
POSITION_MAP = {
    "F": "FWD",
    "S": "FWD",
    "M": "MID",
    "D": "DEF",
    "GK": "GK",
}


def _season_year(season: str) -> int:
    """The start year a season identifier names: ``"2025"`` -> 2025.

    Understat identifies a season by its start year alone, so the hyphenated
    label the rest of the tool uses (``"2025-26"``) is a caller error here
    rather than an alternative spelling, and is refused before any request is
    made instead of surfacing later as a 404-shaped "no data".
    """
    if not (season.isdigit() and len(season) == 4):
        raise ValueError(f"Understat seasons are start years such as '2025', not {season!r}")
    return int(season)


def _match_season_year(match: dict[str, Any]) -> int | None:
    """Season start year a team-page match belongs to, from its kickoff date.

    Every ``dates`` entry Understat serves carries a ``datetime`` such as
    ``"2026-08-22 14:00:00"``; the July cutover in `get_season_year` puts an
    August-to-May season on one start year. None when the entry carries no
    parseable date.

    The cutover is the fixed July one whose limit `season_partition`
    documents (#91): a season that overran into July, as 2019-20 did, would
    have its July matches classified into the following season and dropped
    from the club's record here. That costs a handful of matches at most,
    well inside the full-season bar the prior applies to what is left
    (`team_ratings_prior.PRIOR_MIN_GAMES_PER_VENUE`).
    """
    raw = match.get("datetime")
    if not isinstance(raw, str):
        return None
    try:
        kickoff = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return get_season_year(kickoff)


def matches_in_season(matches: list[dict[str, Any]], season: str) -> list[dict[str, Any]]:
    """Keep the matches that belong to ``season`` (a start year, e.g. ``"2025"``).

    Understat never 404s a season a club has no record of: ``getTeamData``
    answers with that club's most recent season instead. For a club just
    promoted that is the season now in progress, so a request for its
    *previous* season comes back as this season's fixture list, and any
    results in it read as last season's evidence -- which is how a promoted
    side's first home match became a top-tier Premier League defensive prior
    (#235). The kickoff date is the one field that says which season a match
    is from, so it decides.

    The dated matches decide. When none of them fall in the season asked for,
    nothing does: an undated entry in a payload that demonstrably describes
    another season is that season's too, so it cannot be the one match that
    keeps a substituted squad alive. An undated entry is kept only when every
    dated one agrees with the request, or when no entry carries a date at all
    -- the field is Understat's own and every payload seen carries it, so a
    wholly undated payload is shape drift, where degrading to the previous
    behaviour beats silently emptying every club's record at once.

    Raises ValueError for a ``season`` that is not a start year.
    """
    year = _season_year(season)
    classified = [(m, _match_season_year(m)) for m in matches]
    seasons = {y for _, y in classified if y is not None}
    if not seasons:
        return list(matches)
    if year not in seasons:
        return []
    keep_undated = seasons == {year}
    return [m for m, y in classified if y == year or (y is None and keep_undated)]


def decode_entities(value: Any) -> Any:
    """Undo Understat's HTML escaping throughout a freshly decoded payload.

    Understat serves its page data as JSON escaped for the HTML it is embedded
    in rather than for JSON, so an apostrophe arrives as ``&#039;`` and
    ``Dara O'Shea`` reads as ``Dara O&#039;Shea`` (#263). Nothing downstream
    expected that: the name is displayed verbatim, and `_normalise` laundered
    the entity into the digit token ``o039shea``, which can never match FPL's
    plain ``O'Shea`` -- so every apostrophe name silently lost npxG, xGChain
    and the quality and value scores built on them.

    Decoded here, at the one boundary a payload enters through, rather than
    field by field wherever a name is read: the escaping is not confined to one
    key (``player_name`` and ``team_title`` both carry it, and per-match rows
    carry club names of their own), and a per-field fix leaves the next
    escaped field to be found the same way this one was. Walking the structure
    keeps that promise for keys nobody has looked at yet.

    Unescaping is only safe *after* JSON parsing, never on the response text:
    a ``&quot;`` in the raw body would decode into a quote inside a JSON
    string and break the parse.
    """
    if isinstance(value, str):
        return html.unescape(value)
    if isinstance(value, dict):
        # Keys are Understat's field names, not served text -- left alone.
        return {key: decode_entities(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_entities(item) for item in value]
    return value


class UnderstatClient:
    """Client for fetching data from Understat.

    Understat provides detailed xG (expected goals) and xA (expected assists)
    data for players and teams in the top 5 European leagues.
    """

    def __init__(self, timeout: float = 30.0, season_year: int | None = None):
        """Initialize the Understat client.

        Args:
            timeout: Request timeout in seconds.
            season_year: Season start year (e.g. 2025 for 2025/26).
                Defaults to the current season derived from today's date.
        """
        self.timeout = timeout
        self.season_year = season_year if season_year is not None else get_season_year()
        self._league_cache: dict[str, list[dict[str, Any]]] = {}  # season -> players
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _get_api_json(self, endpoint: str, referer: str) -> Any:
        """Fetch JSON from Understat's XHR API.

        Args:
            endpoint: API endpoint path (e.g. "getLeagueData/EPL/2024").
            referer: Referer URL for the request.

        Returns:
            Parsed JSON response, with Understat's HTML escaping undone.
        """
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Referer": f"{BASE_URL}/{referer}",
        }
        response = await self._http.get(f"/{endpoint}", headers=headers)
        response.raise_for_status()
        return decode_entities(response.json())

    async def _get_html(self, endpoint: str) -> str:
        """Fetch HTML from Understat.

        Args:
            endpoint: URL endpoint.

        Returns:
            HTML content.
        """
        response = await self._http.get(f"/{endpoint}")
        response.raise_for_status()
        return response.text

    def _extract_json_data(self, raw_html: str, var_name: str) -> Any:
        """Extract JSON data embedded in HTML.

        Understat embeds data as JavaScript variables in the page.

        Args:
            raw_html: HTML content. Named around the `html` module this
                function's sibling now decodes with, so reaching for
                `html.unescape` in here does not silently resolve to the
                parameter.
            var_name: JavaScript variable name to extract.

        Returns:
            Parsed JSON data, with Understat's HTML escaping undone.
        """
        # Pattern matches: var varName = JSON.parse('...')
        pattern = rf"var\s+{var_name}\s*=\s*JSON\.parse\('([^']+)'\)"
        match = re.search(pattern, raw_html)

        if not match:
            return None

        # The data is escaped, need to decode it
        encoded_data = match.group(1)
        decoded_data = encoded_data.encode().decode("unicode_escape")
        # Same escaping as the XHR API serves, decoded the same way (#263):
        # the two paths carry the same payload and must not disagree about it.
        return decode_entities(json.loads(decoded_data))

    async def get_league_players(self, season: str | None = None) -> list[dict[str, Any]]:
        """Get all players in the Premier League with their stats.

        Uses Understat's JSON API endpoint.

        Args:
            season: Season year (e.g., "2024" for 2024/25). Defaults to current.

        Returns:
            List of player data with xG, xA, etc.
        """
        season = season or understat_season(self.season_year)

        if season in self._league_cache:
            return self._league_cache[season]

        data = await self._get_api_json(
            f"getLeagueData/EPL/{season}",
            referer=f"league/EPL/{season}",
        )
        players_data = data.get("players") if data else None

        if not players_data:
            # The endpoint is undocumented, so a renamed key and "no matches
            # played yet" look identical here — announce it either way rather
            # than degrading silently (#97).
            logger.warning(
                "Understat league data for season %s contains no players — "
                "xG enrichment is unavailable (the endpoint shape may have changed)",
                season,
            )
            return []

        parsed = [self._parse_player(p) for p in players_data]
        self._league_cache[season] = parsed
        return parsed

    async def get_player(self, player_id: int) -> dict[str, Any] | None:
        """Get detailed stats for a specific player.

        Uses Understat's JSON API endpoint.

        Args:
            player_id: Understat player ID.

        Returns:
            Player data with match-by-match xG, xA, shots, and situation groups.
        """
        try:
            data = await self._get_api_json(
                f"getPlayerData/{player_id}",
                referer=f"player/{player_id}",
            )
            return {
                "id": player_id,
                "matches": data.get("matches") or [],
                "shots": data.get("shots") or [],
                "groups": data.get("groups") or {},
            }
        except (httpx.HTTPStatusError, httpx.RequestError):
            return None

    async def get_team(self, team_name: str, season: str | None = None) -> dict[str, Any] | None:
        """Get team stats including match xG data.

        Uses Understat's JSON API endpoint rather than HTML scraping.

        Only the requested season's matches are returned. Understat answers a
        request for a season the club has no record of with the club's most
        recent season instead of an error, so the payload is checked against
        what was asked for (see :func:`matches_in_season`); a club with no
        record for ``season`` reads as None, the same as a club Understat does
        not know at all.

        Args:
            team_name: Team name (FPL format, will be mapped).
            season: Season year (start year, e.g. "2025" for 2025/26). Defaults to current.

        Returns:
            Team data with player stats and match records, or None when
            Understat has nothing for that club in that season.

        Raises:
            ValueError: ``season`` is not a start year -- the hyphenated
                ``"2025-26"`` label is a caller error, refused before any
                request is made rather than reported as a club with no data.
        """
        season = season or understat_season(self.season_year)
        year = _season_year(season)

        # Map FPL team name to Understat format
        understat_name = TEAM_NAME_MAP.get(team_name, team_name)
        url_name = understat_name.replace(" ", "_")

        data = await self._get_team_json(url_name, season)
        if data is None:
            return None

        matches = data.get("dates") or []
        in_season = matches_in_season(matches, season)
        if matches and not in_season:
            # The players list served alongside is that other season's too,
            # so nothing in the payload describes the season asked for.
            served = sorted(
                {year for year in map(_match_season_year, matches) if year is not None}
            )
            logger.info(
                "Understat has no %s record for %s - it served %d matches from %s "
                "instead, which are ignored (expected for a club that was not in "
                "that season's Premier League)",
                season_label(year),
                understat_name,
                len(matches),
                ", ".join(season_label(year) for year in served),
            )
            return None

        return {
            "team": team_name,
            "players": [self._parse_player(p) for p in (data.get("players") or [])],
            "matches": in_season,
        }

    async def _get_team_json(self, url_name: str, season: str) -> dict[str, Any] | None:
        """Fetch team data from Understat JSON API.

        Args:
            url_name: Team name with spaces replaced by underscores.
            season: Season start year.

        Returns:
            Parsed JSON response or None on error.
        """
        try:
            return await self._get_api_json(
                f"getTeamData/{url_name}/{season}",
                referer=f"team/{url_name}/{season}",
            )
        except (httpx.HTTPStatusError, httpx.RequestError):
            return None

    def _parse_player(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse raw player data into a cleaner format.

        Args:
            data: Raw player data from Understat.

        Returns:
            Cleaned player data.
        """
        minutes = int(data.get("time", 0))
        xg = float(data.get("xG", 0))
        xa = float(data.get("xA", 0))
        npxg = float(data.get("npxG", 0))
        xg_chain = float(data.get("xGChain", 0))
        xg_buildup = float(data.get("xGBuildup", 0))

        return {
            "id": int(data.get("id", 0)),
            "name": data.get("player_name", ""),
            "team": data.get("team_title", ""),
            "position": data.get("position", ""),
            "games": int(data.get("games", 0)),
            "minutes": minutes,
            "goals": int(data.get("goals", 0)),
            "assists": int(data.get("assists", 0)),
            "xG": xg,
            "xA": xa,
            "npxG": npxg,
            "xGChain": xg_chain,
            "xGBuildup": xg_buildup,
            "shots": int(data.get("shots", 0)),
            "key_passes": int(data.get("key_passes", 0)),
            "npg": int(data.get("npg", 0)),
            # Per-90 metrics
            "xG_per_90": self._per_90(xg, minutes),
            "xA_per_90": self._per_90(xa, minutes),
            "xGI_per_90": self._per_90(xg + xa, minutes),
            "npxG_per_90": self._per_90(npxg, minutes),
            "xGChain_per_90": self._per_90(xg_chain, minutes),
            "xGBuildup_per_90": self._per_90(xg_buildup, minutes),
            # Over/underperformance
            "goals_minus_xG": int(data.get("goals", 0)) - xg,
            "assists_minus_xA": int(data.get("assists", 0)) - xa,
            "penalty_xG": round(xg - npxg, 2),
            "penalty_xG_per_90": self._per_90(xg - npxg, minutes),
        }

    def _per_90(self, stat: float, minutes: int) -> float:
        """Calculate per-90-minute stat.

        Args:
            stat: Total stat value.
            minutes: Total minutes played.

        Returns:
            Stat per 90 minutes.
        """
        if minutes == 0:
            return 0.0
        return round((stat / minutes) * 90, 2)


@lru_cache(maxsize=4096)
def _normalise(text: str) -> str:
    """Strip diacritics, punctuation, and lowercase for cross-source name comparison.

    Cached because the cross-club fallback rescans every Understat row for
    each player the club gate matched nothing for, so a full-league
    enrichment pass normalises the same few hundred names over and over.

    HTML entities are decoded first, and before the punctuation rules rather
    than after: those rules cannot undo an entity, they launder it. ``&#039;``
    lost its ``&`` and ``#`` and kept its digits, so ``Dara O&#039;Shea``
    normalised to ``dara o039shea`` -- a token indistinguishable from a real
    name (#263). `decode_entities` already decodes everything arriving from
    Understat; this repeats it because a name also reaches here from FPL,
    from a caller's own dict, and from the historical providers, and a
    matcher that can only be trusted on pre-decoded input is one call site
    away from the same silent miss.
    """
    text = html.unescape(text)
    text = strip_diacritics(text).lower()
    text = _NAME_SEPARATOR_RE.sub(" ", text)  # Name separators → spaces
    text = re.sub(r"[^a-z0-9 ]", "", text)  # Strip remaining punctuation
    return re.sub(r" +", " ", text).strip()  # Collapse whitespace


def split_team_titles(team_title: str) -> list[str]:
    """Split an Understat ``team_title`` into its constituent club names.

    Understat comma-joins every club a player has appeared for in the season,
    so a mid-season transfer reads ``"Arsenal,Crystal Palace"``.
    """
    return [part.strip() for part in team_title.split(",")]


def _carries_club(team_title: Any, fpl_team_mapped: str) -> bool:
    """Whether one Understat row belongs to an FPL club's mapped name.

    A player who moved mid-season carries every club they have turned out for,
    comma-joined ("Arsenal,Crystal Palace"), so a title that is not an outright
    match still has to be checked component-wise (#94). Equality short-circuits
    that split for the overwhelming majority of rows, which matters on an
    O(players x candidates) scan.
    """
    if not isinstance(team_title, str):
        return False
    return team_title == fpl_team_mapped or fpl_team_mapped in split_team_titles(team_title)


def understat_club_rows(
    fpl_team: str, understat_players: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The rows Understat carries for an FPL club, through the matcher's gate.

    Public so a health check can ask the question the enrichment itself asks --
    does this club's mapped name name any row in the list the scorer scans? --
    by calling the gate rather than reimplementing it alongside and drifting
    from it (#229).
    """
    fpl_team_mapped = TEAM_NAME_MAP.get(fpl_team, fpl_team)
    return [p for p in understat_players if _carries_club(p.get("team"), fpl_team_mapped)]


# Name-match tiers, in confidence order. Only the top two are trusted without a
# club agreeing: a prefix match is safe once the title already names the FPL
# club, but across all 20 it would let "B. Silva" land on any Silva in the
# league. Tiers are compared *before* the position and minutes bonuses rather
# than summed with them, so a bonus can only break a tie inside a tier — an
# exact-name match with no bonuses outranks a looser one carrying both.
_NAME_EXACT = 10
_NAME_ALL_WORDS = 8
_NAME_PREFIX = 7

# The lowest tier the club-gated pass will admit. It is every tier there is
# today, and deliberately named rather than left implicit: a future
# lower-confidence tier has to raise this floor to opt itself out.
_NAME_TIER_FLOOR = _NAME_PREFIX

# How closely the two sources must agree on minutes before a club-blind match
# is allowed to stand. Both count the same league's minutes, so a real match
# agrees closely and a namesake at another club usually does not.
_CROSS_CLUB_MIN_MINUTES_RATIO = 0.5

# FPL teams already reported as matching no Understat players, so the join-drop
# warning below fires once per team per process rather than once per player.
# Keyed on ``(fpl_team, season_label)``: a club can be legitimately absent from
# a past season's pool and still be a real map gap in the live one, so the two
# must not silence each other (#229).
_unmatched_team_warned: set[tuple[str, str | None]] = set()

# Warning code the join-drop tripwire travels under in a JSON envelope's
# ``metadata.warnings``.
UNDERSTAT_TEAM_UNMATCHED = "understat_team_unmatched"

# Name-level join tally for this run: how many players the pool was expected to
# carry the matcher was asked about, and which of them it found nothing for.
# Reset by `reset_understat_join_warnings` alongside the club record.
_name_join_attempts = 0
_name_join_misses: list[str] = []


def _report_unmatched_team(
    fpl_team: str, fpl_team_mapped: str, season_label: str | None
) -> None:
    """Announce a club no Understat row carries, once per club per pool.

    The join-drop tripwire (#97): a team name the map doesn't resolve fails
    every one of its players identically -- 20 teams in, 19 join -- and
    per-player it would just look like a string-similarity miss. #94 was
    exactly this shape.

    A *past* season's pool is the exception. The caller matches a player's
    current club against the season they are being scored on, so the three
    clubs promoted since are absent from it every year by definition, and
    nothing here can tell that apart from a map gap. Announcing it as one sent
    `fpl returnees` warning about TEAM_NAME_MAP on three healthy clubs while
    `fpl doctor --providers`, which probes the live pool, stayed correctly
    green (#229). Those stay a debug line and never reach `metadata.warnings`.
    """
    key = (fpl_team, season_label)
    if key in _unmatched_team_warned:
        return
    _unmatched_team_warned.add(key)
    if season_label is not None:
        logger.debug(
            "Understat's %s data carries no players for team %r (mapped from FPL team %r) "
            "— expected for a club that was not in that season's Premier League",
            season_label,
            fpl_team_mapped,
            fpl_team,
        )
        return
    logger.warning(
        "No Understat players carry team %r (mapped from FPL team %r) — "
        "TEAM_NAME_MAP may need updating; this club's players lose xG enrichment",
        fpl_team_mapped,
        fpl_team,
    )


def unmatched_understat_teams() -> list[str]:
    """FPL clubs this process found no live Understat rows for.

    The season in progress only: a club missing from a past season's pool is
    the promoted-club case `_report_unmatched_team` deliberately stays quiet
    about, and reporting it would be a false alarm.
    """
    return sorted(team for team, season in _unmatched_team_warned if season is None)


def understat_join_warnings() -> list[dict[str, str]]:
    """The live join-drop tripwires as `metadata.warnings` entries, one per club.

    The log line reaches a human reading stderr; a `--format json` consumer
    parses stdout and would otherwise lose the one signal saying a whole club's
    xG enrichment is missing (#229).
    """
    return [
        {
            "code": UNDERSTAT_TEAM_UNMATCHED,
            "message": (
                f"Understat carries no players for {team} (mapped to "
                f"{TEAM_NAME_MAP.get(team, team)!r}), so every one of that club's "
                "players is missing npxG, xGChain and the quality and value scores "
                "built on them. TEAM_NAME_MAP in fpl_cli/api/understat.py may need "
                "updating."
            ),
        }
        for team in unmatched_understat_teams()
    ]


def reset_understat_join_warnings() -> None:
    """Forget every join-drop tripwire and name-join tally recorded so far.

    The record is process-global so the warning fires once rather than once per
    player, which makes this the hook that scopes it to a run instead. The CLI
    group calls it before dispatching, so one command's unresolved clubs cannot
    surface in the next command's `metadata.warnings`; anything driving the
    agents directly, in a process that outlives one pass, owns the same call.
    """
    global _name_join_attempts

    _unmatched_team_warned.clear()
    _name_join_attempts = 0
    _name_join_misses.clear()


def _record_name_join(
    fpl_name: str,
    fpl_team: str,
    fpl_minutes: int | None,
    match: dict[str, Any] | None,
    season_label: str | None,
) -> None:
    """Tally one name-level join attempt, and log the misses.

    The club tripwire catches a whole club dropping out of the pool; nothing
    counted a player the pool *does* carry a club for and still failed to
    match. #263 was invisible for exactly that reason -- three players lost
    their xG enrichment with no warning anywhere, no null outside the output
    itself, and a `fpl doctor --providers` that stayed green because it only
    checks whether clubs resolve.

    A few percent of misses is normal (players genuinely absent from
    Understat, cameos it has not ingested, a name the two sources spell
    differently), so this is a debug line carrying a running rate rather than
    a warning: it is a jump in the rate that means something, not any single
    miss.

    Counted only for players FPL says have played, in the season in progress,
    whose club the pool carries:

    - a minuteless player is legitimately absent from Understat's pool, and
      counting them would bury the signal under several hundred rows;
    - a past season's pool cannot carry a club promoted since, so its misses
      are expected by definition (see `_report_unmatched_team`);
    - a club nothing carries already reports itself once, and counting it here
      would restate one club gap as twenty name misses.
    """
    global _name_join_attempts

    if season_label is not None or not fpl_minutes:
        return

    _name_join_attempts += 1
    if match is not None:
        return

    _name_join_misses.append(f"{fpl_name} ({fpl_team}, {fpl_minutes}m)")
    logger.debug(
        "No Understat row matched %s (%s, %dm) — %d of %d FPL players with minutes "
        "unmatched so far this run",
        fpl_name,
        fpl_team,
        fpl_minutes,
        len(_name_join_misses),
        _name_join_attempts,
    )


def understat_name_join_stats() -> dict[str, Any]:
    """This run's name-level join tally: how much of the pool actually joined.

    ``attempted`` counts the players the live pool is expected to cover --
    FPL says they have played, and Understat carries their club -- so the rate
    is against the population a miss is surprising in, not against every
    registered player. ``unmatched`` names them in the order they were tried.
    """
    missed = len(_name_join_misses)
    return {
        "attempted": _name_join_attempts,
        "matched": _name_join_attempts - missed,
        "missed": missed,
        "miss_rate": round(missed / _name_join_attempts, 4) if _name_join_attempts else 0.0,
        "unmatched": list(_name_join_misses),
    }


def _minutes_ratio(fpl_minutes: int | None, player: dict[str, Any]) -> float | None:
    """How closely FPL and Understat agree on minutes, or None if either is unknown.

    Two zeroes agree perfectly rather than counting as unknown: a player
    neither source has seen play is not a mismatch, and that is exactly the
    profile of the signing ``_match_across_clubs`` exists for.
    """
    us_minutes = player.get("minutes")
    if fpl_minutes is None or isinstance(us_minutes, bool):
        return None
    if not isinstance(us_minutes, (int, float)):
        return None
    high = max(fpl_minutes, us_minutes)
    if high <= 0:
        return 1.0
    return min(fpl_minutes, us_minutes) / high


def _score_candidate(
    player: dict[str, Any],
    fpl_name_norm: str,
    fpl_words: list[str],
    fpl_position: str | None,
    fpl_minutes: int | None,
    min_name_tier: int,
) -> tuple[int, int]:
    """Score one Understat candidate as ``(name_tier, bonus)``.

    The pair is ordered lexicographically by both callers, which keeps the
    position and minutes bonuses as tiebreakers *within* a name tier instead of
    letting them promote a looser name match above a stronger one.

    A tier of 0 means "not a candidate": no viable name match, or one below
    ``min_name_tier``. Every field is read defensively — the fallback pass
    scores rows belonging to clubs the caller never asked about, so one
    malformed row in an undocumented payload must not take out the lookup.
    """
    understat_name = _normalise(str(player.get("name") or ""))
    if not understat_name:
        return 0, 0
    us_words = understat_name.split()

    # Word-overlap name scoring
    if fpl_name_norm == understat_name:
        tier = _NAME_EXACT
    elif fpl_words and all(w in us_words for w in fpl_words):
        tier = _NAME_ALL_WORDS
    elif fpl_words and all(
        any(uw.startswith(fw) for uw in us_words) for fw in fpl_words
    ):
        tier = _NAME_PREFIX
    else:
        return 0, 0

    if tier < min_name_tier:
        return 0, 0

    bonus = 0

    # Position bonus
    position = player.get("position")
    if fpl_position and isinstance(position, str):
        understat_positions = {
            POSITION_MAP.get(tok) for tok in position.split() if tok in POSITION_MAP
        }
        if fpl_position in understat_positions:
            bonus += 2

    # Minutes proximity bonus
    ratio = _minutes_ratio(fpl_minutes, player)
    if ratio is not None:
        if ratio >= 0.8:
            bonus += 2
        elif ratio >= 0.5:
            bonus += 1

    return tier, bonus


def _match_across_clubs(
    fpl_name_norm: str,
    fpl_words: list[str],
    understat_players: list[dict[str, Any]],
    fpl_position: str | None,
    fpl_minutes: int | None,
) -> dict[str, Any] | None:
    """Match on name alone, for a player the club gate rejected outright (#234).

    An Understat ``team_title`` names only the clubs a player has actually
    turned out for, so a deadline-day mover who has not yet featured for their
    new club carries the old club alone while FPL already lists them at the new
    one. #151's comma-joined title never forms, every candidate fails the gate,
    and they read as having no Understat data at all through exactly the weeks
    people are deciding whether to buy.

    Dropping the gate re-opens the homonym risk it existed to close, so this
    pass is deliberately narrow. Only a full-name match counts (exact, or every
    FPL word present — no prefix tier). Minutes must corroborate where both
    sources report them, since a namesake at another club rarely has a season
    the same length. A top pair two candidates share is refused rather than
    guessed at. The caller adds the last condition: it only reaches here for a
    club Understat *does* carry players for, so an unresolved club still fails
    as a block rather than 20 players each guessing across the league.
    """
    best_match: dict[str, Any] | None = None
    best_score = (0, 0)
    ambiguous = False

    for player in understat_players:
        ratio = _minutes_ratio(fpl_minutes, player)
        if ratio is not None and ratio < _CROSS_CLUB_MIN_MINUTES_RATIO:
            continue
        score = _score_candidate(
            player,
            fpl_name_norm,
            fpl_words,
            fpl_position,
            fpl_minutes,
            min_name_tier=_NAME_ALL_WORDS,
        )
        if score[0] == 0:
            continue
        if score > best_score:
            best_score, best_match, ambiguous = score, player, False
        elif score == best_score and player is not best_match:
            ambiguous = True

    if ambiguous:
        logger.debug(
            "Understat name %r matches several players outside the FPL club — "
            "declining rather than guessing",
            fpl_name_norm,
        )
        return None

    return best_match


def match_fpl_to_understat(
    fpl_name: str,
    fpl_team: str,
    understat_players: list[dict[str, Any]],
    fpl_position: str | None = None,
    fpl_minutes: int | None = None,
    season_label: str | None = None,
) -> dict[str, Any] | None:
    """Match an FPL player to their Understat data using multi-signal scoring.

    Scores candidates carrying the player's FPL club on name match quality,
    position and minutes played, and returns the most confident. A player whose
    own club carries no name match at all falls through to the name-only pass
    in ``_match_across_clubs`` (#234) — but only when the club itself resolved,
    so a club no Understat row carries keeps failing as a block. Returns None
    when neither pass is confident.

    *season_label* names the season ``understat_players`` covers when that is
    not the one in progress; leaving it None says the pool is the live one. It
    only steers the join-drop tripwire — see ``_report_unmatched_team`` for why
    an absent club means something different in a past season's pool.
    """
    fpl_name_norm = _normalise(fpl_name)
    fpl_words = fpl_name_norm.split()
    fpl_team_mapped = TEAM_NAME_MAP.get(fpl_team, fpl_team)

    best_match = None
    best_score = (0, 0)
    team_seen = False

    for player in understat_players:
        # The same gate `understat_club_rows` (and so `fpl doctor`) applies, so
        # the health check and the enrichment can never disagree about which
        # clubs resolve. Season totals stay cumulative across both clubs of a
        # mid-season move, which is what the minutes bonus wants — FPL's
        # minutes are cumulative too.
        if not _carries_club(player.get("team"), fpl_team_mapped):
            continue
        team_seen = True

        score = _score_candidate(
            player,
            fpl_name_norm,
            fpl_words,
            fpl_position,
            fpl_minutes,
            min_name_tier=_NAME_TIER_FLOOR,
        )
        if score > best_score:
            best_score = score
            best_match = player

    if not team_seen and understat_players:
        _report_unmatched_team(fpl_team, fpl_team_mapped, season_label)

    if not team_seen:
        # Nothing carries this club, so every one of its players fails here
        # identically — a TEAM_NAME_MAP gap or a roster Understat has yet to
        # ingest, not a transfer. Sending 20 players off to guess across the
        # league would turn one legible warning into 20 silent strangers'
        # rows, so the whole club fails together as it did before #234. The
        # name-level tally skips it for the same reason.
        return None

    if best_match is None:
        # The club resolved but carries no name match, which is what a player
        # who has moved and not yet played for the new club looks like.
        best_match = _match_across_clubs(
            fpl_name_norm, fpl_words, understat_players, fpl_position, fpl_minutes
        )

    _record_name_join(fpl_name, fpl_team, fpl_minutes, best_match, season_label)
    return best_match
