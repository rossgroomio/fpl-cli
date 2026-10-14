"""Progress, warning and failure lines stay off stdout on the `fpl review` path (#388).

Table mode puts the table on stdout and every other line -- warnings, progress
notices, the reason for a nonzero exit -- on stderr, so `2>/dev/null` leaves
only the output that was asked for. The `fpl review` modules have had their
notices moved off stdout by hand six times (#4, #297, #366, #373, #386, #394),
each round found by someone reading the code or running the command rather
than by anything that runs on every PR. Ruff cannot judge which stream a
message belongs on, so this does what a lint rule would, the way
`test_paths.py` holds the no-import-time-paths rule: it reads the source and
fails on a literal printed on `console` (the stdout `Console` from `_context`)
that reads as a notice.

A notice, here, is a line whose literal text opens with `[dim]`, `[yellow]` or
`[red]`, or with a `[green]` status glyph, or that uses one of the phrases
progress and failure lines are written in. Section content can look the same
-- a dim label, a red result line -- so the content lines the scan flags carry
an allowlist entry keyed on their text, field expressions included, each with
its reason. Text rather than line numbers: an edit above an entry cannot move
it off its line, and a new notice fails even when it lands next to an allowed
one.

Blind spot: the scan reads literals. A notice held in a variable, built by a
call (`Text(...)`, `.format()`), chosen by a conditional expression, or styled
with `style=` rather than markup has no text to judge, and passes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import fpl_cli.cli as cli_package

CLI_DIR = Path(cli_package.__file__).parent

# The modules `fpl review` runs through. Widening the guard to another command
# is one line here, after that module's hits are triaged: each notice moves to
# `error_console`, each piece of content gets an allowlist entry. Never
# baseline a module's hits wholesale -- a baseline lets the next notice in.
SCANNED_MODULES = (
    "review.py",
    "_review_analysis.py",
    "_review_classic.py",
    "_review_draft.py",
    "_review_summarisation.py",
    "_synthesis_retry.py",
    "_helpers.py",
)

# Opens a notice: a dim progress line or hint, a yellow warning, a red failure,
# a green tick. Exact tags only: `[bold red]` and `[bold green]` open section
# headings ("Flagged Players:", "Star Player:"), and a bare `[green]` is a rise
# or a gain, so green counts only with a status glyph after it.
_NOTICE_OPENING = re.compile(r"\[(?:dim|yellow|red)\]|\[green\]\s*[✓✗⚠]")

# The words progress and failure lines are written in, anywhere in the text:
# the backstop for a notice with no markup, or with a tag the opening misses.
_NOTICE_PHRASES = ("Could not", "Fetching", "Generating", "Failed to", "saved to")

# The `Console` methods that put text on its stream. `log` prints a line like
# `print` does, and `status` draws a spinner whose frames reach a terminal's
# stdout even when stderr has gone to /dev/null.
_CONSOLE_WRITES = frozenset({"print", "log", "status"})

# Lines the scan flags that belong on stdout, keyed on (module, text): the
# literal with surrounding whitespace stripped and each field as its source,
# which is how a failure below quotes it.
ALLOWED_ON_STDOUT: dict[tuple[str, str], str] = {
    ("_review_analysis.py", "[dim]Top Scorers This GW:[/dim]"): "a label for the scorer rows printed under it",
    ("_review_analysis.py", "[dim]No high-ownership blankers this GW[/dim]"): "the ### Blankers section's answer",
    ("_review_analysis.py", "[red]Red Cards: {', '.join(red_card_strs)}[/red]"): "a result line under its fixture",
    ("_review_analysis.py", "[red]Own Goals: {', '.join(own_goal_strs)}[/red]"): "a result line under its fixture",
    (
        "_review_classic.py",
        "[dim]None - GW1 squads are bought pre-season, so there is nothing to review here[/dim]",
    ): "the ## Transfers section's answer for GW1",
    ("_review_classic.py", "[dim]...and {nearby_omitted} more within 25[/dim]"): "the tail of the Nearby Rivals listing",
    # Not content: the one save confirmation #394 left on stdout, because the
    # question is wider than this command. `preview`, `league-recap`, `ratings`
    # and `sell-prices` print theirs on stdout too, and #387 asked for one
    # decision across all of them. When that moves this line to stderr, the
    # stale-entry test below fails until the entry goes.
    (
        "review.py",
        "[green]✓[/green] Report saved to: {report_result.data['report_path']}",
    ): "--save confirmation, pending the cross-command decision",
}

# Every line a fix moved off stdout on the review path, as it read before the
# move. The opening and phrases were tuned against these, so each one must
# still be caught: a change that lets one through has loosened the guard.
FIXED_BY_HAND = (
    # #4
    "[yellow]No completed gameweeks yet[/yellow]",
    "[yellow]Could not determine current gameweek[/yellow]",
    "[yellow]Gameweek {gw} is not yet finished[/yellow]",
    "[yellow]No recommendations file found at {recs_path}[/yellow]",
    "[yellow]Could not fetch global stats: {rich_escape(str(e))}[/yellow]",
    "[yellow]Could not fetch fixture results: {rich_escape(str(e))}[/yellow]",
    "[yellow]Could not fetch league table: {rich_escape(str(e))}[/yellow]",
    "[yellow]Could not fetch your team: {rich_escape(str(e))}[/yellow]",
    "[yellow]Could not fetch your team data[/yellow]",
    "[yellow]  ⚠ {transfer_impact}[/yellow]",
    "[yellow]Could not fetch classic league standings: {rich_escape(str(e))}[/yellow]",
    "[yellow]Could not fetch draft league data: {rich_escape(str(e))}[/yellow]",
    "[yellow]  ⚠ Research provider returned empty response[/yellow]",
    # #297
    "[red]  ✗ Research failed: {rich_escape(str(e))}[/red]",
    "[red]  ✗ Synthesis failed: {rich_escape(str(e))}[/red]",
    # #366
    "[dim]Could not fetch transfers: {rich_escape(str(e))}[/dim]",
    "[dim]Could not fetch transactions: {rich_escape(str(e))}[/dim]",
    # #373
    "[dim]Could not fetch draft picks: {rich_escape(str(e))}[/dim]",
    # #386
    "[dim]Fetching your team data...[/dim]",
    "[dim]Set classic_entry_id in config/settings.yaml to see your squad[/dim]",
    "[dim]Set draft_entry_id in config/settings.yaml to see your draft squad[/dim]",
    "[dim]League standings not shown for historical GW{gw} review[/dim]",
    "[dim]Use 'fpl league' for current standings[/dim]",
    "[dim]{rich_escape(league_name)}: standings not published yet -- FPL builds mini-league tables"
    " after the opening gameweek is finalised[/dim]",
    "[dim]Re-run once the table appears, or use 'fpl league'[/dim]",
    # #394
    "[dim]Generating report...[/dim]",
    "[red]✗[/red] Failed to save report: {report_result.message}",
    "[dim]Dream team not available[/dim]",
    "[dim]Could not fetch dream team: {rich_escape(str(e))}[/dim]",
    "[dim]Could not calculate blankers: {rich_escape(str(e))}[/dim]",
    "[dim]Dry run: building prompts without calling LLMs...[/dim]",
    "[dim]Generating LLM summaries...[/dim]",
    "[dim]  Debug output → {debug_dir}/[/dim]",
    "[dim]  Building research prompt...[/dim]",
    "[dim]    → Saved research_system.txt, research_prompt.txt[/dim]",
    "[dim]  Fetching community narrative...[/dim]",
    "[green]  ✓[/green] Community narrative complete",
    "[dim]    → Saved research_*.txt[/dim]",
    "[dim]  Building synthesis prompt...[/dim]",
    "[dim]    → Saved synthesis_system.txt, synthesis_prompt.txt[/dim]",
    "[green]  ✓[/green] Prompts saved to data/debug/",
    "[dim]  Generating personal analysis...[/dim]",
    "[green]  ✓[/green] Personal analysis complete",
    "[dim]    → Saved synthesis_*.txt[/dim]",
)


def _field(node: ast.expr) -> str:
    """A part known only at runtime, as `{<its source>}`.

    The source rather than a bare `{}`, so that allowlisting `[dim]{rank_note}[/dim]`
    as content does not also wave through `[dim]{error}[/dim]` in the same module.
    """
    return "{" + ast.unparse(node) + "}"


def _static_text(node: ast.expr) -> str | None:
    """A printed argument's literal text, each runtime part written as its source.

    None when no part of it is literal (a variable, a `Table`, a call): there
    is no text to judge.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else _field(part.value)  # a FormattedValue
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_text(node.left), _static_text(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else _field(node.left)) + (right if right is not None else _field(node.right))
    return None


def _reads_as_notice(text: str) -> bool:
    return bool(_NOTICE_OPENING.match(text)) or any(phrase in text for phrase in _NOTICE_PHRASES)


def _notices_on_stdout(source: str) -> list[tuple[int, str]]:
    """(line, text) of each `console` write whose first argument reads as a notice.

    Only the stdout console: `error_console.print` is the fix, not a finding.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _CONSOLE_WRITES
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "console"
        ):
            continue
        text = _static_text(node.args[0])
        if text is not None and _reads_as_notice(text.strip()):
            found.append((node.lineno, text.strip()))
    return sorted(found)


def _scan() -> dict[str, list[tuple[int, str]]]:
    hits = {}
    for module in SCANNED_MODULES:
        path = CLI_DIR / module
        assert path.is_file(), f"{module} is not in fpl_cli/cli/ any more: update SCANNED_MODULES"
        hits[module] = _notices_on_stdout(path.read_text(encoding="utf-8"))
    return hits


def test_no_notice_prints_on_stdout():
    offenders = [
        f"  {module}:{line}: {text!r}"
        for module, found in _scan().items()
        for line, text in found
        if (module, text) not in ALLOWED_ON_STDOUT
    ]

    assert offenders == [], (
        "These lines print a progress notice, warning or failure reason on `console` (stdout). "
        "Print them on `error_console` so stdout carries only the output that was asked for. "
        "If one is section content that only looks like a notice, add it to ALLOWED_ON_STDOUT "
        "with its reason:\n" + "\n".join(offenders)
    )


def test_every_allowlist_entry_still_matches_a_line():
    """An entry whose line was moved, reworded or deleted would wave through the next line to reuse its text."""
    flagged = {(module, text) for module, found in _scan().items() for _, text in found}
    stale = [f"  {module}: {text!r}" for module, text in ALLOWED_ON_STDOUT if (module, text) not in flagged]

    assert stale == [], "Remove these ALLOWED_ON_STDOUT entries, which no longer match a line:\n" + "\n".join(stale)


@pytest.mark.parametrize("text", FIXED_BY_HAND)
def test_catches_every_notice_already_fixed_by_hand(text):
    assert _reads_as_notice(text)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('console.print("[dim]Fetching fixtures...[/dim]")', "[dim]Fetching fixtures...[/dim]"),
        (
            'console.print(f"\\n[yellow]Gameweek {gw} is not yet finished[/yellow]")',
            "[yellow]Gameweek {gw} is not yet finished[/yellow]",
        ),
        ('console.print(f"[red]Agent failed: {msg}[/red]")', "[red]Agent failed: {msg}[/red]"),
        ('console.print("[green]  ✓[/green] Summary complete")', "[green]  ✓[/green] Summary complete"),
        ('console.print(f"Report saved to {path}")', "Report saved to {path}"),
        ('console.print("[bold]Generating report...[/bold]")', "[bold]Generating report...[/bold]"),
        ('console.print("[dim]" + message + "[/dim]")', "[dim]{message}[/dim]"),
        (
            'console.print(\n    f"[dim]{league}: standings not published yet"\n    " -- re-run later[/dim]"\n)',
            "[dim]{league}: standings not published yet -- re-run later[/dim]",
        ),
        ('console.log("[dim]Building prompt...[/dim]")', "[dim]Building prompt...[/dim]"),
        ('with console.status("Fetching player data..."):\n    pass', "Fetching player data..."),
    ],
    ids=[
        "dim opening",
        "yellow after a newline",
        "red opening",
        "green glyph",
        "phrase without markup",
        "phrase under another tag",
        "concatenation",
        "split across lines",
        "console.log",
        "console.status spinner",
    ],
)
def test_flags_a_notice_on_stdout(source, expected):
    assert _notices_on_stdout(source) == [(1, expected)]


@pytest.mark.parametrize(
    "source",
    [
        'error_console.print("[dim]Fetching fixtures...[/dim]")',
        'console.print("\\n[bold]## League[/bold]")',
        'console.print("[bold red]Flagged Players:[/bold red]")',
        'console.print(f"[green]+{gain}[/green] places")',
        'console.print(f"  {rank}. {name} - {pts} pts")',
        'console.print("\\n" + "-" * 50)',
        "console.print(table)",
        "console.print()",
    ],
    ids=[
        "stderr console",
        "heading",
        "compound tag heading",
        "green without a glyph",
        "content row",
        "divider",
        "no literal",
        "blank line",
    ],
)
def test_passes_output_and_stderr(source):
    assert _notices_on_stdout(source) == []


def test_two_lines_of_one_shape_never_share_an_allowlist_key():
    """Content allowlisted as `[dim]{rank_note}[/dim]` must not also cover a failure printed as `[dim]{error}[/dim]`."""
    source = 'console.print(f"[dim]{rank_note}[/dim]")\nconsole.print(f"[dim]{error}[/dim]")'

    assert _notices_on_stdout(source) == [(1, "[dim]{rank_note}[/dim]"), (2, "[dim]{error}[/dim]")]
