"""Report agent for generating markdown reports."""
# Keep report sections in sync with cli/preview.py

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import jinja2
from jinja2 import Environment, FileSystemLoader, select_autoescape

from fpl_cli.agents.base import Agent, AgentResult, AgentStatus
from fpl_cli.cli._league_recap_types import RecapManagerEntry
from fpl_cli.paths import TEMPLATE_DIR
from fpl_cli.services.team_ratings import fdr_columns_footer
from fpl_cli.utils.time import format_generated_at


class ReportAgent(Agent):
    """Agent for generating markdown reports.

    Responsibilities:
    - Compile data from other agents into reports
    - Generate gameweek preview reports
    - Generate gameweek review reports
    - Write reports to configured output directory

    `output_dir` is written to verbatim. Report filenames carry the gameweek
    but no season, so the season segment that stops one season's GW21 report
    overwriting the last one's (#85) is applied by the caller --
    `fpl_cli.cli._context.resolve_output_dir`, which partitions both the
    configured directory and an explicit `--output`. Callers that bypass that
    resolver must partition with `fpl_cli.season.season_partition` themselves.
    """

    name = "ReportAgent"
    description = "Generates markdown reports"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.output_dir = Path(
            config.get("output_dir", ".")
        ) if config else Path(".")

        # Setup Jinja2 environment
        self.jinja_env = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=select_autoescape(default=False),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # Footer wording for FDR tables, shared with the CLI so the saved
        # report and the terminal describe the columns the same way
        self.jinja_env.globals["fdr_columns_footer"] = fdr_columns_footer

    async def run(self, context: dict[str, Any] | None = None) -> AgentResult:
        """Generate a report from provided data.

        Args:
            context: Should contain:
                - 'report_type': Type of report ('preview' or 'review')
                - 'gameweek': Gameweek number
                - 'data': Dict of data from various agents

        Returns:
            AgentResult with generated report path.
        """
        if not context:
            return self._create_result(
                AgentStatus.FAILED,
                message="No context provided",
                errors=["Provide report_type, gameweek, and data in context"],
            )

        report_type = context.get("report_type", "preview")
        gameweek = context.get("gameweek")
        data = context.get("data", {})

        if not gameweek:
            return self._create_result(
                AgentStatus.FAILED,
                message="No gameweek specified",
                errors=["Provide gameweek number in context"],
            )

        self.log(f"Generating {report_type} report for GW{gameweek}...")

        try:
            if report_type == "preview":
                content = self._generate_preview_report(gameweek, data)
                filename = f"gw{gameweek}-preview.md"
            elif report_type == "review":
                content = self._generate_review_report(gameweek, data)
                filename = f"gw{gameweek}-review.md"
            elif report_type == "league-recap":
                content = self._generate_league_recap_report(gameweek, data)
                fmt_suffix = "-draft" if data.get("fpl_format") == "draft" else ""
                filename = f"gw{gameweek}-league-recap{fmt_suffix}.md"
            else:
                return self._create_result(
                    AgentStatus.FAILED,
                    message=f"Unknown report type: {report_type}",
                    errors=["Use 'preview', 'review', or 'league-recap'"],
                )

            # Ensure output directory exists
            self.output_dir.mkdir(parents=True, exist_ok=True)

            # Write report
            output_path = self.output_dir / filename
            output_path.write_text(content, encoding="utf-8")

            self.log_success(f"Report written to {output_path}")

            return self._create_result(
                AgentStatus.SUCCESS,
                data={
                    "report_path": str(output_path),
                    "report_type": report_type,
                    "gameweek": gameweek,
                },
                message=f"Report saved to {output_path}",
            )

        except Exception as e:  # noqa: BLE001 — agent top-level handler
            self.log_error(f"Failed to generate report: {e}")
            return self._create_result(
                AgentStatus.FAILED,
                message="Failed to generate report",
                errors=[str(e)],
            )

    def _generate_preview_report(self, gameweek: int, data: dict[str, Any]) -> str:
        """Generate a gameweek preview report."""
        try:
            template = self.jinja_env.get_template("gw_preview.md.j2")
            data.setdefault("generated_at", format_generated_at())
            return template.render(
                gameweek=gameweek,
                **data,
            )
        except jinja2.TemplateNotFound:
            # Fallback to inline template if file not found
            return self._generate_preview_inline(gameweek, data)

    def _generate_preview_inline(self, gameweek: int, data: dict[str, Any]) -> str:
        """Generate preview report with inline template."""
        generated_at = data.get("generated_at", format_generated_at())
        lines = [
            f"*Generated: {generated_at}*",
        ]

        # Deadline
        if data.get("deadline"):
            lines.extend([
                f"**Deadline:** {data['deadline']}",
                "",
            ])

        # --- Fixture Analysis ---
        lines.extend([
            "---",
            "# Fixture Analysis",
        ])

        # Gameweek Fixtures
        if data.get("gw_fixtures"):
            lines.extend([
                "## Gameweek Fixtures",
                "| Home | FDR |    | FDR | Away | Kickoff |",
                "|------|-----|----|-----|------|---------|",
            ])
            for f in data["gw_fixtures"]:
                lines.append(
                    f"| {f['home_team']} | {f['home_fdr']} | vs | {f['away_fdr']} | {f['away_team']} | {f['kickoff']} |"
                )
            lines.append("")

        # Teams with Easy Fixtures
        if data.get("fixtures") and data["fixtures"].get("easy_fixture_runs"):
            easy_runs = data["fixtures"]["easy_fixture_runs"]
            # Support both old list format and new dict format with positional FDR
            if isinstance(easy_runs, dict):
                easy_list = easy_runs.get("overall", [])
            else:
                easy_list = easy_runs
            lines.extend([
                "## Teams with Easy Fixtures",
                "| Team | Avg FDR | ATK | DEF | Next 6 Fixtures |",
                "|------|---------|-----|-----|-----------------|",
            ])
            for team in easy_list[:8]:
                fdr = team["average_fdr"]
                fdr_atk = team.get("average_fdr_atk", fdr)
                fdr_def = team.get("average_fdr_def", fdr)
                lines.append(
                    f"| {team['short_name']} | {fdr:.2f} | {fdr_atk:.2f} | {fdr_def:.2f} | {team['fixtures_summary']} |"
                )
            fdr_mode = data["fixtures"].get("fdr_mode", "difference")
            lines.append(f"*{fdr_columns_footer(fdr_mode)}*")
            lines.append("")

        # --- Team Form ---
        if data.get("fixtures") and data["fixtures"].get("team_form"):
            lines.extend([
                "---",
                "# Team Form",
                "| Team | Pts (6) | GS (6) | GC (6) | Next | Pts (H/A) | GS (H/A) | GC (H/A) |",
                "|------|---------|--------|--------|------|-----------|----------|----------|",
            ])
            for t in data["fixtures"]["team_form"]:
                lines.append(
                    f"| {t['team']} | {t['pts_6']} | {t['gs_6']} | {t['gc_6']}"
                    f" | {t['next_venue']} | {t['pts_ha']} | {t['gs_ha']} | {t['gc_ha']} |"
                )
            lines.append("")

        # --- Classic ---
        lines.extend([
            "---",
            "# Classic",
        ])

        # My Squad
        if data.get("my_squad"):
            lines.extend([
                "## My Squad",
                "| Player | Team | Fixture | Pos | Form | Own% | Status |",
                "|--------|------|---------|-----|------|------|--------|",
            ])
            for p in data["my_squad"]:
                lines.append(
                    f"| {p['name']} | {p['team']} | {p.get('fixture', '—')} | {p['position']}"
                    f" | {p['form']:.1f} | {p['ownership']}% | {p['status']} |"
                )
            lines.append("")

        # Transfer Activity
        if data.get("prices"):
            price_data = data["prices"]
            lines.extend([
                "## Transfer Activity",
            ])

            if price_data.get("risers_this_gw"):
                lines.extend([
                    "### Price Rises",
                    "| Player | Team | Price | Change |",
                    "|--------|------|-------|--------|",
                ])
                for p in price_data["risers_this_gw"][:10]:
                    lines.append(
                        f"| {p['name']} | {p['team']} | £{p['current_price']:.1f}m | +£{p['change_this_gw']:.1f}m |"
                    )
                lines.append("")

            if price_data.get("fallers_this_gw"):
                lines.extend([
                    "### Price Falls",
                    "| Player | Team | Price | Change |",
                    "|--------|------|-------|--------|",
                ])
                for p in price_data["fallers_this_gw"][:10]:
                    lines.append(
                        f"| {p['name']} | {p['team']} | £{p['current_price']:.1f}m | £{p['change_this_gw']:.1f}m |"
                    )
                lines.append("")

            if price_data.get("hot_transfers_in"):
                lines.extend([
                    "### Most Transferred In",
                    "| Player | Team | Transfers In | Net |",
                    "|--------|------|--------------|-----|",
                ])
                for p in price_data["hot_transfers_in"][:8]:
                    lines.append(
                        f"| {p['name']} | {p['team']} | {p['transfers_in']:,} | {p['net_transfers']:+,} |"
                    )
                lines.append("")

            if price_data.get("hot_transfers_out"):
                lines.extend([
                    "### Most Transferred Out",
                    "| Player | Team | Transfers Out | Net |",
                    "|--------|------|--------------:|----:|",
                ])
                for p in price_data["hot_transfers_out"][:8]:
                    lines.append(
                        f"| {p['name']} | {p['team']} | {p['transfers_out']:,} | {p['net_transfers']:+,} |"
                    )
                lines.append("")

        # Performance Stats
        if data.get("stats"):
            stats_data = data["stats"]
            lines.extend([
                "## Performance Stats (Last 6 GWs)",
            ])

            if stats_data.get("top_xgi_per_90"):
                lines.extend([
                    "### Top xGI per 90",
                    "| Player | Team | xG | npxG | xA | xGI/90 | xGC/90 | Goals | Assists |",
                    "|--------|------|----|------|----|--------|--------|-------|---------|",
                ])
                for p in stats_data["top_xgi_per_90"][:10]:
                    npxg = p.get('npxG_per_90')
                    xgc = p.get('xGChain_per_90')
                    npxg_str = f"{npxg:.2f}" if npxg is not None else "-"
                    xgc_str = f"{xgc:.2f}" if xgc is not None else "-"
                    lines.append(
                        f"| {p['name']} | {p['team']} | {p['xG']:.2f} | {npxg_str}"
                        f" | {p['xA']:.2f} | {p['xGI_per_90']:.2f} | {xgc_str}"
                        f" | {p['goals']} | {p['assists']} |"
                    )
                lines.append("")

            if stats_data.get("underperformers"):
                lines.extend([
                    "### Underperformers (G+A < xGI)",
                    "| Player | Team | G+A | xGI | Diff |",
                    "|--------|------|-----|-----|------|",
                ])
                for p in stats_data["underperformers"][:8]:
                    lines.append(
                        f"| {p['name']} | {p['team']} | {p['GI']} | {p['xGI']:.2f} | {p['difference']:.2f} |"
                    )
                lines.append("")

            # Flag penalty-inflated players
            penalty_players = [
                p for p in (stats_data.get("top_xgi_per_90") or [])
                if p.get("penalty_xG") is not None and p["penalty_xG"] > 1.5
            ]
            if penalty_players:
                lines.extend([
                    "### Penalty xG Warning",
                    "Players with >1.5 xG from penalties (regression risk if penalty duties change):",
                ])
                for p in penalty_players[:5]:
                    lines.append(f"- **{p['name']}** ({p['team']}): {p['penalty_xG']:.1f} penalty xG")
                lines.append("")

            if stats_data.get("value_picks"):
                lines.extend([
                    "### Value Picks",
                    "| Player | Team | Price | Own% | xGI/90 |",
                    "|--------|------|-------|------|--------|",
                ])
                for p in stats_data["value_picks"][:8]:
                    lines.append(
                        f"| {p['name']} | {p['team']} | £{p['price']:.1f}m"
                        f" | {p['ownership']:.1f}% | {p['xGI_per_90']:.2f} |"
                    )
                lines.append("")

        # --- Draft ---
        if data.get("draft_squad"):
            lines.extend([
                "---",
                "# Draft",
                "## My Squad",
                "| Player | Team | Fixture | Pos | Form | Status |",
                "|--------|------|---------|-----|------|--------|",
            ])
            for p in data["draft_squad"]:
                lines.append(
                    f"| {p['name']} | {p['team']} | {p.get('fixture', '—')}"
                    f" | {p['position']} | {p['form']:.1f} | {p['status']} |"
                )
            lines.append("")

        lines.extend([
            "---",
            "*Report generated by FPL CLI*",
        ])

        return "\n".join(lines)

    def _generate_review_report(self, gameweek: int, data: dict[str, Any]) -> str:
        """Generate a gameweek review report."""
        try:
            template = self.jinja_env.get_template("gw_review.md.j2")
            data.setdefault("generated_at", format_generated_at())
            return template.render(
                gameweek=gameweek,
                **data,
            )
        except jinja2.TemplateNotFound:
            # Fallback to inline template
            return self._generate_review_inline(gameweek, data)

    def _generate_review_inline(self, gameweek: int, data: dict[str, Any]) -> str:
        """Generate review report with inline template (new structure)."""
        lines = [
            f"*Generated: {format_generated_at()}*",
            "",
            "# Classic",
        ]

        # Team Summary
        if data.get("points"):
            points_data = data["points"]
            total = points_data.get("total")
            rank = points_data.get("rank")
            overall = points_data.get("overall_rank")

            lines.extend([
                "## Team Summary",
                "| Metric | Value |",
                "|--------|-------|",
                f"| **Points** | {total if total else 'N/A'} |",
            ])
            if rank:
                lines.append(f"| **GW Rank** | {rank:,} |")
            if overall:
                lines.append(f"| **Overall Rank** | {overall:,} |")
            lines.extend([
                f"| **GW Average** | {points_data.get('average', 'N/A')} |",
                f"| **GW Highest** | {points_data.get('highest', 'N/A')} |",
                "",
            ])

        # Team Points (unified - no separate bench)
        if data.get("team_points"):
            lines.extend([
                "## Team Points",
                "| Player | Team | Pos | Pts |",
                "|--------|------|-----|-----|",
            ])
            for p in data["team_points"]:
                if p.get("contributed", True):
                    marker = " (C)" if p.get("is_captain") else " (V)" if p.get("is_vice_active") else ""
                    bb = " [BB]" if p.get("is_bench_boost_player") else ""
                    lines.append(f"| {p['name']}{marker} | {p['team']} | {p['position']} | {p['display_points']}{bb} |")
                else:
                    lines.append(f"| {p['name']} | {p['team']} | {p['position']} | ({p['display_points']}) |")
            lines.append("")

        # Classic League
        if data.get("classic_league"):
            cl = data["classic_league"]
            lines.extend([
                "## League",
                f"**{cl.get('league_name', 'Classic League')}**",
            ])
            if cl.get("standings_pending"):
                lines.append(
                    "Standings not published yet - FPL builds mini-league tables"
                    " after the opening gameweek is finalised.",
                )
            elif cl.get("total_entries"):
                lines.extend([
                    f"- **Position:** {cl.get('user_position')} of {cl.get('total_entries')}",
                    f"- **GW Points:** {cl.get('user_gw_points')} (Total: {cl.get('user_total', 0):,})",
                ])
            else:
                lines.append(
                    f"League standings not shown for historical GW{gameweek} review"
                    " - use `fpl league` for current standings.",
                )

            if cl.get("nearby_rivals"):
                lines.extend([
                    "### Nearby Rivals (+/- 25 pts)",
                    "| Pos | Manager | Total | Diff |",
                    "|-----|---------|-------|------|",
                ])
                user_total = cl.get("user_total", 0)
                for r in cl["nearby_rivals"]:
                    diff = r.get("total", 0) - user_total
                    diff_str = f"+{diff}" if diff > 0 else str(diff) if diff < 0 else "-"
                    name = r.get("manager_name", "Unknown")
                    lines.append(f"| {r.get('rank')} | {name} | {r.get('total'):,} | {diff_str} |")

            if cl.get("best_performers"):
                lines.append("### Best GW Performers")
                for p in cl["best_performers"]:
                    rank = p.get("rank_str", "?")
                    lines.append(f"{rank}. {p['name']} - {p['points']} pts")

            if cl.get("worst_performers"):
                lines.append("### Worst GW Performers (Net Points)")
                for p in cl["worst_performers"]:
                    rank = p.get("rank_str", "?")
                    name = "You" if p.get("is_user") else p.get("name", "Unknown")
                    gross = p.get("gross_points", p.get("points", 0))
                    cost = p.get("transfer_cost", 0)
                    net = p.get("net_points", gross)
                    if cost > 0:
                        lines.append(f"{rank}. {name} - {gross} gross, -{cost} hit = {net} net")
                    else:
                        lines.append(f"{rank}. {name} - {net} pts")
                if cl.get("transfer_impact"):
                    lines.append(f"\n⚠ {cl['transfer_impact']}")
            lines.append("")

        # Global section
        if data.get("global_stats"):
            gs = data["global_stats"]
            lines.append("## Global")

            if gs.get("summary"):
                summary = gs["summary"]
                lines.append("### Summary")

                if summary.get("most_transferred_in"):
                    lines.append("**Most Transferred In:**")
                    for i, p in enumerate(summary["most_transferred_in"], 1):
                        lines.append(f"{i}. {p['name']} ({p['team']}) - {p['transfers']:,} transfers")

                if summary.get("most_transferred_out"):
                    lines.append("**Most Transferred Out:**")
                    for i, p in enumerate(summary["most_transferred_out"], 1):
                        lines.append(f"{i}. {p['name']} ({p['team']}) - {p['transfers']:,} transfers")

                if summary.get("top_scorers"):
                    lines.append("**Top Scorers:**")
                    for i, p in enumerate(summary["top_scorers"], 1):
                        lines.append(f"{i}. {p['name']} ({p['team']}) - {p['points']} pts")

            if gs.get("dream_team"):
                lines.extend([
                    "",
                    "### Dream Team",
                    "| Player | Team | Pos | Pts |",
                    "|--------|------|-----|-----|",
                ])
                for p in gs["dream_team"]:
                    lines.append(f"| {p['name']} | {p['team']} | {p['position']} | {p['points']} |")

            lines.append("")

        # Draft section
        lines.extend([
            "---",
            "# Draft",
        ])

        # Draft Squad Points
        if data.get("draft_squad_points"):
            lines.extend([
                "## Team Points",
                "| Player | Team | Pos | Pts |",
                "|--------|------|-----|-----|",
            ])
            for p in data["draft_squad_points"]:
                if p.get("contributed", True):
                    lines.append(f"| {p['name']} | {p['team']} | {p['position']} | {p['points']} |")
                else:
                    lines.append(f"| {p['name']} | {p['team']} | {p['position']} | ({p['points']}) |")
            lines.append("")

        # Draft League
        if data.get("draft_league"):
            dl = data["draft_league"]
            lines.extend([
                "## League",
                f"- **Position:** {dl.get('user_position')} of {dl.get('total_entries')}",
                f"- **GW Points:** {dl.get('user_gw_points')} (Total: {dl.get('user_total'):,})",
            ])

            if dl.get("best_performers"):
                lines.append("### Best GW Performers")
                for p in dl["best_performers"]:
                    rank = p.get("rank_str", "?")
                    lines.append(f"{rank}. {p['name']} - {p['points']} pts")

            if dl.get("worst_performers"):
                lines.append("### Worst GW Performers")
                for p in dl["worst_performers"]:
                    rank = p.get("rank_str", "?")
                    lines.append(f"{rank}. {p['name']} - {p['points']} pts")
            lines.append("")

        # Results
        lines.extend([
            "---",
            "# Results",
        ])
        if data.get("fixtures"):
            for f in data["fixtures"]:
                lines.append(f"**{f['home_team']} {f['home_score']}-{f['away_score']} {f['away_team']}**")
                if f.get("goals"):
                    lines.append(f"- Goals: {f['goals']}")
                if f.get("assists"):
                    lines.append(f"- Assists: {f['assists']}")
                if f.get("own_goals"):
                    lines.append(f"- Own Goals: {f['own_goals']}")
                if f.get("bonus"):
                    lines.append(f"- Bonus: {f['bonus']}")
                lines.append("")

        # League Table
        if data.get("league_table"):
            lines.extend([
                "---",
                "# League Table",
                "| Pos | Team | P | W | D | L | GD | Pts |",
                "|-----|------|---|---|---|---|-----|-----|",
            ])
            for t in data["league_table"]:
                lines.append(
                    f"| {t['position']} | {t['name']} | {t['played']} "
                    f"| {t['win']} | {t['draw']} | {t['loss']} "
                    f"| {t['goal_difference']} | {t['points']} |"
                )
            lines.append("")

        lines.extend([
            "---",
            "*Report generated by FPL CLI*",
        ])

        return "\n".join(lines)

    def _generate_league_recap_report(self, gameweek: int, data: dict[str, Any]) -> str:
        """Generate a league recap report."""
        template = self.jinja_env.get_template("gw_league_recap.md.j2")
        data.setdefault("generated_at", format_generated_at())
        data["standings_block"] = _format_standings_block(data.get("managers", []))
        return template.render(**data)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# R10: a position or total that could not be derived is named by these two
# constants, never rendered blank or zero. Shared with the console surface
# (`fpl_cli.cli.league_recap._render_console_highlights`) so the two
# surfaces can't drift onto different wording for the same fact.
POSITION_UNAVAILABLE = "position unavailable"
TOTAL_UNAVAILABLE = "total unavailable"


def _format_standings_block(managers: Sequence[RecapManagerEntry]) -> str:
    """Render league standings as a space-aligned text block.

    Sort order: gw_points desc, tie-break by overall_rank asc (managers with
    no derivable position, e.g. an unreconstructable draft replay per R10,
    sort last). Each row: GW rank, manager (+ chip), GW points, (ordinal
    league pos[ arrow], total) -- either half of that pair is named
    "unavailable" by R10 rather than rendered blank or zero when it could not
    be derived.
    """
    if not managers:
        return ""

    sorted_managers = sorted(
        managers,
        key=lambda m: (-m["gw_points"], m.get("overall_rank", _UNRANKED)),
    )

    def display_name(m: RecapManagerEntry) -> str:
        chip = m.get("active_chip")
        return f"{m['manager_name']} ({chip})" if chip else m["manager_name"]

    name_width = max(len(display_name(m)) for m in sorted_managers)
    pts_width = max(3, max(len(str(m["gw_points"])) for m in sorted_managers))
    rank_width = max(2, len(str(len(sorted_managers))))

    lines = []
    for idx, m in enumerate(sorted_managers, start=1):
        overall_rank = m.get("overall_rank")
        previous_rank = m.get("previous_rank")
        total_points = m.get("total_points")

        if overall_rank is None:
            position_str = POSITION_UNAVAILABLE
            arrow = ""
        else:
            position_str = _ordinal(overall_rank)
            if previous_rank is None:
                arrow = ""
            else:
                delta = previous_rank - overall_rank
                if delta > 0:
                    arrow = f" ↑{delta}"
                elif delta < 0:
                    arrow = f" ↓{-delta}"
                else:
                    arrow = ""
        total_str = str(total_points) if total_points is not None else TOTAL_UNAVAILABLE
        context = f"({position_str}{arrow}, {total_str})"
        lines.append(
            f"{idx:>{rank_width}}.  "
            f"{display_name(m):<{name_width}}  "
            f"{m['gw_points']:>{pts_width}}   "
            f"{context}"
        )
    return "\n".join(lines)


# Sort-only sentinel for a manager with no derivable league position --
# larger than any real rank, so they sort after every ranked manager.
_UNRANKED = float("inf")
