"""Shared CLI infrastructure: console, config paths, settings loader, format resolution."""

from __future__ import annotations

import dataclasses
import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import click
import yaml
from rich.console import Console

from fpl_cli.paths import SHIPPED_CONFIG_DIR, user_config_dir

console = Console()
error_console = Console(stderr=True)


def _user_config_dir() -> Path:
    return user_config_dir()


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


def resolve_output_dir(settings: dict[str, Any]) -> Path:
    raw = settings.get("reports", {}).get("output_dir")
    if raw:
        return Path(raw).expanduser()
    return _user_config_dir() / "output"


def resolve_research_dir(settings: dict[str, Any]) -> Path:
    raw = settings.get("reports", {}).get("research_dir")
    if raw:
        return Path(raw).expanduser()
    return _user_config_dir() / "research"


def load_settings() -> dict[str, Any]:
    """Load settings: project defaults, then user overrides."""
    settings = _load_yaml_file(SHIPPED_CONFIG_DIR / "defaults.yaml")
    user_settings = _load_yaml_file(_user_config_dir() / "settings.yaml")
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
    """Check whether custom analysis features are enabled in settings."""
    return bool(settings.get("custom_analysis", False))


class FormatAwareGroup(click.Group):
    """Click group that renders commands in format-aware sections."""

    def _is_experimental_hidden(self, ctx: click.Context) -> bool:
        """Return True when experimental commands should be suppressed."""
        if isinstance(ctx.obj, CLIContext):
            settings = ctx.obj.settings
        else:
            settings = load_settings()
        return not is_custom_analysis_enabled(settings)

    def list_commands(self, ctx: click.Context) -> list[str]:
        commands = super().list_commands(ctx)
        if self._is_experimental_hidden(ctx):
            commands = [c for c in commands if c not in EXPERIMENTAL]
        return commands

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name in EXPERIMENTAL and self._is_experimental_hidden(ctx):
            return None
        return super().get_command(ctx, cmd_name)

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
