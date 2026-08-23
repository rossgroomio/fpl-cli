"""CLI entry point for fpl-cli."""

from __future__ import annotations

import click

from fpl_cli import __version__

# Load environment variables: user config dir first, local .env fills gaps only
from fpl_cli.cli._context import CLIContext, FormatAwareGroup, load_settings, resolve_format
from fpl_cli.cli.allocate import allocate_command
from fpl_cli.cli.captain import captain_command
from fpl_cli.cli.chips import chips_group
from fpl_cli.cli.credentials import credentials_group
from fpl_cli.cli.differentials import differentials_command
from fpl_cli.cli.doctor import doctor_command
from fpl_cli.cli.fdr import fdr_command
from fpl_cli.cli.fixtures import fixtures_command
from fpl_cli.cli.history import history_command
from fpl_cli.cli.init import init_command
from fpl_cli.cli.intel import intel_group
from fpl_cli.cli.league import league_command
from fpl_cli.cli.league_recap import league_recap_command
from fpl_cli.cli.player import player_command
from fpl_cli.cli.preview import preview_command
from fpl_cli.cli.price_changes import price_changes_command
from fpl_cli.cli.price_history import price_history_command
from fpl_cli.cli.ratings import ratings_group
from fpl_cli.cli.review import review_command
from fpl_cli.cli.squad import squad_group
from fpl_cli.cli.stats import stats_command
from fpl_cli.cli.status import status_command
from fpl_cli.cli.targets import targets_command
from fpl_cli.cli.transfer_eval import transfer_eval_command
from fpl_cli.cli.waivers import waivers_command
from fpl_cli.cli.xg import xg_command
from fpl_cli.paths import ensure_legacy_migration, load_env_files

load_env_files()


@click.group(cls=FormatAwareGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="fpl-cli", message="⚽ fpl-cli v%(version)s")
@click.pass_context
def main(ctx: click.Context) -> None:
    """fpl-cli - Fantasy Premier League analysis for classic and draft formats."""
    if ctx.invoked_subcommand == "doctor":
        # Doctor diagnoses unusable FPL_CLI_* overrides, so it must start even
        # when resolving a directory would fail -- migration and settings both
        # resolve user dirs, and doctor re-reads settings itself, per check.
        ctx.obj = CLIContext(format=None, settings={})
        return
    # Deferred to invocation: the user dirs must not resolve until .env is loaded.
    ensure_legacy_migration()
    if ctx.invoked_subcommand == "init":
        ctx.obj = CLIContext(format=None, settings={})
        return
    settings = load_settings()
    ctx.obj = CLIContext(
        format=resolve_format(settings),
        settings=settings,
    )


# --- Top-level commands ---
main.add_command(init_command)
main.add_command(status_command)
main.add_command(doctor_command)
main.add_command(fixtures_command)
main.add_command(player_command)
main.add_command(stats_command)
main.add_command(history_command)
main.add_command(league_command)
main.add_command(waivers_command)
main.add_command(credentials_group)
main.add_command(fdr_command)
main.add_command(xg_command)
main.add_command(price_changes_command)
main.add_command(price_history_command)
main.add_command(captain_command)
main.add_command(differentials_command)
main.add_command(targets_command)
main.add_command(review_command)
main.add_command(league_recap_command)
main.add_command(preview_command)
main.add_command(transfer_eval_command)
main.add_command(allocate_command)
# --- Subgroups ---
main.add_command(squad_group)
main.add_command(chips_group)
main.add_command(ratings_group)
main.add_command(intel_group)

if __name__ == "__main__":
    main()
