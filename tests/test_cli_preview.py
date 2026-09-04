"""Tests for custom_analysis toggle on preview command."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from fpl_cli.cli.preview import preview_command
from fpl_cli.season import season_label
from tests.conftest import make_agent, make_draft_player, make_player


def _make_fpl_client(gw=25):
    """Minimal FPLClient mock for preview tests."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_next_gameweek = AsyncMock(return_value={"id": gw, "deadline_time": "2026-04-05T11:00:00Z"})
    client.get_players = AsyncMock(return_value=[])
    client.get_teams = AsyncMock(return_value=[])
    client.get_fixtures = AsyncMock(return_value=[])
    return client


def _make_agent(success=True, data=None):
    """Create a mock agent with context-manager support."""
    return make_agent(data, success=success, message="" if success else "failed")


def _run_preview(custom_analysis=True, fixture_data=None, stats_data=None):
    """Invoke preview with mocked agents and toggle control."""
    fpl_client = _make_fpl_client()

    fixture_agent = _make_agent(data=fixture_data or {
        "easy_fixture_runs": {
            "overall": [
                {
                    "short_name": "ARS",
                    "average_fdr": 2.1,
                    "average_fdr_atk": 1.9,
                    "average_fdr_def": 2.3,
                    "fixtures_summary": "bou(H), MCI(A), new(H)",
                },
            ],
        },
        "team_form": [],
    })

    stats_agent = _make_agent(data=stats_data or {
        "top_xgi_per_90": [
            {"player_name": "Haaland", "team_short": "MCI", "xG": 12.5, "xA": 3.2,
             "xGI_per_90": 0.95, "goals": 15, "assists": 4},
        ],
        "underperformers": [],
        "value_picks": [
            {"player_name": "Mbeumo", "team_short": "BRE", "price": 6.5,
             "ownership": 8.2, "xGI_per_90": 0.72},
        ],
        "window_label": "last 6 GWs",
    })

    price_agent = _make_agent(data={})

    settings = {"custom_analysis": custom_analysis}

    runner = CliRunner()
    with (
        patch("fpl_cli.cli.preview.is_custom_analysis_enabled", return_value=custom_analysis),
        patch("fpl_cli.cli.preview.get_settings", return_value=settings),
        patch("fpl_cli.api.fpl.FPLClient", return_value=fpl_client),
        patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
        patch("fpl_cli.agents.analysis.stats.StatsAgent", return_value=stats_agent),
        patch("fpl_cli.agents.data.price.PriceAgent", return_value=price_agent),
    ):
        return runner.invoke(preview_command, [])


class TestPreviewCustomAnalysisToggle:
    """Tests for custom_analysis toggle on preview display sections."""

    def test_toggle_off_no_atk_def_columns(self):
        """When toggle off, ATK/DEF columns absent from easy fixtures table."""
        result = _run_preview(custom_analysis=False)
        assert result.exit_code == 0, result.output
        # ATK/DEF should not appear as column headers
        lines = result.output.split("\n")
        header_lines = [line for line in lines if "ATK" in line or "DEF" in line]
        assert len(header_lines) == 0
        # Team name and Avg FDR should still appear
        assert "ARS" in result.output

    def test_toggle_on_has_atk_def_columns(self):
        """When toggle on, ATK/DEF columns present (no regression)."""
        result = _run_preview(custom_analysis=True)
        assert result.exit_code == 0, result.output
        assert "ATK" in result.output
        assert "DEF" in result.output

    def test_toggle_on_footer_names_fdr_mode(self):
        """The easy-fixtures footer says which mode all three columns share (#186)."""
        result = _run_preview(custom_analysis=True)
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        # No mode in the (mocked) result: described as the agent default
        assert "All three columns use difference mode" in flat
        assert "FDR is the mean of ATK and DEF" in flat

    def test_toggle_on_footer_follows_result_mode(self):
        result = _run_preview(custom_analysis=True, fixture_data={
            "fdr_mode": "opponent",
            "easy_fixture_runs": {
                "overall": [
                    {
                        "short_name": "ARS",
                        "average_fdr": 2.1,
                        "average_fdr_atk": 1.9,
                        "average_fdr_def": 2.3,
                        "fixtures_summary": "bou(H), MCI(A), new(H)",
                    },
                ],
            },
            "team_form": [],
        })
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "All three columns use opponent mode (opponent strength at the venue only)" in flat

    def test_ratings_warning_surfaces(self):
        """With no usable ratings every FDR cell is a neutral 4.0, and the preview says so."""
        result = _run_preview(custom_analysis=True, fixture_data={
            "ratings_warning": "⚠️ No team ratings available - every fixture will score a neutral 4.0.",
            "easy_fixture_runs": {"overall": []},
            "team_form": [],
        })
        assert result.exit_code == 0, result.output
        assert "No team ratings available" in result.output

    def test_toggle_off_no_fdr_footer(self):
        """With ATK/DEF hidden there is no column relationship to explain."""
        result = _run_preview(custom_analysis=False)
        assert result.exit_code == 0, result.output
        assert "FDR scale" not in result.output

    def test_toggle_off_no_value_picks(self):
        """When toggle off, Value Picks section absent from performance stats."""
        result = _run_preview(custom_analysis=False)
        assert result.exit_code == 0, result.output
        assert "Value Picks" not in result.output
        assert "Mbeumo" not in result.output

    def test_toggle_on_has_value_picks(self):
        """When toggle on, Value Picks section present (no regression)."""
        result = _run_preview(custom_analysis=True)
        assert result.exit_code == 0, result.output
        assert "Value Picks" in result.output or "Mbeumo" in result.output

    def test_toggle_off_raw_data_still_shown(self):
        """When toggle off, raw data sections (xGI/90, underperformers) still shown."""
        result = _run_preview(custom_analysis=False)
        assert result.exit_code == 0, result.output
        assert "Haaland" in result.output
        assert "xGI/90" in result.output

    def test_custom_on_gw_fixtures_uses_agent_fdr(self):
        """When custom_analysis on and FixtureAgent has fixtures_by_gameweek, FDR comes from agent."""
        fixture_data = {
            "easy_fixture_runs": {"overall": []},
            "team_form": [],
            "fixtures_by_gameweek": {
                25: [
                    {
                        "home_team": "ARS",
                        "home_fdr": 5.5,
                        "away_team": "MCI",
                        "away_fdr": 3.2,
                        "kickoff": "2026-04-20T14:00:00",
                        "finished": False,
                    }
                ]
            },
        }
        result = _run_preview(custom_analysis=True, fixture_data=fixture_data)
        assert result.exit_code == 0, result.output
        assert "5.5" in result.output
        assert "3.2" in result.output

    def test_custom_off_gw_fixtures_uses_api_fdr(self):
        """When custom_analysis off, gw_fixtures FDR falls back to FPL API (get_fixtures called)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from click.testing import CliRunner

        fpl_client = _make_fpl_client()
        raw_fixture = MagicMock()
        raw_fixture.home_team_id = 1
        raw_fixture.away_team_id = 2
        raw_fixture.home_difficulty = 3
        raw_fixture.away_difficulty = 4
        raw_fixture.kickoff_time = None
        fpl_client.get_fixtures = AsyncMock(return_value=[raw_fixture])

        fixture_agent = _make_agent(data={"easy_fixture_runs": {"overall": []}, "team_form": []})
        stats_agent = _make_agent(data={
            "top_xgi_per_90": [], "underperformers": [], "value_picks": [], "window_label": "last 6 GWs"
        })
        price_agent = _make_agent(data={})
        settings = {"custom_analysis": False}

        runner = CliRunner()
        with (
            patch("fpl_cli.cli.preview.is_custom_analysis_enabled", return_value=False),
            patch("fpl_cli.cli.preview.get_settings", return_value=settings),
            patch("fpl_cli.api.fpl.FPLClient", return_value=fpl_client),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
            patch("fpl_cli.agents.analysis.stats.StatsAgent", return_value=stats_agent),
            patch("fpl_cli.agents.data.price.PriceAgent", return_value=price_agent),
        ):
            result = runner.invoke(preview_command, [])

        assert result.exit_code == 0, result.output
        fpl_client.get_fixtures.assert_awaited_once()


def _run_scout_preview(tmp_path):
    """Invoke `preview --scout` with a mocked scout agent and a temp research dir."""
    fpl_client = _make_fpl_client()
    fixture_agent = _make_agent(data={"easy_fixture_runs": {"overall": []}, "team_form": []})
    stats_agent = _make_agent(data={"top_xgi_per_90": [], "underperformers": [], "value_picks": []})
    price_agent = _make_agent(data={})
    scout_agent = _make_agent(data={
        "content_referenced": "Scout body [1]",
        "content_clean": "Scout body",
        "citations": ["https://example.test/a"],
    })

    settings = {
        "custom_analysis": True,
        "reports": {"research_dir": str(tmp_path / "02_Research")},
        # `--scout` pre-flights the research provider before any agent runs.
        "llm": {"research": {"provider": "perplexity"}},
    }

    runner = CliRunner()
    with (
        patch("fpl_cli.cli.preview.is_custom_analysis_enabled", return_value=True),
        patch("fpl_cli.cli.preview.get_settings", return_value=settings),
        patch("fpl_cli.api.fpl.FPLClient", return_value=fpl_client),
        patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=fixture_agent),
        patch("fpl_cli.agents.analysis.stats.StatsAgent", return_value=stats_agent),
        patch("fpl_cli.agents.data.price.PriceAgent", return_value=price_agent),
        patch("fpl_cli.agents.data.scout.ScoutAgent", return_value=scout_agent),
    ):
        # The pre-flight provider check runs before any agent is constructed,
        # so it needs a key even though the agent itself is mocked out.
        return runner.invoke(preview_command, ["--scout"], env={"PERPLEXITY_API_KEY": "test-key"})


class TestScoutReportsAreSeasonPartitioned:
    """`gw{N}-scout-preview.md` carries no season either (#85), so the season
    sits between the source directory and the file."""

    def test_scout_reports_land_under_the_season_directory(self, tmp_path):
        result = _run_scout_preview(tmp_path)

        assert result.exit_code == 0, result.output
        scout_dir = tmp_path / "02_Research" / "ai-scout-reports" / season_label()
        assert (scout_dir / "gw25-scout-preview.md").exists()
        assert (scout_dir / "gw25-scout-preview-referenced.md").exists()

    def test_the_source_directory_itself_stays_empty_of_reports(self, tmp_path):
        """The partition goes below `ai-scout-reports/`, leaving sibling
        research sources free to carry their own."""
        result = _run_scout_preview(tmp_path)

        assert result.exit_code == 0, result.output
        assert not list((tmp_path / "02_Research" / "ai-scout-reports").glob("*.md"))

    def test_frontmatter_names_the_season(self, tmp_path):
        """A file that names its own season can be identified after a move,
        without inferring it from the path."""
        result = _run_scout_preview(tmp_path)

        assert result.exit_code == 0, result.output
        report = (
            tmp_path / "02_Research" / "ai-scout-reports" / season_label() / "gw25-scout-preview.md"
        )
        assert f"season: {season_label()}" in report.read_text(encoding="utf-8")


class TestPreviewDraftSquadMatching:
    """#168: the draft squad's Status column comes from the matched main-game
    player, so a name the main game changed must not read as fit."""

    @staticmethod
    def _draft_client(draft_elements):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_bootstrap_static = AsyncMock(return_value={
            "elements": draft_elements,
            "teams": [{"id": 19, "short_name": "MCI", "name": "Man City"}],
        })
        client.get_entry_picks = AsyncMock(
            return_value={"picks": [{"element": 403, "position": 1}]},
        )
        return client

    def _run(self, draft_elements, main_players):
        fpl_client = _make_fpl_client()
        fpl_client.get_players = AsyncMock(return_value=main_players)
        settings = {
            "custom_analysis": True,
            "fpl": {"draft_league_id": 1, "draft_entry_id": 1},
        }
        runner = CliRunner()
        with (
            patch("fpl_cli.cli.preview.is_custom_analysis_enabled", return_value=True),
            patch("fpl_cli.cli.preview.get_settings", return_value=settings),
            patch("fpl_cli.api.fpl.FPLClient", return_value=fpl_client),
            patch("fpl_cli.api.fpl_draft.FPLDraftClient", return_value=self._draft_client(draft_elements)),
            patch("fpl_cli.agents.data.fixture.FixtureAgent", return_value=_make_agent(data={})),
            patch("fpl_cli.agents.analysis.stats.StatsAgent", return_value=_make_agent(data={})),
            patch("fpl_cli.agents.data.price.PriceAgent", return_value=_make_agent(data={})),
        ):
            return runner.invoke(preview_command, [])

    def test_renamed_draft_player_carries_the_main_games_injury_news(self):
        """The draft game kept `Savinho` after the main game moved to `Sávio`.
        Matching on the shared code is what surfaces his doubt."""
        renamed = make_draft_player(id=403, code=510281, web_name="Savinho", team=19, element_type=3)
        main_player = make_player(
            id=403, code=510281, web_name="Sávio", team_id=19,
            chance_of_playing_next_round=25, news="Knock",
        )

        result = self._run([renamed], [main_player])

        assert result.exit_code == 0
        assert "Savinho" in result.output
        assert "25%" in result.output

    def test_a_player_neither_key_resolves_is_left_unannotated(self):
        """The fallback must not invent a match: an element the main game has
        no row for keeps the bare tick it has always had."""
        stranger = make_draft_player(id=403, code=999999, web_name="Mystery", team=19, element_type=3)
        main_player = make_player(
            id=1, code=510281, web_name="Sávio", team_id=19,
            chance_of_playing_next_round=25, news="Knock",
        )

        result = self._run([stranger], [main_player])

        assert result.exit_code == 0
        assert "Mystery" in result.output
        assert "25%" not in result.output
