"""Tests for season-partitioned report destinations (#85).

Every generated report is named by gameweek alone -- `gw21-review.md`,
`gw21-league-recap.md`, `gw22-scout-preview.md` -- and written with an
unconditional `write_text`. Without a season in the path, the first time a new
season reaches a gameweek the previous one already reached, the older report is
destroyed with no warning. These tests pin the directory partition that makes
that collision impossible rather than merely detectable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl_cli.agents.base import AgentStatus
from fpl_cli.agents.orchestration.report import ReportAgent
from fpl_cli.cli._context import resolve_output_dir, resolve_research_dir
from fpl_cli.paths import user_config_dir
from fpl_cli.season import season_label


@pytest.fixture
def frozen_season(monkeypatch):
    """Pin the season the resolvers see, so a test can play two seasons off
    against each other without waiting a year for the July cutover."""

    def _freeze(year: int) -> str:
        monkeypatch.setattr("fpl_cli.season.get_season_year", lambda *_a, **_k: year)
        return season_label(year)

    return _freeze


class TestResolveOutputDir:
    """The configured `reports.output_dir` and an explicit `--output` are both
    partitioned -- a scripted run is no less entitled to the protection."""

    def test_configured_dir_is_partitioned(self, tmp_path, frozen_season):
        label = frozen_season(2026)
        settings = {"reports": {"output_dir": str(tmp_path / "01_Reports")}}

        assert resolve_output_dir(settings) == tmp_path / "01_Reports" / label

    def test_override_is_partitioned_too(self, tmp_path, frozen_season):
        label = frozen_season(2026)
        settings = {"reports": {"output_dir": str(tmp_path / "configured")}}

        resolved = resolve_output_dir(settings, str(tmp_path / "explicit"))

        assert resolved == tmp_path / "explicit" / label

    def test_override_wins_over_the_configured_dir(self, tmp_path):
        settings = {"reports": {"output_dir": str(tmp_path / "configured")}}

        resolved = resolve_output_dir(settings, str(tmp_path / "explicit"))

        assert (tmp_path / "configured") not in resolved.parents

    def test_default_location_is_partitioned(self, frozen_season):
        label = frozen_season(2026)

        assert resolve_output_dir({}) == user_config_dir() / "output" / label

    def test_expands_a_tilde_before_partitioning(self, frozen_season):
        label = frozen_season(2026)
        settings = {"reports": {"output_dir": "~/fpl-reports"}}

        resolved = resolve_output_dir(settings)

        assert "~" not in str(resolved)
        assert resolved == Path.home() / "fpl-reports" / label

    def test_a_dir_already_naming_the_season_is_not_nested_twice(self, tmp_path, frozen_season):
        """A user who has already pointed the setting at a season directory
        gets that directory, not `2026-27/2026-27`."""
        label = frozen_season(2026)
        settings = {"reports": {"output_dir": str(tmp_path / "01_Reports" / label)}}

        assert resolve_output_dir(settings) == tmp_path / "01_Reports" / label

    def test_an_explicit_season_overrides_the_clock(self, tmp_path, frozen_season):
        """#91: a caller holding a GW1-derived season (`review`, `league-recap`,
        `preview`) passes it explicitly rather than relying on the clock-based
        default -- which matters precisely when a season overruns the July
        cutover and the two disagree."""
        frozen_season(2026)  # clock says 2026-27
        settings = {"reports": {"output_dir": str(tmp_path / "01_Reports")}}

        resolved = resolve_output_dir(settings, season="2019-20")

        assert resolved == tmp_path / "01_Reports" / "2019-20"

    def test_consecutive_seasons_resolve_to_different_dirs(self, tmp_path, monkeypatch):
        """The regression itself: one configured output dir, two seasons, two
        destinations."""
        settings = {"reports": {"output_dir": str(tmp_path / "01_Reports")}}

        monkeypatch.setattr("fpl_cli.season.get_season_year", lambda *_a, **_k: 2025)
        last_season = resolve_output_dir(settings)
        monkeypatch.setattr("fpl_cli.season.get_season_year", lambda *_a, **_k: 2026)
        this_season = resolve_output_dir(settings)

        assert last_season != this_season
        assert last_season.name == "2025-26"
        assert this_season.name == "2026-27"


class TestStaleSeasonDirectory:
    """A directory named for a season that has passed is a misconfiguration.
    Partitioning still happens -- nesting loses nothing, where reusing the
    stale directory would file this season's reports under last season's name
    -- but it must not be silent."""

    def test_a_stale_season_dir_is_still_partitioned(self, tmp_path, frozen_season):
        label = frozen_season(2026)
        settings = {"reports": {"output_dir": str(tmp_path / "01_Reports" / "2025-26")}}

        resolved = resolve_output_dir(settings)

        assert resolved == tmp_path / "01_Reports" / "2025-26" / label

    def test_a_stale_season_dir_warns(self, tmp_path, frozen_season, capsys):
        frozen_season(2026)
        settings = {"reports": {"output_dir": str(tmp_path / "01_Reports" / "2025-26")}}

        resolve_output_dir(settings)

        warning = capsys.readouterr().err
        assert "2025-26" in warning
        assert "2026-27" in warning

    def test_the_stale_check_compares_against_an_explicit_season_not_the_clock(
        self, tmp_path, frozen_season, capsys,
    ):
        """#91: a caller passing a GW1-derived season gets the warning judged
        against *that* season, so a run during a July-overrun no longer
        misreports a current directory as stale (or a stale one as current)
        just because the clock disagrees with the data."""
        frozen_season(2026)  # clock says 2026-27
        settings = {"reports": {"output_dir": str(tmp_path / "01_Reports" / "2019-20")}}

        resolve_output_dir(settings, season="2019-20")

        assert capsys.readouterr().err == ""

    def test_the_current_season_dir_does_not_warn(self, tmp_path, frozen_season, capsys):
        """The supported shortcut stays quiet -- only a stale label is worth
        interrupting for."""
        label = frozen_season(2026)
        settings = {"reports": {"output_dir": str(tmp_path / "01_Reports" / label)}}

        resolve_output_dir(settings)

        assert capsys.readouterr().err == ""

    def test_an_ordinary_dir_does_not_warn(self, tmp_path, frozen_season, capsys):
        """`is_season_label` is exact, so a directory that merely contains
        digits is not mistaken for a season."""
        frozen_season(2026)
        settings = {"reports": {"output_dir": str(tmp_path / "reports-2026")}}

        resolve_output_dir(settings)

        assert capsys.readouterr().err == ""


class TestResolveResearchDir:
    """The research root holds one subdirectory per source, so the season
    segment belongs inside each of those, not on the root. Taking the source
    as an argument is what makes the partition non-optional for a future
    caller."""

    def test_partitions_below_the_source_subdir(self, tmp_path, frozen_season):
        """The shape `fpl preview --scout` writes: the season sits under
        `ai-scout-reports/`, leaving sibling sources their own partitions."""
        label = frozen_season(2026)
        settings = {"reports": {"research_dir": str(tmp_path / "02_Research")}}

        resolved = resolve_research_dir(settings, "ai-scout-reports")

        assert resolved == tmp_path / "02_Research" / "ai-scout-reports" / label

    def test_default_location_is_partitioned_too(self, frozen_season):
        label = frozen_season(2026)

        resolved = resolve_research_dir({}, "ai-scout-reports")

        assert resolved == user_config_dir() / "research" / "ai-scout-reports" / label

    def test_an_explicit_season_overrides_the_clock(self, tmp_path, frozen_season):
        """#91 review: `preview --scout` derives its season from GW1's
        deadline and must pass it through here rather than let the clock
        override it -- the same gap `resolve_output_dir` already closed."""
        frozen_season(2026)  # clock says 2026-27
        settings = {"reports": {"research_dir": str(tmp_path / "02_Research")}}

        resolved = resolve_research_dir(settings, "ai-scout-reports", season="2019-20")

        assert resolved == tmp_path / "02_Research" / "ai-scout-reports" / "2019-20"

    def test_sibling_sources_get_independent_partitions(self, tmp_path, frozen_season):
        """A second research source cannot skip the partition -- it comes with
        the resolver rather than being the caller's job to remember."""
        label = frozen_season(2026)
        settings = {"reports": {"research_dir": str(tmp_path / "02_Research")}}

        scout = resolve_research_dir(settings, "ai-scout-reports")
        injuries = resolve_research_dir(settings, "injury-news")

        assert scout.name == injuries.name == label
        assert scout.parent != injuries.parent


class TestReportAgentWritesWhereTold:
    """The agent stays a dumb writer: partitioning is the resolver's job, so
    the agent must not add a second season segment of its own."""

    async def test_writes_directly_into_the_given_dir(self, tmp_path, frozen_season):
        label = frozen_season(2026)
        agent = ReportAgent(config={"output_dir": str(tmp_path / label)})

        result = await agent.run(
            context={"report_type": "review", "gameweek": 21, "data": {}},
        )

        assert result.status == AgentStatus.SUCCESS
        assert (tmp_path / label / "gw21-review.md").exists()
        assert not (tmp_path / label / label).exists()

    async def test_the_same_gameweek_in_two_seasons_keeps_both_reports(self, tmp_path, monkeypatch):
        """End to end over the resolver: GW21 written in 2025-26 survives GW21
        being written in 2026-27."""
        settings = {"reports": {"output_dir": str(tmp_path / "01_Reports")}}
        context = {"report_type": "review", "gameweek": 21, "data": {}}

        monkeypatch.setattr("fpl_cli.season.get_season_year", lambda *_a, **_k: 2025)
        agent = ReportAgent(config={"output_dir": str(resolve_output_dir(settings))})
        await agent.run(context=context)
        last_season_report = tmp_path / "01_Reports" / "2025-26" / "gw21-review.md"
        last_season_report.write_text("last season", encoding="utf-8")

        monkeypatch.setattr("fpl_cli.season.get_season_year", lambda *_a, **_k: 2026)
        agent = ReportAgent(config={"output_dir": str(resolve_output_dir(settings))})
        await agent.run(context=context)

        assert last_season_report.read_text(encoding="utf-8") == "last season"
        assert (tmp_path / "01_Reports" / "2026-27" / "gw21-review.md").exists()
