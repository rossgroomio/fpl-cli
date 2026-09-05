"""Shared CLI infrastructure: console, config paths, settings loader, context accessors."""

from __future__ import annotations

import dataclasses
import os
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import yaml
from rich.console import Console

from fpl_cli.paths import SHIPPED_CONFIG_DIR, UserDirError, user_config_dir
from fpl_cli.season import is_season_label, season_label, season_partition

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fpl_cli.agents.base import AgentResult
    from fpl_cli.services.fixture_predictions import FixturePredictionsService

console = Console()
error_console = Console(stderr=True)


class ConfigError(ValueError):
    """A `settings.yaml` block a command cannot run past.

    Config is parsed lazily, deep inside whichever command needs it, so the
    place that finds the defect knows neither the command name nor the format
    its reader is on. Raising this type rather than a bare `ValueError` lets
    `config_failure_boundary` (`cli/_json.py`) catch it once per command and
    turn it into the same `{command, error}` envelope every other failure
    produces. Before #170 it escaped click as a traceback, which under
    `--format json` meant exit 1 with zero bytes on stdout -- the one outcome
    a consumer cannot tell apart from a crash or a hang.

    A `ValueError` subclass because that is what these parsers raised before
    the boundary existed: a caller already catching `ValueError` keeps
    working, and the boundary still catches nothing wider than a parser
    deliberately raises. A bug in our own code raises something else and
    still surfaces as a traceback, where it can be seen.
    """


def warn_prediction_problems(pred_service: FixturePredictionsService) -> None:
    """Report prediction files that were skipped (unreadable, malformed, empty, stale).

    Without this a broken user copy is invisible: predictions quietly fall
    back to the shipped file, or vanish, and the only clue is a staleness
    warning that blames the wrong file. Every command that constructs a
    FixturePredictionsService should call this once after first use.
    """
    from rich.markup import escape as rich_escape

    for warning in pred_service.load_warnings:
        error_console.print(f"[yellow]{rich_escape(warning)}[/yellow]")


def print_result_warnings(data: Mapping[str, Any]) -> None:
    """Print the warnings an agent attached to its result, for table mode.

    The prior-blended commands (`fpl targets`, `fpl differentials`,
    `fpl waivers`, `fpl transfer-eval`) all carry the early-season notice the
    same way: the agent decides it, because only the agent knows whether the
    priors loaded, and the command routes it to whichever channel its reader
    is on. Centralising the table-mode half keeps those paths behaviourally
    identical, the same argument `handle_agent_failure` makes below — four
    copies of the loop is four places to forget the markup escape.

    Escaped, because a warning is prose assembled from data (player names,
    file paths) and a stray `[` would otherwise be read as Rich markup and
    swallow the rest of the line.
    """
    from rich.markup import escape as rich_escape

    for warning in data.get("warnings", []):
        error_console.print(f"[yellow]{rich_escape(warning['message'])}[/yellow]")


def split_result_warnings(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Split an agent's result data into the JSON payload and its warnings.

    The JSON counterpart to `print_result_warnings`: a warning belongs in the
    envelope's `metadata.warnings`, never in `data`, so a consumer reads one
    place whichever command produced the score. Returning both halves keeps
    that invariant in one named place rather than in each command's `pop`.
    """
    payload = dict(data)
    warnings = payload.pop("warnings", [])
    return payload, warnings


def handle_agent_failure(result: AgentResult) -> None:
    """Print an agent failure to stderr and exit nonzero.

    Table-mode counterpart to `_json.emit_json_error` -- centralising this
    keeps the two failure paths behaviourally identical, so a copy-pasted
    two-line `return` can't silently reintroduce a zero exit code on
    command failure (#47).

    On stderr, like every other reason a command exits 1 (#162): the parallel
    with `emit_json_error` only holds if this half keeps stdout clear too.
    """
    error_console.print(f"[red]Agent failed: {result.message}[/red]")
    for error in result.errors:
        error_console.print(f"  [red]{error}[/red]")
    raise SystemExit(1)


def _user_config_dir() -> Path:
    return user_config_dir()


# settings.yaml paths already reported by _warn_once_if_settings_missing.
# Keyed by path so a process that resolves more than one config dir (tests) is
# warned per dir; cleared by the autouse fixture in tests/conftest.py.
_warned_missing_settings: set[Path] = set()


def _warn_once_if_settings_missing(settings_file: Path) -> None:
    """Warn on stderr when an explicitly-set config dir holds no settings.yaml.

    Only fires for FPL_CLI_CONFIG_DIR. An absent settings.yaml under the
    default platformdirs location is the normal pre-`fpl init` state, but
    someone who pointed the variable at a specific directory meant it to hold
    their config -- staying silent there is how a mis-set override reads as a
    corrupted install instead of a misconfiguration (#46).
    """
    if not os.environ.get("FPL_CLI_CONFIG_DIR") or settings_file.exists():
        return
    if settings_file in _warned_missing_settings:
        return
    _warned_missing_settings.add(settings_file)
    click.echo(
        f"Warning: FPL_CLI_CONFIG_DIR points at {settings_file.parent}, which has no "
        f"settings.yaml -- running on defaults only. Run 'fpl init' to create one there.",
        err=True,
    )


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        click.echo(f"Warning: invalid YAML in {path}: {exc}", err=True)
        return {}


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge override into base, mutating base."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


class Format(StrEnum):
    CLASSIC = "classic"
    DRAFT = "draft"
    BOTH = "both"


@dataclasses.dataclass
class CLIContext:
    format: Format | None
    settings: dict[str, Any]


def resolve_format(settings: dict[str, Any]) -> Format | None:
    """Infer FPL format from configured IDs. FPL_FORMAT env var overrides."""
    env_override = os.environ.get("FPL_FORMAT")
    if env_override:
        try:
            return Format(env_override.lower())
        except ValueError:
            click.echo(f"Warning: ignoring unrecognised FPL_FORMAT={env_override!r}", err=True)

    fpl = settings.get("fpl", {})
    has_classic = bool(fpl.get("classic_entry_id"))
    has_draft = bool(fpl.get("draft_league_id"))
    if has_classic and has_draft:
        return Format.BOTH
    if has_classic:
        return Format.CLASSIC
    if has_draft:
        return Format.DRAFT
    return None


def get_format(ctx: click.Context) -> Format | None:
    """Extract resolved format from Click context, or None if unavailable."""
    return ctx.obj.format if isinstance(ctx.obj, CLIContext) else None


def get_settings(ctx: click.Context) -> dict[str, Any]:
    """Merged settings for a command, from the Click context or loaded on demand.

    The settings counterpart to `get_format(ctx)`, and how a command holding a
    context should read them. `main()` merges defaults and user overrides once
    per invocation and puts the result on the context; unwrapping `ctx.obj` at
    the call site instead had the same isinstance dance written out six times
    across five commands, and reaching past it for `load_settings()` was a
    second shape for the same question in four more (#219).

    Falls back to loading rather than to `{}` because a command invoked
    without the group -- programmatically, or in a test -- still has defaults:
    `{}` is not "no user overrides", it is "no `rolling_window`, no
    thresholds, no llm roles" either. `_is_experimental_hidden` and
    `format_commands` below already load for that reason; this makes it the
    rule rather than the exception.
    """
    if isinstance(ctx.obj, CLIContext):
        return ctx.obj.settings
    return load_settings()


def _warn_if_stale_season_dir(base: Path) -> None:
    """Warn when a report directory is named for a season that has passed.

    Left alone this is the quiet half of #85: the directory keeps last
    season's name, this season's reports nest inside it, and nothing says so.
    Partitioning still happens -- nesting loses no data, where reusing the
    stale directory would file the reports under the wrong season -- but the
    user hears about it once so they can repoint the setting.
    """
    if is_season_label(base.name) and base.name != season_label():
        error_console.print(
            f"[yellow]Warning:[/yellow] report directory {base} is named for season "
            f"{base.name}, but the current season is {season_label()}. "
            f"Reports will be written to {base / season_label()}. "
            f"Drop the season from the setting -- it is appended automatically."
        )


def resolve_output_dir(settings: dict[str, Any], output: str | None = None) -> Path:
    """Season-partitioned directory that generated reports are written to.

    `output` is the command's `--output` flag, which wins over the configured
    `reports.output_dir` but is partitioned just the same: reports are named
    by gameweek alone, so an unpartitioned destination lets a new season's
    GW21 report overwrite the previous season's (#85), and a scripted
    `--output` is no less entitled to that protection than a configured one.
    """
    if output:
        base = Path(output).expanduser()
    else:
        raw = settings.get("reports", {}).get("output_dir")
        base = Path(raw).expanduser() if raw else _user_config_dir() / "output"
    _warn_if_stale_season_dir(base)
    return season_partition(base)


def resolve_research_dir(settings: dict[str, Any], source: str) -> Path:
    """Season-partitioned directory for one research `source`, e.g. `ai-scout-reports`.

    The research root holds one subdirectory per source -- and, in the vault,
    sibling directories owned by other tools -- so the season segment belongs
    inside each source rather than above them all. Taking `source` here rather
    than returning the bare root is what makes that non-optional: a future
    writer adding `injury-news/` gets the partition by construction instead of
    having to remember to wrap the result in `season_partition()`.
    """
    raw = settings.get("reports", {}).get("research_dir")
    root = Path(raw).expanduser() if raw else _user_config_dir() / "research"
    return season_partition(root / source)


def load_settings() -> dict[str, Any]:
    """Load settings: project defaults, then user overrides."""
    settings = _load_yaml_file(SHIPPED_CONFIG_DIR / "defaults.yaml")
    user_file = _user_config_dir() / "settings.yaml"
    _warn_once_if_settings_missing(user_file)
    user_settings = _load_yaml_file(user_file)
    _deep_merge(settings, user_settings)
    return settings


CLASSIC_ONLY: frozenset[str] = frozenset({
    "allocate", "chips", "captain",
    "targets", "differentials", "credentials",
    "sell-prices",
})
DRAFT_ONLY: frozenset[str] = frozenset({"waivers"})

EXPERIMENTAL: frozenset[str] = frozenset({
    "captain", "targets", "differentials", "waivers",
    "allocate", "transfer-eval", "ratings",
})


def is_custom_analysis_enabled(settings: dict[str, Any]) -> bool:
    """Whether custom analysis features are enabled in the given settings.

    Ask this when the settings are already in hand -- `fpl stats` also needs
    `rolling_window`, `fpl player` and `fpl preview` the configured entry IDs
    -- so the gate answers for the same dict the rest of the command reads.
    `custom_analysis_enabled(ctx)` below is the one-liner for a command whose
    only interest in the settings is this question.
    """
    return bool(settings.get("custom_analysis", False))


def custom_analysis_enabled(ctx: click.Context) -> bool:
    """Whether custom analysis is switched on, for a command holding a context.

    The gate `fpl fdr`, `fpl fixtures` and `fpl xg` share: they want the
    toggle and nothing else from the settings, and each was resolving them
    itself to ask (#219). Deliberately not used where a command already holds
    its settings -- re-resolving them there is a second answer that can
    disagree with the dict the command is otherwise reading.
    """
    return is_custom_analysis_enabled(get_settings(ctx))


def experimental_gate_message(cmd_name: str) -> str:
    """Explain why a gated command is unavailable and how to turn it on.

    Names the settings.yaml actually being read, because the usual cause of a
    surprise gate is the CLI resolving a different config dir than expected.
    """
    try:
        location = str(_user_config_dir() / "settings.yaml")
    except UserDirError:  # pragma: no cover - the dir is resolved before this point
        location = "settings.yaml in your fpl-cli config directory"
    return (
        f"'{cmd_name}' is a custom-analysis command and is currently switched off.\n"
        f"Enable it with 'custom_analysis: true' in {location}, or run 'fpl init'."
    )


class FormatAwareGroup(click.Group):
    """Click group that renders commands in format-aware sections."""

    def main(self, *args: Any, **kwargs: Any) -> Any:
        """Report an unusable FPL_CLI_* directory as an error, not a traceback."""
        try:
            return super().main(*args, **kwargs)
        except UserDirError as exc:
            if not kwargs.get("standalone_mode", True):
                # Click's contract for programmatic use: raise, don't print-and-exit.
                raise
            failure = click.ClickException(str(exc))
            failure.show()
            raise SystemExit(failure.exit_code) from exc

    def _is_experimental_hidden(self, ctx: click.Context) -> bool:
        """Return True when experimental commands should be suppressed."""
        return not custom_analysis_enabled(ctx)

    def list_commands(self, ctx: click.Context) -> list[str]:
        commands = super().list_commands(ctx)
        if self._is_experimental_hidden(ctx):
            commands = [c for c in commands if c not in EXPERIMENTAL]
        return commands

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name in EXPERIMENTAL and self._is_experimental_hidden(ctx):
            return None
        return super().get_command(ctx, cmd_name)

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """Name the gate instead of letting click contradict itself.

        click builds its "Did you mean" suggestions from `self.commands`, which
        still holds the gated commands -- so a hidden one suggests itself:
        "No such command 'ratings'. Did you mean 'ratings'?" (#46). Intercept
        before that and say what the gate actually is.

        Shell completion resolves commands with `resilient_parsing` set and
        expects a `None` command back, not an exception -- leave that path to
        click, which already returns None for a gated name.
        """
        if (
            args
            and not ctx.resilient_parsing
            and args[0] in EXPERIMENTAL
            and self._is_experimental_hidden(ctx)
        ):
            raise click.UsageError(experimental_gate_message(args[0]), ctx=ctx)
        return super().resolve_command(ctx, args)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # ctx.obj may be None when --help short-circuits the callback
        if isinstance(ctx.obj, CLIContext):
            fmt = ctx.obj.format
        else:
            fmt = resolve_format(load_settings())
        commands = self.list_commands(ctx)

        sections: dict[str, list[tuple[str, str]]] = {}
        for name in commands:
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            help_text = cmd.get_short_help_str(limit=formatter.width - 6 - len(name))
            if name in CLASSIC_ONLY:
                if fmt != Format.DRAFT:
                    sections.setdefault("Classic", []).append((name, help_text))
            elif name in DRAFT_ONLY:
                if fmt != Format.CLASSIC:
                    sections.setdefault("Draft", []).append((name, help_text))
            else:
                sections.setdefault("General", []).append((name, help_text))

        for section_name in ["General", "Classic", "Draft"]:
            rows = sections.get(section_name, [])
            if rows:
                with formatter.section(f"{section_name} Commands"):
                    formatter.write_dl(rows)
