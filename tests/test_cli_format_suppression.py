"""Tests for Phase 4: format-aware output suppression in shared commands."""

from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli._context import CLIContext, Format

# ---------------------------------------------------------------------------
# league.py - section gating
# ---------------------------------------------------------------------------

class TestLeagueFormatSuppression:
    """League command skips irrelevant format sections."""

    def _mock_fpl_client(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_current_gameweek = AsyncMock(return_value={"id": 25, "finished": True})
        client.get_classic_league_standings = AsyncMock(return_value={
            "league": {"name": "Test League"},
            "standings": {"results": []},
        })
        client.get_manager_picks = AsyncMock(return_value={
            "entry_history": {"event_transfers_cost": 0},
        })
        return client

    def _mock_draft_client(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_league_details = AsyncMock(return_value={
            "league": {"name": "Draft League"},
            "standings": [],
            "league_entries": [],
        })
        return client

    def _settings_both(self):
        return {"fpl": {
            "classic_entry_id": 1, "classic_league_id": 100,
            "draft_league_id": 200, "draft_entry_id": 2,
        }}

    def test_draft_format_skips_classic_section(self):
        from fpl_cli.cli.league import league_command

        with (
            patch("fpl_cli.cli.league.load_settings", return_value=self._settings_both()),
            patch("fpl_cli.api.fpl.FPLClient", return_value=self._mock_fpl_client()),
            patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=self._mock_draft_client()),
        ):
            runner = CliRunner()
            result = runner.invoke(
                league_command, [],
                obj=CLIContext(format=Format.DRAFT, settings={}),
            )

        assert result.exit_code == 0, result.output
        assert "Classic League" not in result.output
        assert "Set classic_league_id" not in result.output

    def test_classic_format_skips_draft_section(self):
        from fpl_cli.cli.league import league_command

        with (
            patch("fpl_cli.cli.league.load_settings", return_value=self._settings_both()),
            patch("fpl_cli.api.fpl.FPLClient", return_value=self._mock_fpl_client()),
            patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=self._mock_draft_client()),
        ):
            runner = CliRunner()
            result = runner.invoke(
                league_command, [],
                obj=CLIContext(format=Format.CLASSIC, settings={}),
            )

        assert result.exit_code == 0, result.output
        assert "Draft League" not in result.output
        assert "Set draft_league_id" not in result.output

    def test_no_context_shows_all(self):
        """Without ctx.obj (legacy invocation), both sections are shown."""
        from fpl_cli.cli.league import league_command

        with (
            patch("fpl_cli.cli.league.load_settings", return_value=self._settings_both()),
            patch("fpl_cli.api.fpl.FPLClient", return_value=self._mock_fpl_client()),
            patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=self._mock_draft_client()),
        ):
            runner = CliRunner()
            # No obj= passed - ctx.obj is None
            result = runner.invoke(league_command, [])

        assert result.exit_code == 0, result.output
        # Both sections should appear (or at least not be blocked)


# ---------------------------------------------------------------------------
# status.py - draft column suppression
# ---------------------------------------------------------------------------

class TestStatusFormatSuppression:
    """Status command no longer shows form table or draft ownership (moved to stats)."""

    def _mock_fpl_client(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_current_gameweek = AsyncMock(return_value={"id": 25, "finished": False})
        client.get_next_gameweek = AsyncMock(return_value={"id": 26, "deadline_time": "2026-03-28T11:30:00Z"})
        client.get_players = AsyncMock(return_value=[])
        client.get_teams = AsyncMock(return_value=[])
        client.get_manager_entry = AsyncMock(return_value={})
        client.get_manager_picks = AsyncMock(return_value={"picks": []})
        client.get_manager_history = AsyncMock(return_value={"current": [], "chips": []})
        return client

    def test_no_form_table_or_draft_column(self):
        from fpl_cli.cli.status import status_command

        with (
            patch("fpl_cli.cli.status.load_settings", return_value={
                "fpl": {"draft_league_id": 999},
            }),
            patch("fpl_cli.api.fpl.FPLClient", return_value=self._mock_fpl_client()),
        ):
            runner = CliRunner()
            result = runner.invoke(
                status_command, [],
                obj=CLIContext(format=Format.DRAFT, settings={}),
            )

        assert result.exit_code == 0, result.output
        assert "Top Players by Form" not in result.output
        assert "Draft" not in result.output


# ---------------------------------------------------------------------------
# fdr.py - format-aware --my-squad
# ---------------------------------------------------------------------------

class TestFdrFormatAwareSquad:
    """fdr --my-squad auto-selects squad source based on format."""

    def _mock_fixture_agent(self):
        agent = MagicMock()
        agent.__aenter__ = AsyncMock(return_value=agent)
        agent.__aexit__ = AsyncMock(return_value=False)
        result = MagicMock()
        result.success = True
        result.data = {
            "current_gameweek": 25,
            "easy_fixture_runs": {"overall": [], "for_attackers": [], "for_defenders": []},
            "blank_gameweeks": {},
            "double_gameweeks": {},
            "squad_exposure": [],
        }
        agent.run = AsyncMock(return_value=result)
        return agent

    def _mock_draft_client(self, picks_data, bootstrap_elements, current_gw=25):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_game_state = AsyncMock(return_value={"current_event": current_gw})
        client.get_bootstrap_static = AsyncMock(return_value={"elements": bootstrap_elements})
        client.get_entry_picks = AsyncMock(return_value=picks_data)
        return client

    def test_draft_format_auto_selects_draft_squad(self):
        """--my-squad in draft format uses draft squad without --draft flag."""
        from fpl_cli.cli.fdr import fdr_command
        from tests.conftest import make_draft_player

        player_a = make_draft_player(id=10, web_name="Salah", team=14, element_type=3)
        picks_data = {"picks": [{"element": 10}]}
        draft_client = self._mock_draft_client(picks_data, [player_a])
        fixture_agent = self._mock_fixture_agent()

        captured_context: list = []
        original_run = fixture_agent.run

        async def capture_run(context=None):
            captured_context.append(context)
            return await original_run(context=context)

        fixture_agent.run = AsyncMock(side_effect=capture_run)

        with (
            patch("fpl_cli.cli.fdr.is_custom_analysis_enabled", return_value=True),
            patch("fpl_cli.cli.fdr.load_settings", return_value={"fpl": {"draft_entry_id": 999}}),
            patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=draft_client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.services.team_ratings.TeamRatingsService") as mock_ratings,
            patch("fpl_cli.services.fixture_predictions.FixturePredictionsService") as mock_preds,
        ):
            mock_ratings.return_value.get_staleness_warning.return_value = None
            mock_preds.return_value.get_predicted_blanks.return_value = []
            mock_preds.return_value.get_predicted_doubles.return_value = []

            runner = CliRunner()
            # No --draft flag, but format is DRAFT
            result = runner.invoke(
                fdr_command, ["--my-squad"],
                obj=CLIContext(format=Format.DRAFT, settings={}),
            )

        assert result.exit_code == 0, result.output
        assert len(captured_context) == 1
        squad = captured_context[0]["squad"]
        assert len(squad) == 1
        assert squad[0]["web_name"] == "Salah"


# ---------------------------------------------------------------------------
# player.py - selected_by suppression
# ---------------------------------------------------------------------------

class TestPlayerFormatSuppression:
    """Player command suppresses selected_by_percent for draft-only users."""

    def _mock_player(self):
        player = MagicMock()
        player.id = 1
        player.web_name = "Salah"
        player.full_name = "Mohamed Salah"
        player.team_id = 14
        player.position_name = "MID"
        player.price = 13.0
        player.form = 8.5
        player.total_points = 200
        player.points_per_game = 7.5
        player.goals_scored = 15
        player.assists = 10
        player.expected_goals = 14.5
        player.expected_assists = 9.2
        player.selected_by_percent = 45.3
        from fpl_cli.models.player import PlayerStatus
        player.status = PlayerStatus.AVAILABLE
        player.chance_of_playing_next_round = 100
        player.news = ""
        player.minutes = 2000
        player.code = 118748
        player.penalties_order = None
        player.corners_and_indirect_freekicks_order = None
        player.direct_freekicks_order = None
        player.defensive_contribution_per_90 = 0.0
        player.penalties_saved = 0
        return player

    def _mock_team(self):
        team = MagicMock()
        team.id = 14
        team.name = "Liverpool"
        team.short_name = "LIV"
        return team

    def test_draft_format_hides_selected_by(self):
        from fpl_cli.cli.player import player_command

        player = self._mock_player()
        team = self._mock_team()

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_players = AsyncMock(return_value=[player])
        client.get_teams = AsyncMock(return_value=[team])
        client.get_next_gameweek = AsyncMock(return_value={"id": 30})

        with (
            patch("fpl_cli.cli.player.load_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.understat.UnderstatClient") as mock_understat,
        ):
            mock_understat_instance = MagicMock()
            mock_understat_instance.__aenter__ = AsyncMock(return_value=mock_understat_instance)
            mock_understat_instance.__aexit__ = AsyncMock(return_value=False)
            mock_understat_instance.get_league_players = AsyncMock(return_value=[])
            mock_understat.return_value = mock_understat_instance

            runner = CliRunner()
            result = runner.invoke(
                player_command, ["Salah"],
                obj=CLIContext(format=Format.DRAFT, settings={}),
            )

        assert result.exit_code == 0, result.output
        assert "Selected by" not in result.output
        # Price should still be shown
        assert "13.0m" in result.output

    def test_classic_format_shows_selected_by(self):
        from fpl_cli.cli.player import player_command

        player = self._mock_player()
        team = self._mock_team()

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_players = AsyncMock(return_value=[player])
        client.get_teams = AsyncMock(return_value=[team])
        client.get_next_gameweek = AsyncMock(return_value={"id": 30})

        with (
            patch("fpl_cli.cli.player.load_settings", return_value={"fpl": {}}),
            patch("fpl_cli.api.fpl.FPLClient", return_value=client),
            patch("fpl_cli.api.understat.UnderstatClient") as mock_understat,
        ):
            mock_understat_instance = MagicMock()
            mock_understat_instance.__aenter__ = AsyncMock(return_value=mock_understat_instance)
            mock_understat_instance.__aexit__ = AsyncMock(return_value=False)
            mock_understat_instance.get_league_players = AsyncMock(return_value=[])
            mock_understat.return_value = mock_understat_instance

            runner = CliRunner()
            result = runner.invoke(
                player_command, ["Salah"],
                obj=CLIContext(format=Format.CLASSIC, settings={}),
            )

        assert result.exit_code == 0, result.output
        assert "Selected by" in result.output


# ---------------------------------------------------------------------------
# gw_review.md.j2 - template format gating
# ---------------------------------------------------------------------------

class TestReviewTemplateFormatGating:
    """Template conditionally renders Classic/Draft sections."""

    def _render(self, fpl_format: str | None, **kwargs):
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader("templates"))
        template = env.get_template("gw_review.md.j2")
        defaults = {
            "generated_at": "2026-03-23 12:00",
            "fpl_format": fpl_format,
            "points": None,
            "team_points": None,
            "classic_transfers": None,
            "classic_league": None,
            "global_stats": None,
            "draft_squad_points": [{"name": "Salah", "team": "LIV", "position": "MID",
                                     "points": 10, "contributed": True,
                                     "auto_sub_in": False, "auto_sub_out": False, "red_cards": 0}],
            "draft_transactions": None,
            "draft_league": None,
            "fixtures": None,
            "league_table": None,
        }
        defaults.update(kwargs)
        return template.render(**defaults)

    def test_classic_format_excludes_draft(self):
        output = self._render("classic")
        assert "# Classic" in output
        assert "# Draft" not in output

    def test_draft_format_excludes_classic(self):
        output = self._render("draft")
        assert "# Draft" in output
        assert "# Classic" not in output

    def test_both_format_includes_all(self):
        output = self._render("both")
        assert "# Classic" in output
        assert "# Draft" in output

    def test_none_format_includes_all(self):
        output = self._render(None)
        assert "# Classic" in output
        assert "# Draft" in output
