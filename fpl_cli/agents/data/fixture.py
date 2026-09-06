"""Fixture agent for fetching and analyzing Premier League fixtures."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fpl_cli.models.team import Team
    from fpl_cli.services.fixture_predictions import BlankPrediction, BlankTeamInfo, DoublePrediction, DoubleTeamInfo

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.api.fpl import FPLClient
from fpl_cli.models.fixture import Fixture
from fpl_cli.season import TOTAL_GAMEWEEKS
from fpl_cli.services.fixture_predictions import (
    FixturePredictionsService,
    find_blank_gameweeks,
    find_double_gameweeks,
)
from fpl_cli.services.matchup import POSITION_WEIGHTS as _SERVICE_POSITION_WEIGHTS
from fpl_cli.services.matchup import calculate_matchup_score as _service_calculate_matchup_score
from fpl_cli.services.team_form import calculate_team_form
from fpl_cli.services.team_ratings import TeamRatingsService
from fpl_cli.services.team_ratings import general_fdr as _general_fdr


class FixtureAgent(Agent):
    """Agent for fetching and analyzing fixture data.

    Responsibilities:
    - Fetch upcoming fixtures from FPL API
    - Calculate fixture difficulty ratings (FDR)
    - Identify blank gameweeks (teams not playing)
    - Identify double gameweeks (teams playing twice)
    - Analyze fixture runs for planning
    """

    name = "FixtureAgent"
    description = "Fetches and analyzes Premier League fixtures"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        client: FPLClient | None = None,
    ) -> None:
        super().__init__(config)
        self.client = client or FPLClient()
        self._owns_client = client is None
        self.ratings_service = TeamRatingsService()
        self.lookahead_gameweeks = config.get("lookahead_gameweeks", 6) if config else 6
        # FDR mode: "difference" (Ben's preferred) or "opponent"
        self.fdr_mode = config.get("fdr_mode", "difference") if config else "difference"
        self.from_gw: int | None = config.get("from_gw") if config else None
        self.to_gw: int | None = config.get("to_gw") if config else None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Fetch and analyze fixture data.

        Returns:
            AgentResult with fixture analysis including:
            - fixtures: List of upcoming fixtures
            - fdr_by_team: FDR analysis per team
            - blank_gameweeks: Teams with blank GWs
            - double_gameweeks: Teams with double GWs
            - easy_runs: Teams with favorable fixture runs
        """
        self.log("Fetching fixture data...")
        await self.ratings_service.ensure_fresh(self.client)

        try:
            # Get current/next gameweek
            next_gw = await self.client.get_next_gameweek()
            if not next_gw:
                return self._create_result(
                    AgentStatus.FAILED,
                    message="Could not determine next gameweek",
                    errors=["Season may have ended or not started"],
                )

            current_gw = next_gw["id"]

            # Resolve GW window: explicit from_gw/to_gw override defaults
            start_gw = self.from_gw if self.from_gw is not None else current_gw
            end_gw = (
                self.to_gw
                if self.to_gw is not None
                else min(start_gw + self.lookahead_gameweeks, TOTAL_GAMEWEEKS)
            )
            self.log(f"Analyzing GW{start_gw}-{end_gw}")

            # Fetch all fixtures
            all_fixtures = await self.client.get_fixtures()
            teams = await self.client.get_teams()
            team_map = {t.id: t for t in teams}

            # Filter to target GW window
            upcoming_fixtures = [
                f for f in all_fixtures
                if f.gameweek is not None and start_gw <= f.gameweek <= end_gw
            ]

            # Analyze fixtures
            fixtures_by_gw = self._group_by_gameweek(upcoming_fixtures)
            blank_gws = find_blank_gameweeks(fixtures_by_gw, teams, start_gw, end_gw)
            double_gws = find_double_gameweeks(fixtures_by_gw, teams)
            fdr_analysis = self._analyze_fdr(upcoming_fixtures, team_map, start_gw, end_gw)
            easy_runs = self._find_easy_runs(fdr_analysis, team_map)
            team_form = calculate_team_form(all_fixtures, teams)

            n_fixtures = len(upcoming_fixtures)
            n_gws = end_gw - start_gw
            self.log_success(f"Analyzed {n_fixtures} fixtures across {n_gws} gameweeks")

            result_data: dict[str, Any] = {
                "current_gameweek": current_gw,
                # Every FDR figure below (general, ATK, DEF) is scored in this mode
                "fdr_mode": self.fdr_mode,
                "fixtures": [self._fixture_to_dict(f, team_map) for f in upcoming_fixtures],
                "fixtures_by_gameweek": {
                    gw: [self._fixture_to_dict(f, team_map) for f in fixtures]
                    for gw, fixtures in fixtures_by_gw.items()
                },
                "fdr_by_team": fdr_analysis,
                "blank_gameweeks": blank_gws,
                "double_gameweeks": double_gws,
                "easy_fixture_runs": easy_runs,
                "team_form": team_form,
                # Rating-quality warning from the service this agent refreshed
                # and team-set checked. Sourced here rather than from a second
                # TeamRatingsService in the CLI, which would load the file
                # again without either check and warn about the wrong things.
                "ratings_warning": self.ratings_service.get_staleness_warning(),
            }

            if context and "squad" in context:
                pred_service = FixturePredictionsService()
                result_data["predictions_stale"] = pred_service.is_stale
                # Skipped prediction files (unreadable, malformed, empty, stale):
                # surfaced in the result so consumers don't silently lose BGW/DGW
                # context when a user override is broken.
                result_data["prediction_warnings"] = pred_service.load_warnings
                result_data["squad_exposure"] = self._analyze_squad_exposure(
                    squad=context["squad"],
                    blank_gws=blank_gws,
                    double_gws=double_gws,
                    teams=teams,
                    predicted_blanks=pred_service.get_predicted_blanks(min_gw=current_gw),
                    predicted_doubles=pred_service.get_predicted_doubles(min_gw=current_gw),
                )

            return self._create_result(
                AgentStatus.SUCCESS,
                data=result_data,
                message=f"Fixture analysis complete for GW{start_gw}-{end_gw}",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to fetch fixtures: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to fetch fixture data",
                errors=[str(e)],
            )

    def _group_by_gameweek(self, fixtures: list[Fixture]) -> dict[int, list[Fixture]]:
        """Group fixtures by gameweek."""
        by_gw: dict[int, list[Fixture]] = defaultdict(list)
        for f in fixtures:
            if f.gameweek is not None:
                by_gw[f.gameweek].append(f)
        return dict(by_gw)

    def _analyze_squad_exposure(
        self,
        squad: list[dict[str, Any]],
        blank_gws: dict[int, list[BlankTeamInfo]],
        double_gws: dict[int, list[DoubleTeamInfo]],
        teams: list[Team],
        predicted_blanks: list[BlankPrediction] | None = None,
        predicted_doubles: list[DoublePrediction] | None = None,
    ) -> list[dict[str, Any]]:
        """Analyze squad exposure to blank and double gameweeks.

        Formation-aware starter projection uses per-position caps:
        min(gk,1) + min(def,5) + min(mid,5) + min(fwd,3), capped at 11.

        Args:
            squad: Player dicts with team_id, element_type (1=GK,2=DEF,3=MID,4=FWD), web_name
            blank_gws: Confirmed blank GWs {gw: [{team_id, ...}]}
            double_gws: Confirmed double GWs {gw: [{team_id, ...}]}
            teams: Team objects for short_name -> id resolution
            predicted_blanks: BlankPrediction list; skip entries with teams=[]
            predicted_doubles: DoublePrediction list; skip entries with teams=[]

        Returns:
            List of exposure dicts sorted by GW, one per event with squad overlap.
        """
        short_to_id: dict[str, int] = {t.short_name: t.id for t in teams}
        total = len(squad)

        # Collect events: (gw, type, team_ids_set, source)
        events: list[tuple[int, str, set[int], str]] = []

        for gw, teams_list in blank_gws.items():
            team_ids = {t["team_id"] for t in teams_list}
            events.append((int(gw), "blank", team_ids, "confirmed"))

        for gw, teams_list in double_gws.items():
            team_ids = {t["team_id"] for t in teams_list}
            events.append((int(gw), "double", team_ids, "confirmed"))

        confirmed_teams: dict[tuple[int, str], set[int]] = {}
        for gw, teams_list in blank_gws.items():
            confirmed_teams[(int(gw), "blank")] = {t["team_id"] for t in teams_list}
        for gw, teams_list in double_gws.items():
            confirmed_teams[(int(gw), "double")] = {t["team_id"] for t in teams_list}

        for pred in (predicted_blanks or []):
            if not pred.teams:
                continue
            team_ids = {short_to_id[s] for s in pred.teams if s in short_to_id}
            remaining = team_ids - confirmed_teams.get((pred.gameweek, "blank"), set())
            if remaining:
                events.append((pred.gameweek, "blank", remaining, "predicted"))

        for pred in (predicted_doubles or []):
            if not pred.teams:
                continue
            team_ids = {short_to_id[s] for s in pred.teams if s in short_to_id}
            remaining = team_ids - confirmed_teams.get((pred.gameweek, "double"), set())
            if remaining:
                events.append((pred.gameweek, "double", remaining, "predicted"))

        pos_caps = {1: 1, 2: 5, 3: 5, 4: 3}
        exposure: list[dict[str, Any]] = []

        for gw, gw_type, affected_ids, source in sorted(events, key=lambda e: (e[0], e[1])):
            affected = [p for p in squad if p["team_id"] in affected_ids]
            if not affected:
                continue

            pos_counts: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}
            for p in affected:
                et = p.get("element_type", 3)
                if et in pos_counts:
                    pos_counts[et] += 1

            projected = min(
                sum(min(pos_counts[et], cap) for et, cap in pos_caps.items()),
                11,
            )

            exposure.append({
                "gw": gw,
                "type": gw_type,
                "affected": len(affected),
                "total": total,
                "starters": projected,
                "players": [p["web_name"] for p in affected],
                "source": source,
            })

        return exposure

    def _analyze_fdr(
        self,
        fixtures: list[Fixture],
        team_map: dict[int, Any],
        start_gw: int,
        end_gw: int,
    ) -> dict[str, dict[str, Any]]:
        """Analyze fixture difficulty ratings for each team.

        Every figure is scored in the agent's ``fdr_mode`` at the fixture's venue:
        - fdr_atk: For attackers (FWD/MID) based on opponent's defensive weakness
        - fdr_def: For defenders (DEF/GK) based on opponent's offensive threat
        - fdr: General FDR, the mean of fdr_atk and fdr_def

        Each team also carries ``fdr_by_gameweek``: the mean general FDR of its
        fixtures in each gameweek, with the count they were averaged over.
        """
        fdr_by_team: dict[int, list[dict[str, Any]]] = defaultdict(list)

        for f in fixtures:
            if f.gameweek is None:
                continue

            home_team = team_map.get(f.home_team_id)
            away_team = team_map.get(f.away_team_id)

            if home_team and away_team:
                # Positional FDR in the agent's mode
                home_pos_fdr = self.get_fixture_fdr_by_position(
                    team_short=home_team.short_name,
                    opponent_short=away_team.short_name,
                    is_home=True,
                )
                away_pos_fdr = self.get_fixture_fdr_by_position(
                    team_short=away_team.short_name,
                    opponent_short=home_team.short_name,
                    is_home=False,
                )

                # General FDR is the ATK/DEF mean, so it sits on the same model
                # and venue as the two columns shown beside it and the "overall"
                # ranking agrees with them (#186). The opponent-only,
                # venue-blind avg_overall_fdr it replaced ranked the league's
                # weakest side above its strongest because it could see neither
                # team; `fpl fixtures` read the same figure until #202.
                # No API-difficulty fallback for unrated clubs: that put their
                # fixtures on the API's 1-5 scale inside a 1-7 ranking, so they
                # floated up the easiest-runs table. They score the same
                # neutral 4.0 the positional columns already give them.
                home_fdr = self.general_fdr(home_pos_fdr)
                away_fdr = self.general_fdr(away_pos_fdr)

                fdr_by_team[f.home_team_id].append({
                    "gameweek": f.gameweek,
                    "opponent": away_team.short_name,
                    "opponent_id": f.away_team_id,
                    "is_home": True,
                    "fdr": home_fdr,
                    "fdr_atk": home_pos_fdr["ATK"],
                    "fdr_def": home_pos_fdr["DEF"],
                })

                fdr_by_team[f.away_team_id].append({
                    "gameweek": f.gameweek,
                    "opponent": home_team.short_name,
                    "opponent_id": f.home_team_id,
                    "is_home": False,
                    "fdr": away_fdr,
                    "fdr_atk": away_pos_fdr["ATK"],
                    "fdr_def": away_pos_fdr["DEF"],
                })

        # Calculate average FDR and format output
        # Average is per-GW (sum FDR within each GW, then average across GWs) so that
        # DGW teams are rewarded rather than diluted.
        result = {}
        for team_id, fixtures_list in fdr_by_team.items():
            team = team_map.get(team_id)
            if not team:
                continue

            gw_fdr: dict[int, dict[str, float]] = defaultdict(
                lambda: {"fdr": 0.0, "fdr_atk": 0.0, "fdr_def": 0.0, "count": 0.0}
            )
            for f in fixtures_list:
                gw_fdr[f["gameweek"]]["fdr"] += f["fdr"]
                gw_fdr[f["gameweek"]]["fdr_atk"] += f["fdr_atk"]
                gw_fdr[f["gameweek"]]["fdr_def"] += f["fdr_def"]
                gw_fdr[f["gameweek"]]["count"] += 1
            n_gws = len(gw_fdr) or 1
            avg_fdr = sum(v["fdr"] for v in gw_fdr.values()) / n_gws
            avg_fdr_atk = sum(v["fdr_atk"] for v in gw_fdr.values()) / n_gws
            avg_fdr_def = sum(v["fdr_def"] for v in gw_fdr.values()) / n_gws
            fixtures_list.sort(key=lambda x: x["gameweek"])

            result[team.short_name] = {
                "team_id": team_id,
                "team_name": team.name,
                "fixtures": fixtures_list,
                "average_fdr": round(avg_fdr, 2),
                "average_fdr_atk": round(avg_fdr_atk, 2),
                "average_fdr_def": round(avg_fdr_def, 2),
                # Per-gameweek breakdown, kept rather than discarded so a caller
                # asking "how hard is this team's gameweek?" (a DGW Triple
                # Captain candidate, say) reads it from the module that owns the
                # shape instead of re-deriving the mean from `fixtures`. Note
                # `fdr` here is the MEAN across the gameweek's fixtures, where
                # `average_fdr` above sums within a gameweek before averaging
                # across them - that deliberately rewards a DGW team over the
                # season-long run, which is the wrong lens for one gameweek.
                # `fixture_count` lets a caller see a gameweek scored on fewer
                # fixtures than it expects.
                "fdr_by_gameweek": {
                    gw: {
                        "fdr": round(v["fdr"] / v["count"], 2),
                        "fixture_count": int(v["count"]),
                    }
                    for gw, v in sorted(gw_fdr.items())
                },
                "fixture_count": len(fixtures_list),
            }

        return result

    def _find_easy_runs(
        self,
        fdr_analysis: dict[str, dict[str, Any]],
        team_map: dict[int, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        """Find teams with the easiest upcoming fixture runs.

        Returns:
            Dict with "overall", "for_attackers" (FWD/MID), and "for_defenders" (DEF/GK)
        """
        teams_data = [
            {
                "short_name": short_name,
                "team_name": data["team_name"],
                "average_fdr": data["average_fdr"],
                "average_fdr_atk": data.get("average_fdr_atk", data["average_fdr"]),
                "average_fdr_def": data.get("average_fdr_def", data["average_fdr"]),
                "fixture_count": data["fixture_count"],
                "fixtures_summary": " ".join(
                    f["opponent"].upper() if f["is_home"] else f["opponent"].lower()
                    for f in data["fixtures"][:6]
                ),
            }
            for short_name, data in fdr_analysis.items()
        ]

        # Sort by overall average FDR (lowest = easiest)
        overall = sorted(teams_data, key=lambda x: x["average_fdr"])[:10]

        # Sort by ATK FDR (best for FWD/MID)
        for_attackers = sorted(teams_data, key=lambda x: x["average_fdr_atk"])[:10]

        # Sort by DEF FDR (best for DEF/GK)
        for_defenders = sorted(teams_data, key=lambda x: x["average_fdr_def"])[:10]

        return {
            "overall": overall,
            "for_attackers": for_attackers,
            "for_defenders": for_defenders,
        }

    def _fixture_to_dict(self, fixture: Fixture, team_map: dict[int, Any]) -> dict[str, Any]:
        """Convert fixture to dictionary with team names and positional FDR.

        ``home_fdr`` / ``away_fdr`` are the same general FDR ``_analyze_fdr``
        records, so one fixture reads identically here and in ``fdr_by_team``.
        Only a team missing from ``team_map`` altogether (so no short name to
        rate) keeps the FPL API's difficulty; ``_analyze_fdr`` skips those.
        """
        home = team_map.get(fixture.home_team_id)
        away = team_map.get(fixture.away_team_id)

        result: dict[str, Any] = {
            "id": fixture.id,
            "gameweek": fixture.gameweek,
            "home_team": home.short_name if home else "???",
            "home_team_id": fixture.home_team_id,
            "away_team": away.short_name if away else "???",
            "away_team_id": fixture.away_team_id,
            # FPL API difficulty, replaced below when both teams are known
            "home_fdr": fixture.home_difficulty,
            "away_fdr": fixture.away_difficulty,
            "kickoff": fixture.kickoff_time.isoformat() if fixture.kickoff_time else None,
            "finished": fixture.finished,
        }

        # Positional FDR, and the general FDR as their mean, when both teams are known
        if home and away:
            home_pos_fdr = self.get_fixture_fdr_by_position(
                team_short=home.short_name,
                opponent_short=away.short_name,
                is_home=True,
            )
            away_pos_fdr = self.get_fixture_fdr_by_position(
                team_short=away.short_name,
                opponent_short=home.short_name,
                is_home=False,
            )
            result["home_fdr_atk"] = home_pos_fdr["ATK"]
            result["home_fdr_def"] = home_pos_fdr["DEF"]
            result["away_fdr_atk"] = away_pos_fdr["ATK"]
            result["away_fdr_def"] = away_pos_fdr["DEF"]
            result["home_fdr"] = self.general_fdr(home_pos_fdr)
            result["away_fdr"] = self.general_fdr(away_pos_fdr)

        return result

    @staticmethod
    def general_fdr(positional_fdr: dict[str, float]) -> float:
        """General FDR for a fixture, from the ATK/DEF pair already scored for it.

        The service owns the definition -- `fpl fixtures` reaches the same
        number through `get_fixture_fdr()`, which is this call with the pair
        fetched first -- so a fixture is scored one way in the codebase (#202).

        Args:
            positional_fdr: ``get_fixture_fdr_by_position`` output for the fixture
        """
        return _general_fdr(positional_fdr)

    def get_positional_fdr(
        self,
        position: str,
        team_short: str,
        opponent_short: str,
        is_home: bool,
        mode: str | None = None,
    ) -> float:
        """Get position-specific FDR using team ratings.

        Args:
            position: Player position ("FWD", "MID", "DEF", "GK")
            team_short: Player's team short name (e.g., "LIV")
            opponent_short: Opponent team short name (e.g., "ARS")
            is_home: Whether player's team is at home
            mode: "difference" or "opponent" (defaults to self.fdr_mode)

        Returns:
            FDR value (1-7 scale, lower = easier fixture)
        """
        resolved_mode: str = mode or self.fdr_mode
        venue = "home" if is_home else "away"
        return self.ratings_service.get_positional_fdr(
            position=position,
            team=team_short,
            opponent=opponent_short,
            venue=venue,
            mode=resolved_mode,
        )

    def get_fixture_fdr_by_position(
        self,
        team_short: str,
        opponent_short: str,
        is_home: bool,
        mode: str | None = None,
    ) -> dict[str, float]:
        """Get FDR for all position groups for a fixture.

        Args:
            team_short: Player's team short name
            opponent_short: Opponent team short name
            is_home: Whether player's team is at home
            mode: "difference" or "opponent"

        Returns:
            Dict with keys "ATK" (FWD/MID) and "DEF" (DEF/GK)
        """
        resolved_mode: str = mode or self.fdr_mode
        return self.ratings_service.get_positional_fdr_pair(
            team=team_short,
            opponent=opponent_short,
            venue="home" if is_home else "away",
            mode=resolved_mode,
        )

    # Position-specific weights for matchup scoring (delegated to service)
    POSITION_WEIGHTS = _SERVICE_POSITION_WEIGHTS

    def calculate_matchup_score(
        self,
        player_team_form: dict[str, Any],
        opponent_form: dict[str, Any],
        position: str,
        is_home: bool,
    ) -> dict[str, Any]:
        """Calculate position-weighted matchup score with component breakdown.

        Delegates to ``fpl_cli.services.matchup.calculate_matchup_score``.
        """
        return _service_calculate_matchup_score(
            player_team_form, opponent_form, position, is_home
        )
