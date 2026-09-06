"""Tests for ReportAgent."""

from pathlib import Path

from fpl_cli.agents.base import AgentStatus
from fpl_cli.agents.orchestration.report import ReportAgent

# ---------------------------------------------------------------------------
# Minimal data helpers
# ---------------------------------------------------------------------------

def _preview_data() -> dict:
    return {
        "gw_fixtures": [
            {
                "home_team": "LIV",
                "home_fdr": 2,
                "away_fdr": 4,
                "away_team": "ARS",
                "kickoff": "Sat 15:00",
            }
        ],
        "deadline": "Fri 18:30",
        "my_squad": [
            {
                "name": "Salah",
                "team": "LIV",
                "fixture": "ARS",
                "position": "MID",
                "form": 7.5,
                "ownership": 45.2,
                "status": "Available",
            }
        ],
        "draft_squad": [
            {
                "name": "Saka",
                "team": "ARS",
                "fixture": "liv",
                "position": "MID",
                "form": 6.2,
                "status": "✓",
            }
        ],
        "prices": {
            "risers_this_gw": [
                {
                    "name": "Palmer",
                    "team": "CHE",
                    "current_price": 5.6,
                    "change_this_gw": 0.1,
                }
            ]
        },
    }


def _review_data() -> dict:
    return {
        "points": {
            "total": 72,
            "rank": 50000,
            "overall_rank": 120000,
            "average": 55,
            "highest": 143,
        },
        "team_points": [
            {
                "name": "Salah",
                "team": "LIV",
                "position": "MID",
                "display_points": 14,
                "is_captain": True,
                "is_triple_captain": False,
                "is_vice_active": False,
                "contributed": True,
                "auto_sub_in": False,
                "auto_sub_out": False,
                "red_cards": 0,
            },
            {
                "name": "Saka",
                "team": "ARS",
                "position": "MID",
                "display_points": 6,
                "is_captain": False,
                "is_triple_captain": False,
                "is_vice_active": False,
                "contributed": True,
                "auto_sub_in": False,
                "auto_sub_out": False,
                "red_cards": 0,
            },
        ],
        "classic_transfers": [
            {
                "player_in": "Salah",
                "player_in_team": "LIV",
                "player_in_points": 14,
                "player_out": "Saka",
                "player_out_team": "ARS",
                "player_out_points": 6,
                "net": 8,
                "verdict": "Hit",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Group 1: run() contract
# ---------------------------------------------------------------------------

class TestRunContract:
    async def test_no_context_returns_failed(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run(None)
        assert result.status == AgentStatus.FAILED
        assert "No context" in result.message

    async def test_missing_gameweek_returns_failed(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run({"report_type": "preview", "data": {}})
        assert result.status == AgentStatus.FAILED
        assert "gameweek" in result.message.lower()

    async def test_unknown_report_type_returns_failed(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run({"report_type": "summary", "gameweek": 29, "data": {}})
        assert result.status == AgentStatus.FAILED
        assert "Unknown report type" in result.message

    async def test_valid_preview_writes_file(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run({
            "report_type": "preview",
            "gameweek": 29,
            "data": _preview_data(),
        })
        assert result.status == AgentStatus.SUCCESS
        report_path = Path(result.data["report_path"])
        assert report_path.exists()
        assert report_path.name == "gw29-preview.md"

    async def test_valid_review_writes_file(self, tmp_path):
        agent = ReportAgent(config={"output_dir": str(tmp_path)})
        result = await agent.run({
            "report_type": "review",
            "gameweek": 29,
            "data": _review_data(),
        })
        assert result.status == AgentStatus.SUCCESS
        report_path = Path(result.data["report_path"])
        assert report_path.exists()
        assert report_path.name == "gw29-review.md"


# ---------------------------------------------------------------------------
# Group 2: Template rendering (Jinja2 path)
# ---------------------------------------------------------------------------

class TestTemplateRendering:
    def setup_method(self):
        self.agent = ReportAgent()

    # Preview

    def test_preview_contains_deadline(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "Fri 18:30" in output

    def test_preview_player_name_in_my_squad(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "Salah" in output

    def test_preview_fixture_team_in_table(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "LIV" in output

    def test_preview_price_riser_name_and_price(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "Palmer" in output
        assert "5.6" in output

    # Review

    def test_review_total_points_in_summary(self):
        output = self.agent._generate_review_report(29, _review_data())
        assert "72" in output

    def test_review_captain_marker(self):
        output = self.agent._generate_review_report(29, _review_data())
        assert "Salah (C)" in output

    def test_review_no_captain_marker_for_non_captain(self):
        output = self.agent._generate_review_report(29, _review_data())
        assert "Saka (C)" not in output

    def test_review_triple_captain_marker(self):
        data = _review_data()
        data["team_points"][0]["is_triple_captain"] = True
        output = self.agent._generate_review_report(29, data)
        assert "Salah (TC)" in output

    def test_review_auto_sub_in_marker(self):
        data = _review_data()
        data["team_points"][1]["auto_sub_in"] = True
        data["team_points"][1]["contributed"] = False
        output = self.agent._generate_review_report(29, data)
        assert "[SUB IN]" in output

    def test_review_auto_sub_out_marker(self):
        data = _review_data()
        data["team_points"][1]["auto_sub_out"] = True
        data["team_points"][1]["contributed"] = False
        output = self.agent._generate_review_report(29, data)
        assert "[DIDN'T PLAY]" in output

    def test_review_unused_bench_marker(self):
        data = _review_data()
        data["team_points"][1]["contributed"] = False
        data["team_points"][1]["display_points"] = 8  # >= 6, triggers UNUSED!
        output = self.agent._generate_review_report(29, data)
        assert "[UNUSED!]" in output

    # classic_league section: three states the League heading can render.

    def test_review_league_standings_pending(self):
        data = _review_data()
        data["classic_league"] = {"league_name": "Office League", "standings_pending": True}
        output = self.agent._generate_review_report(29, data)
        assert "Standings not published yet" in output

    def test_review_league_historical_review_explains_absence(self):
        data = _review_data()
        data["classic_league"] = {"league_name": "Office League"}
        output = self.agent._generate_review_report(29, data)
        assert "standings not shown for historical GW29 review" in output
        assert "Position:" not in output.split("## League")[1].split("##")[0]

    def test_review_league_populated_shows_position(self):
        data = _review_data()
        data["classic_league"] = {
            "league_name": "Office League", "user_position": 3, "total_entries": 10,
            "user_gw_points": 60, "user_total": 720,
        }
        output = self.agent._generate_review_report(29, data)
        assert "Position:** 3 of 10" in output

    def test_review_bench_boost_marker(self):
        # BB bench players are contributors with is_bench_boost_player=True -> [BB] suffix
        data = _review_data()
        data["team_points"][1]["is_bench_boost_player"] = True
        output = self.agent._generate_review_report(29, data)
        saka_row = next(line for line in output.splitlines() if "Saka" in line)
        assert "[BB]" in saka_row
        assert "(6)" not in saka_row  # no brackets - BB bench still contributes

    def test_review_no_bench_boost_marker_by_default(self):
        output = self.agent._generate_review_report(29, _review_data())
        assert "[BB]" not in output

    def test_review_red_card_emoji(self):
        data = _review_data()
        data["team_points"][1]["red_cards"] = 1
        output = self.agent._generate_review_report(29, data)
        assert "🟥" in output

    def test_review_no_red_cards_omits_column(self):
        data = _review_data()
        # Default data has red_cards=0 for all players
        output = self.agent._generate_review_report(29, data)
        # Table header should not have the red card column
        for line in output.splitlines():
            if line.startswith("| Player"):
                assert "🟥" not in line
                break

    def test_review_transfer_row_rendered(self):
        output = self.agent._generate_review_report(29, _review_data())
        assert "Salah" in output
        assert "Saka" in output
        assert "Hit" in output


# ---------------------------------------------------------------------------
# Group 3: Inline fallback path
# ---------------------------------------------------------------------------

class TestInlineFallback:
    def setup_method(self):
        self.agent = ReportAgent()

    # Preview inline

    def test_preview_inline_player_name(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "Salah" in output

    def test_preview_inline_price_riser(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "Palmer" in output

    def test_preview_inline_classic_section(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "# Classic" in output

    def test_preview_inline_no_further_reading(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "# Further Reading" not in output

    # Review inline

    def test_review_inline_player_name(self):
        output = self.agent._generate_review_inline(29, _review_data())
        assert "Salah" in output

    def test_review_inline_points_value(self):
        output = self.agent._generate_review_inline(29, _review_data())
        assert "72" in output

    def test_review_inline_classic_section(self):
        output = self.agent._generate_review_inline(29, _review_data())
        assert "# Classic" in output

    def test_review_inline_draft_section(self):
        output = self.agent._generate_review_inline(29, _review_data())
        assert "# Draft" in output

    def test_review_inline_captain_marker(self):
        output = self.agent._generate_review_inline(29, _review_data())
        assert "Salah (C)" in output

    # classic_league: standings not yet published (GW1) or historical review,
    # neither of which carries total_entries/user_total -- both used to crash
    # `_generate_review_inline` with a TypeError on `{cl['user_total']:,}`.

    def test_review_inline_standings_pending_no_crash(self):
        data = _review_data()
        data["classic_league"] = {"league_name": "Office League", "standings_pending": True}
        output = self.agent._generate_review_inline(29, data)
        assert "Standings not published yet" in output

    def test_review_inline_historical_review_no_crash(self):
        data = _review_data()
        data["classic_league"] = {"league_name": "Office League"}
        output = self.agent._generate_review_inline(29, data)
        assert "standings not shown for historical GW29 review" in output

    def test_review_inline_populated_league_shows_position(self):
        data = _review_data()
        data["classic_league"] = {
            "league_name": "Office League", "user_position": 3, "total_entries": 10,
            "user_gw_points": 60, "user_total": 720,
        }
        output = self.agent._generate_review_inline(29, data)
        assert "Position:** 3 of 10" in output
        assert "Total: 720" in output


# ---------------------------------------------------------------------------
# Group 4: Fixture column
# ---------------------------------------------------------------------------

class TestFixtureColumn:
    def setup_method(self):
        self.agent = ReportAgent()

    def test_fixture_in_template_preview(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "ARS" in output

    def test_fixture_in_template_draft(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "liv" in output

    def test_fixture_in_inline_preview(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "ARS" in output

    def test_fixture_in_inline_draft(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "liv" in output

    def test_fixture_header_in_template(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "| Fixture |" in output

    def test_fixture_header_in_inline(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "| Fixture |" in output


# ---------------------------------------------------------------------------
# Group 4b: Gameweek Fixtures FDR rounding matches `fpl fixtures` (#237)
# ---------------------------------------------------------------------------

def _custom_fdr_preview_data() -> dict:
    """Preview data with the custom-analysis team-ratings FDR (a float)."""
    data = _preview_data()
    data["gw_fixtures"] = [
        {
            "home_team": "LIV",
            "home_fdr": 3.75,
            "away_fdr": 4.25,
            "away_team": "ARS",
            "kickoff": "Sat 15:00",
        }
    ]
    return data


class TestGameweekFixturesFdrRounding:
    """The saved report must round a float FDR to 1dp like `fpl fixtures`
    does, not print the raw team-ratings figure (issue #237)."""

    def setup_method(self):
        self.agent = ReportAgent()

    def test_template_rounds_float_fdr_to_one_decimal(self):
        output = self.agent._generate_preview_report(29, _custom_fdr_preview_data())
        assert "3.8" in output
        assert "4.2" in output
        assert "3.75" not in output
        assert "4.25" not in output

    def test_inline_rounds_float_fdr_to_one_decimal(self):
        output = self.agent._generate_preview_inline(29, _custom_fdr_preview_data())
        assert "3.8" in output
        assert "4.2" in output
        assert "3.75" not in output
        assert "4.25" not in output

    def test_template_leaves_raw_api_int_fdr_unformatted(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "| LIV | 2 | vs | 4 | ARS |" in output

    def test_inline_leaves_raw_api_int_fdr_unformatted(self):
        output = self.agent._generate_preview_inline(29, _preview_data())
        assert "| LIV | 2 | vs | 4 | ARS |" in output


# ---------------------------------------------------------------------------
# Group 5: Teams with Easy Fixtures footer (#186)
# ---------------------------------------------------------------------------

def _easy_fixtures_data(fdr_mode: str | None = None) -> dict:
    data = _preview_data()
    fixtures: dict = {
        "easy_fixture_runs": {
            "overall": [
                {
                    "short_name": "LIV",
                    "average_fdr": 2.5,
                    "average_fdr_atk": 2.0,
                    "average_fdr_def": 3.0,
                    "fixtures_summary": "ars BOU",
                }
            ]
        }
    }
    if fdr_mode:
        fixtures["fdr_mode"] = fdr_mode
    data["fixtures"] = fixtures
    return data


def _footer_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("*FDR scale")]
    assert len(lines) == 1, output
    return lines[0]


class TestEasyFixturesFooter:
    def setup_method(self):
        self.agent = ReportAgent()

    def test_template_footer_names_mode_and_column_relationship(self):
        output = self.agent._generate_preview_report(29, _easy_fixtures_data("opponent"))
        footer = _footer_line(output)
        assert "All three columns use opponent mode (opponent strength at the venue only)" in footer
        assert "FDR is the mean of ATK and DEF" in footer
        assert "1 (easiest) - 7 (hardest)" in footer

    def test_template_footer_defaults_to_difference_mode(self):
        """A result without the mode (older data) is described as the agent default."""
        output = self.agent._generate_preview_report(29, _easy_fixtures_data())
        footer = _footer_line(output)
        assert "difference mode (opponent strength at the venue, blended with the team's own)" in footer

    def test_inline_footer_matches_template(self):
        data = _easy_fixtures_data("difference")
        template_footer = _footer_line(self.agent._generate_preview_report(29, data))
        inline_footer = _footer_line(self.agent._generate_preview_inline(29, data))
        assert inline_footer == template_footer

    def test_no_footer_without_easy_fixtures(self):
        output = self.agent._generate_preview_report(29, _preview_data())
        assert "*FDR scale" not in output


class TestEasyFixturesFdrRounding:
    """The inline fallback must round Teams with Easy Fixtures FDR the same
    way the template does -- both are `%.1f` (issue #237/#239 review), not
    the inline path's former `.2f`."""

    def setup_method(self):
        self.agent = ReportAgent()

    @staticmethod
    def _data() -> dict:
        data = _preview_data()
        data["fixtures"] = {
            "easy_fixture_runs": {
                "overall": [
                    {
                        "short_name": "LIV",
                        "average_fdr": 3.75,
                        "average_fdr_atk": 4.25,
                        "average_fdr_def": 2.35,
                        "fixtures_summary": "ars BOU",
                    }
                ]
            }
        }
        return data

    def test_template_and_inline_round_to_the_same_one_decimal(self):
        template_output = self.agent._generate_preview_report(29, self._data())
        inline_output = self.agent._generate_preview_inline(29, self._data())

        for output in (template_output, inline_output):
            assert "3.8" in output
            assert "4.2" in output
            assert "2.4" in output
            assert "3.75" not in output
            assert "4.25" not in output
            assert "2.35" not in output


# ---------------------------------------------------------------------------
# Performance Stats window and empty reason (#227)
# ---------------------------------------------------------------------------

class TestPreviewPerformanceStatsWindow:
    """The stats heading reports the window analysed, not a hardcoded six.

    Before GW9 the analysis window is clamped to the gameweeks played, so a
    fixed "(Last 6 GWs)" heading described football that had not happened; and
    an empty section printed bare, exactly the silence #227 was about.
    """

    agent = ReportAgent()

    @staticmethod
    def _data_with_stats(**stats) -> dict:
        data = _preview_data()
        data["stats"] = {
            "top_xgi_per_90": [], "underperformers": [], "value_picks": [],
            **stats,
        }
        return data

    def test_template_heading_carries_the_clamped_window(self):
        output = self.agent._generate_preview_report(3, self._data_with_stats(
            window_label="last 2 GWs (window of 6 clamped to gameweeks played)",
        ))
        assert "last 2 GWs (window of 6 clamped to gameweeks played)" in output
        assert "Last 6 GWs" not in output

    def test_template_renders_the_empty_reason(self):
        output = self.agent._generate_preview_report(3, self._data_with_stats(
            window_label="whole season",
            empty_reason={"code": "no_minutes_played", "message": "Nothing played yet."},
        ))
        assert "Nothing played yet." in output

    def test_inline_fallback_heading_carries_the_clamped_window(self):
        """The fallback used when the template file is missing says the same."""
        output = self.agent._generate_preview_inline(3, self._data_with_stats(
            window_label="last 2 GWs (window of 6 clamped to gameweeks played)",
        ))
        assert "last 2 GWs (window of 6 clamped to gameweeks played)" in output
        assert "Last 6 GWs" not in output

    def test_inline_fallback_renders_the_empty_reason(self):
        output = self.agent._generate_preview_inline(3, self._data_with_stats(
            window_label="whole season",
            empty_reason={"code": "no_minutes_played", "message": "Nothing played yet."},
        ))
        assert "Nothing played yet." in output

    def test_a_payload_without_a_window_label_keeps_the_old_heading(self):
        """Degrades rather than printing "None" if stats ever arrive unlabelled."""
        for render in (self.agent._generate_preview_report, self.agent._generate_preview_inline):
            output = render(3, self._data_with_stats())
            assert "Performance Stats (Last 6 GWs)" in output


# ---------------------------------------------------------------------------
# Group 8: incomplete synthesis is visible in the saved report (#266)
# ---------------------------------------------------------------------------

class TestSynthesisProblemsCallout:
    """A verdict the model dropped must not read as one deliberately omitted.

    The stderr warning is gone by the time someone opens the file weeks later,
    so the durable artefact has to say so itself.
    """

    def setup_method(self):
        self.agent = ReportAgent()

    def _render(self, **overrides):
        data = _review_data()
        data["synthesis_summary"] = "## Summary\nA shrug of a week.\n\n## Classic Verdict\nGrim, and then"
        data.update(overrides)
        return self.agent._generate_review_report(29, data)

    def test_the_problems_are_named_in_the_report(self):
        output = self._render(synthesis_problems=[
            "missing section(s): ## Draft Verdict, ## Next Week",
            "response ends without terminal punctuation (likely cut off mid-sentence)",
        ])
        assert "failed its completeness check" in output
        assert "## Draft Verdict" in output
        assert "terminal punctuation" in output

    def test_the_summary_itself_is_still_written(self):
        output = self._render(synthesis_problems=["missing section(s): ## Draft Verdict"])
        assert "A shrug of a week." in output

    def test_a_clean_run_adds_no_callout(self):
        assert "completeness check" not in self._render(synthesis_problems=[])

    def test_a_run_that_never_set_the_key_adds_no_callout(self):
        assert "completeness check" not in self._render()
