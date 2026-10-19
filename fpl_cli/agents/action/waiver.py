"""Waiver agent for recommending and managing draft waiver claims."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.agents.common import (
    enrich_player,
    fetch_understat_lookup,
    rekey_for_draft,
)
from fpl_cli.api.fpl import FPLClient
from fpl_cli.api.fpl_draft import (
    FPLDraftClient,
    match_draft_teams_to_main,
    match_draft_to_main,
    unread_squads_warning,
    untranslated_draft_teams,
)
from fpl_cli.models.player import Player
from fpl_cli.models.types import EnrichedPlayer, WaiverPoolEntry, WaiverTarget
from fpl_cli.services.player_prior import CUTOFF_GW, early_season_quality_warning
from fpl_cli.services.scoring import (
    ConsistencySignals,
    apply_adjusted_npxg,
    apply_consistency,
    apply_gk_signals,
    build_player_evaluation,
    calculate_waiver_score,
    compute_aggregate_matchup,
    compute_form_trajectory,
    compute_xgi_sustainability,
    coverage_warnings,
    prepare_scoring_data,
    read_chance_of_playing,
    understat_join_coverage,
)
from fpl_cli.services.scoring.constants import CONSISTENCY_PHASE_IN_START
from fpl_cli.utils.text import join_and

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fpl_cli.services.scoring import ScoringData


class WaiverAgent(Agent):
    """Agent for analyzing and recommending waiver claims.

    Responsibilities:
    - Analyze available players for waiver potential
    - Compare against current team needs
    - Rank waiver targets with reasoning
    - Track waiver deadline
    """

    name = "WaiverAgent"
    description = "Recommends waiver claims for draft leagues"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.client = FPLDraftClient()
        self.fpl_client = FPLClient()
        self._adjusted_npxg_lookup: dict[int, float] | None = None
        self._consistency_lookup: dict[int, ConsistencySignals] | None = None
        self._main_player_map: dict[int, Player] = {}
        self.league_id = config.get("draft_league_id") if config else None
        self.entry_id = config.get("draft_entry_id") if config else None

    async def close(self) -> None:
        await self.client.close()
        await self.fpl_client.close()

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Analyze and recommend waiver targets.

        Args:
            context: Can contain:
                - 'league_id': Draft league ID
                - 'entry_id': Your draft entry ID
                - 'available_players': Pre-fetched available players
                - 'current_team': Your current squad

        Returns:
            AgentResult with waiver recommendations.
        """
        league_id = (context or {}).get("league_id") or self.league_id
        entry_id = (context or {}).get("entry_id") or self.entry_id

        if not league_id:
            return self._create_result(
                AgentStatus.FAILED,
                message="No draft league ID provided",
                errors=["Set draft_league_id in config or provide in context"],
            )

        self.log(f"Analyzing waiver options for league {league_id}...")

        try:
            # Fetch data
            bootstrap = await self.client.get_bootstrap_static()
            teams_data = bootstrap.get("teams", [])
            draft_teams = {t["id"]: t for t in teams_data}

            # Every parsed player carries the main game's team id, since that
            # is what fixtures, form and FDR are keyed by (#423). Only the
            # main team list is needed this early, and it costs nothing extra:
            # the bootstrap it comes from is cached on the client, so
            # prepare_scoring_data reads the same copy further down.
            team_id_map = match_draft_teams_to_main(teams_data, await self.fpl_client.get_teams())

            # Get available players
            available = await self.client.get_available_players(league_id, bootstrap)

            # Get recently released players (independent of entry_id)
            recent_releases: list[dict[str, Any]] = []
            league_details: dict[str, Any] = {}
            try:
                releases = await self.client.get_recent_releases(league_id, bootstrap)
                # Build entry->name map to resolve who dropped each player
                league_details = await self.client.get_league_details(league_id)
                league_entries = league_details.get("league_entries", [])
                entry_name_map = {
                    e.get("entry_id"): f"{e.get('player_first_name', '')} {e.get('player_last_name', '')}".strip()
                    for e in league_entries
                }
                recent_releases = [
                    {
                        **self._enrich(r["player"], team_id_map, draft_teams),
                        "dropped_by": entry_name_map.get(r["dropped_by"], str(r["dropped_by"])),
                        "gameweek": r["gameweek"],
                    }
                    for r in releases[:20]
                ]
            except Exception as e:  # noqa: BLE001 — best-effort enrichment
                self.log_warning(f"Could not fetch recent releases: {e}")

            # Parse players
            parsed_available = [
                self._enrich(p, team_id_map, draft_teams, include_availability=False)
                for p in available
            ]

            # Enrich with Understat data
            us_lookup = await fetch_understat_lookup(
                cast(list[dict[str, Any]], parsed_available),
                lambda p: p.get("team_name", ""),
            )
            for i, us_match in us_lookup.items():
                parsed_available[i]["npxG_per_90"] = us_match.get("npxG_per_90")
                parsed_available[i]["xGChain_per_90"] = us_match.get("xGChain_per_90")
                parsed_available[i]["penalty_xG_per_90"] = us_match.get("penalty_xG_per_90")

            # Get current squad if entry_id provided
            current_squad = []
            squad_by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

            if entry_id:
                try:
                    # Reuse league_details from recent releases fetch, or fetch fresh
                    if not league_details:
                        league_details = await self.client.get_league_details(league_id)
                    league_entries = league_details.get("league_entries", [])

                    # Find matching entry - entry_id could be entry_id or id
                    # The API uses entry_id for fetching picks
                    draft_entry_id = None
                    for entry in league_entries:
                        if entry.get("entry_id") == entry_id or entry.get("id") == entry_id:
                            draft_entry_id = entry.get("entry_id")  # Use entry_id for API calls
                            self.log(f"Found team: {entry.get('entry_name')}")
                            break

                    if draft_entry_id:
                        # Get current gameweek
                        game_data = await self.client.get_game_state()
                        current_gw = game_data.get("current_event", 1)

                        # Fetch picks for current gameweek
                        picks_data = await self.client.get_entry_picks(draft_entry_id, current_gw)
                        player_ids = [p.get("element") for p in picks_data.get("picks", [])]

                        player_map = {p["id"]: p for p in bootstrap.get("elements", [])}
                        for pid in player_ids:
                            if pid in player_map:
                                player = self._enrich(
                                    player_map[pid], team_id_map, draft_teams,
                                    include_availability=False,
                                )
                                current_squad.append(player)
                                pos = player.get("position", "???")
                                if pos in squad_by_position:
                                    squad_by_position[pos].append(player)
                    else:
                        self.log_warning(f"Entry ID {entry_id} not found in league entries")
                except Exception as e:  # noqa: BLE001 — best-effort enrichment
                    self.log_warning(f"Could not fetch current squad: {e}")

            # Fetch fixture data and build shared scoring context
            data = await prepare_scoring_data(
                self.fpl_client, include_players=True,
                include_history=True, include_prior=True,
                include_match_data=True,
            )
            next_gw_id = data.next_gw_id
            scoring_ctx = data.scoring_ctx

            # The draft bootstrap carries neither saves nor expected goals
            # conceded, so a keeper's three scoring signals only exist on the
            # main game's player. Joined on `code`, never on (web_name,
            # team_id) — a mid-season rename breaks the name join (#168).
            # Every row the payload carries is joined, not only the pool, so
            # no squad or release row keeps the parser's 0 for dc_per_90
            # beside a target carrying the real rate (#437). Squad and release
            # rows are draft bootstrap elements; keying on id matches a player
            # who sits in two of the lists once.
            payload_rows = (*parsed_available, *current_squad, *recent_releases)
            elements = {e["id"]: e for e in bootstrap.get("elements", [])}
            elements.update((e["id"], e) for e in available)
            payload_ids = {int(p.get("id") or 0) for p in payload_rows}
            self._main_player_map = match_draft_to_main(
                [elements[i] for i in payload_ids if i in elements],
                data.players or [],
                team_id_map=team_id_map,
            )
            self._apply_main_dc_per_90(payload_rows)

            # Every lookup below arrives keyed by the main game's element id,
            # and the pool scored from here on is keyed by the draft's, so
            # each one is translated through the same join exactly once (#209).
            # Read raw they land on whichever main-game player happens to
            # share the draft id: the two numberings coincide for most of the
            # pool and diverge over the elements each bootstrap registered
            # after the other, so the miss is silent and player-specific.
            self._player_histories = rekey_for_draft(
                data.player_histories, self._main_player_map,
            ) or {}
            self._player_priors = rekey_for_draft(
                data.player_priors, self._main_player_map,
            )
            self._adjusted_npxg_lookup = rekey_for_draft(
                data.adjusted_npxg_lookup, self._main_player_map,
            )
            self._consistency_lookup = rekey_for_draft(
                data.consistency_lookup, self._main_player_map,
            )

            # Enrich available players with matchup and FDR
            matchup_cache: dict[tuple[int, str], float] = {}
            for player in parsed_available:
                tid = player.get("team_id")
                if tid is None:
                    # No main-game club to read fixtures for: unscored
                    # rather than scored on whichever club shares the id
                    continue
                pos = player.get("position", "MID")
                matchup_avg_3gw, positional_fdr = compute_aggregate_matchup(
                    tid, pos, scoring_ctx, matchup_cache=matchup_cache,
                )
                if matchup_avg_3gw is not None:
                    player["matchup_avg_3gw"] = matchup_avg_3gw
                if positional_fdr is not None:
                    player["positional_fdr"] = positional_fdr

            # Score and rank waiver targets
            waiver_targets = self._rank_waiver_targets(
                parsed_available,
                current_squad,
                squad_by_position,
                next_gw_id=next_gw_id,
            )

            # Get waiver priority
            waiver_order = await self.client.get_waiver_order(league_id)

            # Find our position in waiver order
            our_waiver_position = None
            if entry_id:
                for i, team in enumerate(waiver_order, 1):
                    if team.get("entry_id") == entry_id:
                        our_waiver_position = i
                        break

            self.log_success(f"Found {len(waiver_targets)} potential waiver targets")

            # waiver_score carries the early-season prior blend; say so beside
            # it, and say whether the blend actually ran. On this path the
            # prior can go missing two ways — the history fetch failed, or the
            # draft element found no main-game counterpart to join to — and
            # the notice is what tells a ranked list built on pedigree from
            # one built on three gameweeks of observation.
            warning = early_season_quality_warning(
                next_gw_id,
                blended=bool(self._player_priors),
                score_names=("waiver_score",),
            )
            warnings = [warning] if warning is not None else []
            club_warning = self._untranslated_club_warning(
                [*parsed_available, *current_squad], teams_data, team_id_map,
            )
            if club_warning is not None:
                warnings.append(club_warning)
            # Table mode prints every entry here to stderr, so this is the one
            # place the miss is reported for either format (#448).
            # Ranked order, so the names the entry shows are the misses most
            # likely to be claimed rather than whoever sorts first in the pool.
            unjoined_warning = self._unjoined_player_warning(
                waiver_targets,
                carried_terms=self._join_carried_terms(data, next_gw_id),
                main_list_empty=not data.players,
            )
            if unjoined_warning is not None:
                warnings.append(unjoined_warning)
            squads_warning = unread_squads_warning(self.client.unread_squads)
            if squads_warning is not None:
                warnings.append(squads_warning)
            # The bundle's coverage entries, plus this agent's own Understat
            # join over the draft pool, which it runs itself rather than
            # through the bundle, judged by the bundle's rule (#438).
            warnings.extend(data.warnings)
            warnings.extend(coverage_warnings([
                understat_join_coverage(parsed_available, us_lookup, next_gw_id=next_gw_id),
            ]))

            return self._create_result(
                AgentStatus.SUCCESS,
                data={
                    "warnings": warnings,
                    "league_id": league_id,
                    "entry_id": entry_id,
                    "waiver_position": our_waiver_position,
                    "total_waiver_teams": len(waiver_order),
                    "top_targets": waiver_targets[:15],
                    "targets_by_position": self._group_by_position(waiver_targets[:30]),
                    "pool": self._build_pool(parsed_available),
                    "current_squad": current_squad,
                    "squad_weaknesses": self._identify_weaknesses(squad_by_position),
                    "recommendations": self._generate_recommendations(
                        waiver_targets,
                        squad_by_position,
                    ),
                    "recent_releases": recent_releases,
                },
                message=f"Top waiver target: {waiver_targets[0]['player_name'] if waiver_targets else 'None'}",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to analyze waivers: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to analyze waiver options",
                errors=[str(e)],
            )

    def _apply_main_dc_per_90(
        self, players: Sequence[EnrichedPlayer | dict[str, Any]],
    ) -> None:
        """Overwrite each player's ``dc_per_90`` with its main-game rate.

        The draft bootstrap carries defensive contribution only as a season
        total, and the parser drops even that, so ``enrich_player`` leaves
        every draft player at 0. Only the defender score weights the rate
        (``without_xgi``): left at 0 it scored each defender with no DC term
        against a waiver anchor calibrated with it (#437). Every position is
        filled all the same, because midfielders and forwards earn DC points
        too and a 0 on their rows would be a made-up figure, not an absent
        one; for them the rate is display-only, as it is in classic. Read off
        the joined main player, the field classic scoring uses; a miss keeps 0.
        """
        for player in players:
            main_player = self._main_player_map.get(int(player.get("id") or 0))
            if main_player is not None:
                player["dc_per_90"] = main_player.defensive_contribution_per_90

    def _enrich(
        self,
        raw: dict[str, Any],
        team_id_map: Mapping[int, int],
        draft_teams: Mapping[int, dict[str, Any]],
        *,
        include_availability: bool = True,
    ) -> EnrichedPlayer:
        """Parse one draft player and attach his club's names.

        ``team_id`` comes out in the main game's numbering, for fixtures and
        form. The names are the draft list's own entry for his draft club, a
        same-game lookup that needs no translation, so a club the main game
        does not list keeps its name: Understat is asked about a real club,
        and the exposure count keeps its players apart (#423).
        """
        return enrich_player(
            self.client.parse_player(raw, team_id_map),
            draft_teams.get(raw.get("team", 0)),
            include_availability=include_availability,
        )

    @staticmethod
    def _untranslated_club_warning(
        players: list[EnrichedPlayer],
        draft_teams: list[dict[str, Any]],
        team_id_map: Mapping[int, int],
    ) -> dict[str, str] | None:
        """The notice for players whose draft club has no main-game id.

        They are ranked without the matchup and FDR terms, which would
        otherwise read as a quiet fixture list rather than a failed
        translation, so the reader is told which clubs and how many players.
        """
        affected = [p for p in players if p.get("team_id") is None]
        if not affected:
            return None
        clubs = untranslated_draft_teams(draft_teams, team_id_map)
        return {
            "code": "draft_club_untranslated",
            "message": (
                f"{len(affected)} player(s) at draft clubs the main game does not list"
                f" ({', '.join(clubs) or 'unknown club'}) are ranked without fixture"
                " difficulty or matchup. `fpl doctor --providers` compares the two club lists."
            ),
        }

    @staticmethod
    def _join_carried_terms(data: ScoringData, next_gw_id: int) -> list[str]:
        """The score terms the join carries onto every position this gameweek.

        Each lookup is main-keyed and reaches a draft player only through the
        join, but it is only a lost term where it would have scored: a feed
        that came back empty is missing for joined players too, the prior
        blend ends at ``CUTOFF_GW``, and the consistency bonus phases in after
        ``CONSISTENCY_PHASE_IN_START``. Naming a term outside those windows
        blames the join for something no player is scored on (#479 review).
        """
        terms = []
        if data.player_histories:
            terms.append("history")
        if data.player_priors and next_gw_id < CUTOFF_GW:
            terms.append("prior")
        if data.adjusted_npxg_lookup:
            terms.append("adjusted npxG")
        if data.consistency_lookup and next_gw_id > CONSISTENCY_PHASE_IN_START:
            terms.append("consistency signals")
        return terms

    def _unjoined_player_warning(
        self,
        ranked: Sequence[Mapping[str, Any]],
        *,
        carried_terms: Sequence[str] = (),
        main_list_empty: bool = False,
    ) -> dict[str, str] | None:
        """The notice for players the main-game join missed.

        *carried_terms* (``_join_carried_terms``) are what a miss costs every
        position: a player without them is scored on a different mix of terms
        from the rest of the pool, and the change is not uniformly down (a
        midfielder dropped from the join has been seen to climb past the top
        defender, #475), so a consumer can read a miss only as a score that is
        not comparable, never as an under-rating. Keepers and defenders lose a
        position-specific input on top (a keeper's saves/xGC block, a
        defender's DC/90), so they count even when nothing is carried; a
        midfielder or forward counts only when something is. The names come
        from *ranked* in its order, the first three per position, so the miss
        a consumer is likeliest to claim is the one shown.

        An empty main-game player list misses the whole pool for one upstream
        reason, so it gets one sentence saying so rather than counts naming
        hundreds of players.
        """
        missed = []
        extras = []
        for position, noun, extra in (
            ("GK", "keeper", "saves/xGC signals"),
            ("DEF", "defender", "defensive contribution"),
            ("MID", "midfielder", None),
            ("FWD", "forward", None),
        ):
            if extra is None and not carried_terms:
                continue
            names = [
                p["player_name"] for p in ranked
                if p.get("position") == position
                and int(p.get("id") or 0) not in self._main_player_map
            ]
            if names:
                shown = ", ".join(names[:3]) + (", ..." if len(names) > 3 else "")
                missed.append(f"{len(names)} draft {noun}(s) ({shown})")
                if extra is not None:
                    extras.append((noun, extra))
        if not missed:
            return None
        clauses = []
        if carried_terms:
            clauses.append(f"each is scored without its {join_and(carried_terms)}")
        verb = "also" if carried_terms else "are scored"
        clauses.extend(f"{noun}s {verb} without {extra}" for noun, extra in extras)
        if main_list_empty:
            message = (
                "The main-game player list is empty, so no draft player could be"
                f" joined: {join_and(clauses)}."
            )
        else:
            message = (
                f"No main-game match for {join_and(missed)}: {join_and(clauses)},"
                " so their waiver scores are not comparable with the rest of the pool."
            )
        return {"code": "draft_player_unjoined", "message": message}

    @staticmethod
    def _build_pool(available: list[EnrichedPlayer]) -> list[WaiverPoolEntry]:
        """Build the lean identity roster of every unowned player.

        ``top_targets`` is the ranked top 15, and the availability factor in the
        waiver score suppresses anyone who is not playing, so a flagged player
        can never surface there. Draft validation checks pool membership against
        this full roster instead, so a claim staked on a returnee is not
        reported as a pool miss.

        Each row carries ``code`` beside the draft ``id`` because the one
        consumer that joins this roster to another command's rows -- the
        gw-prep skill, matching the returnee radar's main-game entries against
        it -- has no key in common otherwise: the radar emits main ids, and a
        mid-season addition holds a different id in each game (#418).
        """
        return [
            WaiverPoolEntry(
                id=int(p["id"] or 0),
                code=p.get("code"),
                player_name=p["player_name"],
                position=p["position"],
                team_short=p["team_short"],
            )
            for p in available
        ]

    def _get_team_exposure(
        self,
        squad_by_position: dict[str, list],
    ) -> dict[str, int]:
        """Count players per team in current squad."""
        team_counts: dict[str, int] = defaultdict(int)
        for players in squad_by_position.values():
            for p in players:
                team_short = p.get("team_short", "???")
                team_counts[team_short] += 1
        return dict(team_counts)

    def _check_team_exposure(
        self,
        target: WaiverTarget,
        drop_candidate: dict[str, Any] | None,
        team_counts: dict[str, int],
    ) -> tuple[int, str | None]:
        """Check resulting team exposure after a waiver.

        Returns:
            (new_count, warning_message or None)
        """
        target_team = target.get("team_short", "???")
        drop_team = drop_candidate.get("team_short") if drop_candidate else None

        current = team_counts.get(target_team, 0)

        # If dropping from same team, net change is 0
        if drop_team == target_team:
            return current, None

        new_count = current + 1

        if new_count >= 4:
            return new_count, f"Heavy exposure: {new_count} {target_team} players"
        elif new_count == 3:
            return new_count, f"Triple-up: 3 {target_team} players"

        return new_count, None

    def _rank_waiver_targets(
        self,
        available: list[EnrichedPlayer],
        current_squad: list[dict[str, Any]],
        squad_by_position: dict[str, list],
        next_gw_id: int = 38,
    ) -> list[WaiverTarget]:
        """Rank available players as waiver targets."""
        scored_players = []
        team_counts = self._get_team_exposure(squad_by_position)

        for player in available:
            score = self._calculate_waiver_score(
                player, squad_by_position, team_counts, next_gw_id=next_gw_id,
            )
            reasons = self._generate_target_reasons(player, squad_by_position)

            scored_players.append({
                **player,  # superset of WaiverTarget keys via EnrichedPlayer
                "waiver_score": score,
                "reasons": reasons,
            })

        # No position-mean shrinkage here: the early-season prior is blended
        # into the quality baseline inside the score itself (#206), which can
        # reorder the pool as shrinkage cannot, and the two are never stacked.
        # The #122 hold-out for players known not to be playing lives inside
        # the blend, so it still applies.

        # Sort by waiver score
        scored_players.sort(key=lambda p: p["waiver_score"], reverse=True)
        return scored_players

    def _calculate_waiver_score(
        self,
        player: EnrichedPlayer,
        squad_by_position: dict[str, list],
        team_counts: dict[str, int] | None = None,
        next_gw_id: int = 38,
    ) -> int:
        """Calculate a waiver priority score via the player scoring engine.

        The player's prior carries the early-season blend into the score's
        quality baseline, in place of the position-mean shrinkage this ranking
        used to apply afterwards (#206). Both read ``self._player_priors``,
        which ``run()`` re-keys into draft element ids (#209).
        """
        enrichment: dict[str, Any] = {}
        histories = getattr(self, "_player_histories", {})
        history = histories.get(player.get("id", 0), [])
        if history:
            enrichment["form_trajectory"] = compute_form_trajectory(history, next_gw_id)
            sustainability, divergence = compute_xgi_sustainability(history, next_gw_id, player.get("position", "MID"))
            enrichment["xgi_sustainability"] = sustainability
            enrichment["xgi_divergence"] = divergence
        priors = getattr(self, "_player_priors", None)
        prior = priors.get(player.get("id", 0)) if priors else None
        if prior:
            enrichment["prior_confidence"] = prior.confidence
        if "npxG_per_90" not in enrichment:
            npxg = player.get("npxG_per_90")
            if npxg is not None:
                enrichment["npxG_per_90"] = npxg
        pid = int(player.get("id") or 0)
        apply_adjusted_npxg(enrichment, pid, self._adjusted_npxg_lookup)
        apply_consistency(enrichment, pid, self._consistency_lookup)
        gk_signals_supplied = apply_gk_signals(
            enrichment, pid, self._main_player_map, position=player.get("position"),
        )
        evaluation, _ = build_player_evaluation(
            player,
            enrichment=enrichment,
            matchup_avg_3gw=player.get("matchup_avg_3gw"),
            positional_fdr=player.get("positional_fdr"),
        )
        return calculate_waiver_score(
            evaluation,
            squad_by_position=squad_by_position,
            team_counts=team_counts,
            next_gw_id=next_gw_id,
            gk_signals_supplied=gk_signals_supplied,
            prior=prior,
        )

    def _generate_target_reasons(
        self,
        player: EnrichedPlayer,
        squad_by_position: dict[str, list],
    ) -> list[str]:
        """Generate reasons why a player is a good waiver target."""
        reasons = []

        form = player.get("form", 0)
        if form >= 6:
            reasons.append(f"Excellent form ({form})")
        elif form >= 4:
            reasons.append(f"Good form ({form})")

        ppg = player.get("ppg", 0)
        if ppg >= 5:
            reasons.append(f"Strong PPG ({ppg:.1f})")

        xgi = player.get("xGI_per_90", 0)
        if xgi >= 0.4:
            reasons.append(f"High xGI ({xgi:.2f}/90)")

        minutes = player.get("minutes", 0)
        if minutes >= 1500:
            reasons.append("Regular starter")

        # Check if fills a need
        pos_name = player.get("position", "???")
        if pos_name in squad_by_position:
            position_players = squad_by_position[pos_name]
            if position_players:
                avg_form = sum(p.get("form", 0) for p in position_players) / len(position_players)
                if form > avg_form + 1:
                    reasons.append(f"Better than current {pos_name} options")

        if not reasons:
            reasons.append("Depth option")

        return reasons

    def _group_by_position(
        self,
        players: list[WaiverTarget],
    ) -> dict[str, list[WaiverTarget]]:
        """Group players by position."""
        by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}

        for player in players:
            pos = player.get("position", "???")
            if pos in by_position:
                by_position[pos].append(player)

        return by_position

    def _identify_weaknesses(
        self,
        squad_by_position: dict[str, list],
    ) -> list[dict[str, Any]]:
        """Identify weak positions in the squad."""
        weaknesses = []

        for pos, players in squad_by_position.items():
            if not players:
                weaknesses.append({
                    "position": pos,
                    "severity": "high",
                    "reason": "No players at this position",
                })
            else:
                avg_form = sum(p.get("form", 0) for p in players) / len(players)
                if avg_form < 3:
                    weaknesses.append({
                        "position": pos,
                        "severity": "medium",
                        "reason": f"Low average form ({avg_form:.1f})",
                        "current_players": [p.get("player_name") for p in players],
                    })

        return weaknesses

    def _calculate_drop_priority(self, player: dict[str, Any]) -> float:
        """Calculate drop priority score (higher = more droppable).

        Priority order:
        1. Suspended players (status="s") - highest priority to drop
        2. Unavailable with 0% chance of playing
        3. Injured with low chance (<50%)
        4. Injured with medium chance (50-75%)
        5. Available players sorted by form (lowest form = higher priority)
        """
        status = player.get("status", "a")
        chance = read_chance_of_playing(player)
        form = player.get("form", 0)

        # Suspended players are highest priority to drop
        if status == "s":
            return 1000

        # Unavailable with 0% chance
        if status != "a" and chance == 0:
            return 500

        # Injured with low chance (<50%)
        if status != "a" and chance is not None and chance < 50:
            return 200 + (50 - chance)

        # Injured with medium chance (50-75%)
        if status != "a" and chance is not None and chance < 75:
            return 100 + (75 - chance)

        # Available players - inverse form (lower form = higher drop score)
        return 10 - min(form, 10)

    def _generate_recommendations(
        self,
        waiver_targets: list[WaiverTarget],
        squad_by_position: dict[str, list],
    ) -> list[dict[str, Any]]:
        """Generate specific waiver recommendations."""
        recommendations = []
        team_counts = self._get_team_exposure(squad_by_position)

        # Find best target for each position
        seen_positions = set()

        for target in waiver_targets[:20]:
            pos = target.get("position", "???")

            if pos not in seen_positions and pos in squad_by_position:
                squad_players = squad_by_position[pos]

                # Find worst player to drop (highest drop priority)
                drop_candidate = None
                if squad_players:
                    drop_candidate = max(
                        squad_players, key=lambda p: self._calculate_drop_priority(p)
                    )

                # Determine why this player is being dropped
                drop_reason = None
                if drop_candidate:
                    drop_status = drop_candidate.get("status", "a")
                    drop_chance = read_chance_of_playing(drop_candidate)
                    if drop_status == "s":
                        drop_reason = "Suspended"
                    elif drop_status != "a" and drop_chance == 0:
                        drop_reason = "Unavailable (0%)"
                    elif drop_status != "a" and drop_chance is not None and drop_chance < 75:
                        drop_reason = f"Doubtful ({drop_chance}%)"
                    else:
                        drop_reason = f"Low form ({drop_candidate.get('form', 0)})"

                # Check team exposure
                new_count, exposure_warning = self._check_team_exposure(
                    target, drop_candidate, team_counts
                )

                priors = getattr(self, "_player_priors", None)
                target_prior = priors.get(target.get("id", 0)) if priors else None
                reliability = target_prior.reliability if target_prior is not None else None

                con_lookup = getattr(self, "_consistency_lookup", None)
                con_signals = con_lookup.get(target.get("id", 0)) if con_lookup else None
                con_pct = con_signals.cv_xgi_percentile if con_signals is not None else None

                rec: dict[str, Any] = {
                    "priority": len(recommendations) + 1,
                    "target": {
                        "name": target.get("player_name"),
                        "team": target.get("team_short"),
                        "position": pos,
                        "form": target.get("form"),
                        "waiver_score": target.get("waiver_score"),
                        "reliability": reliability,
                        "cv_xgi_percentile": con_pct,
                    },
                    "drop": {
                        "name": drop_candidate.get("player_name") if drop_candidate else None,
                        "form": drop_candidate.get("form") if drop_candidate else None,
                        "reason": drop_reason,
                    } if drop_candidate else None,
                    "reasons": target.get("reasons", []),
                }

                if exposure_warning:
                    rec["exposure"] = {
                        "team": target.get("team_short"),
                        "count_after": new_count,
                        "warning": exposure_warning,
                    }

                recommendations.append(rec)
                seen_positions.add(pos)

            if len(recommendations) >= 5:
                break

        return recommendations
