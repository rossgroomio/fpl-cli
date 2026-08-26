# fpl-cli

Fantasy Premier League analysis from the terminal. Classic and Draft formats. Six data sources, one interface.

[![PyPI](https://img.shields.io/pypi/v/fplkit)](https://pypi.org/project/fplkit/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

<img src="https://raw.githubusercontent.com/rossgroomio/fpl-cli/main/docs/images/fpl-player-demo-v1-0.png"
     alt="FPL player scouting table in fpl-cli showing form, xGI, expected goals, availability flags and Fantasy Premier League data"
     width="600"
     style="max-width: 100%; height: auto;">

*Example output from `fpl player` — real-time player analysis with form, xGI and availability flags.*

## Features

- **Multi-source data** - FPL API, Draft API, Understat (npxG/xGChain), historical data (vaastav 2022-25 + Core-Insights 2025-26), and football-data.org in one place.
- **Player scouting** - Filter by any stat, track xG trends, spot underperformers, check fixture runs.
- **Fixture intelligence** - Bayesian difficulty ratings from actual match results, blank/double GW detection, squad exposure analysis.
- **Custom analysis** - Captain picks, transfer targets, differentials, waivers, and ILP-optimal squad allocation. Opt-in, off by default.
- **Gameweek reports** - Post-GW reviews and league recaps with optional LLM narrative.
- **Draft parity** - Most commands work for both Classic and Draft. Waivers cover the free-agent wire.
- **Agent-friendly** - `--format json` with a consistent envelope on key commands. Ready-made agent skills in `.agents/skills/`.

## Quickstart

```console
$ pipx install fplkit
$ fpl init
$ fpl status
$ fpl stats -p MID -s form --available-only
```

`fpl init` configures your FPL IDs and optional features. With just an entry ID you get the full data toolkit.

## Installation

Requires **Python 3.11+**.

```console
$ pipx install fplkit
```

Alternatives: `uv pip install fplkit` or `pip install fplkit`.

> [!NOTE]
> Browser scraping (`fpl squad sell-prices --refresh`) requires the scraper extra and Playwright:
> ```console
> $ pipx install 'fplkit[scraper]'
> $ playwright install chromium
> ```

## Usage

### After the Gameweek

```console
$ fpl status                       # GW result, deadline, rank movement, flagged players
$ fpl review --save --summarise    # Full review with LLM narrative
$ fpl league                       # Live league standings
$ fpl league-recap                 # Awards, standings movement, and running streaks
$ fpl league-recap --summarise     # Add the LLM editorial for the group chat
$ fpl league-fines                 # Who owes what this season, and which weeks were ruled
```

Fines are ruled per gameweek by `league-recap` and recorded against it, so `fpl
league-fines` reads them straight back off disk — no network, and any season still
on disk (`--season 2025-26`). Every gameweek that could not be ruled is named
beneath the table, because a zero there means "not known", not "not fined".

The recap prints the season table only at GW19 and at the finale, so the set-piece
stays a set-piece — though with `--summarise` its editorial knows the running totals
every week and may work one into a sentence. `fpl league-fines` answers the season
question any week you want it.

Streaks come with season totals: the recap counts every time a condition has occurred
this season — gameweek wins, last places, weeks on top, captain blanks — across resets,
so a report or editorial can say "their sixth gameweek win of the season", not just
"two in a row". Each count has its own rule for when it is worth mentioning, tuned to
how often it happens: wins and last places on every third, captain blanks and waiver
hauls on every fifth, bottom-half gameweeks only in the second half of the season and
only on every tenth, and a green-arrow drought only while unbroken at five or ten
gameweeks, never counting the weeks a manager spent top of the table with nowhere
to climb. Some bring along others who added to the same count that week. The full list
prints twice a season, at GW19 and the finale, alongside the fines table. Unjudged
gameweeks are always stated beside the number — see
[Season counts](docs/command-reference.md#league-recap) for the per-count rules.

### Checking Your Setup

```console
$ fpl doctor                       # Verify configured IDs and data files - essential after a season rollover
$ fpl doctor --providers           # Probe the external data sources for shape and volume drift
```

Every ID in settings.yaml is resolved against the live API and the team/league name reported back, so a dead or recycled ID (which otherwise fails silently) is visible. Per-team data files are checked against the current season's clubs. Exits non-zero when something needs fixing.

`--providers` checks the other side of the bargain: that each external data source (FPL and Draft APIs, the historical datasets, Understat, football-data.org) still serves data of the expected shape and size, that every club resolves across sources, and that the per-gameweek match files actually parse into records through the same code the scoring commands run — the upstream drift that otherwise surfaces as plausible but wrong output, especially at a season rollover.

### Scouting Players

```console
$ fpl stats -p FWD -s form -a             # Forwards by form, excluding unavailable
$ fpl player Rice -f -u                   # Deep dive: fixtures + Understat analysis
$ fpl xg                                  # xG/xA analysis, over/underperformers
$ fpl history                             # Career arc across 3 seasons
$ fpl price-history -n 4 -s price_slope   # Bandwagon detection
$ fpl returnees                           # Injured and suspended players due back soon
$ fpl returnees --enrich                  # Add searched return timing where FPL says nothing
```

`fpl returnees` reads the availability news attached to each player, works out who is due back inside the next few gameweeks, and keeps the list short by filtering on past performance — the players worth claiming in draft before they are fit again, and worth planning around in classic. FPL states a return date for only a small minority of flagged players, so `--enrich` searches the web for fresher timing on the ones it is quiet or stale about and shows what it finds alongside the FPL news rather than in place of it. Each run remembers what it showed, so the next one can say whose outlook moved.

### Before the Deadline

```console
$ fpl squad                        # Squad health: form, injuries, recommendations
$ fpl squad grid -n 8 -w Mbeumo    # 8-GW fixture difficulty grid with a watchlist player
$ fpl fdr --my-squad               # Your squad's blank/double GW exposure
$ fpl preview --save --scout       # Full analysis + BUY/SELL research via LLM
```

### Strategic Planning

```console
$ fpl fdr --blanks                 # Confirmed + predicted blank/double GWs
$ fpl chips timing                 # Rule-based Free Hit / Bench Boost / Triple Captain signals
$ fpl fixtures                     # Next GW fixtures with FDR
```

### Season Preview Intel

Optional. Notes you write up per team before the season — projected XIs, long-term
injuries, new signings with no Premier League record — from whatever source you read.
Nothing ships with the tool; the previews are yours.

```console
$ fpl intel schema                 # The file format, every field explained
$ fpl intel init                   # Scaffold one empty file per team
$ fpl intel resolve ARS --write    # Match player names to FPL codes, save them
$ fpl intel                        # Coverage across the league, and what it permits
$ fpl intel show ARS               # One team's intel, aged to the current gameweek
```

Intel expires on a schedule (`fpl intel --show-decay` prints it): each section is aged
out at the point where real data supersedes it — API news for injuries, actual minutes
for projected XIs. There is no in-season upkeep, and squad-builder and gw-prep read it
automatically when present.

### Custom Analysis

Off by default. Enable via `fpl init` or `custom_analysis: true` in settings.yaml.

```console
$ fpl captain                      # Ranked captain picks (0-100 matchup scoring)
$ fpl targets --min-own 30         # Transfer targets, template tier
$ fpl differentials -t 3           # Ultra-differentials (<3% owned)
$ fpl waivers                      # Free-agent waiver picks with drop suggestions (draft)
$ fpl transfer-eval --out Palmer --in "Salah,Mbeumo"  # Side-by-side comparison
$ fpl allocate --horizon 1         # Free Hit: optimal squad for a single GW
$ fpl ratings                      # Bayesian team strength ratings from match results
```

Enabling custom analysis also enriches other commands: `fpl stats` gains `--value` columns, `fpl xg` adds Value Picks, `fpl fdr` upgrades to Bayesian FDR with ATK/DEF split.

### JSON Output

Commands with `--format json` emit a consistent envelope:

```json
{
  "command": "stats",
  "metadata": {"gameweek": null, "format": "classic", "custom_analysis": true},
  "data": [...]
}
```

```console
$ fpl stats --format json -p MID -s expected_goal_involvements
$ fpl status --format json
$ fpl fdr --blanks --format json
$ fpl league-recap --format json
$ fpl league-fines --format json
```

Failures come back on the same stream: `{"command", "error"}` on stdout with exit code 1.
Parse stdout either way, and read the message from `error` rather than from stderr — see
[JSON Output](docs/command-reference.md#json-output).

## Configuration

Run `fpl init` to configure interactively. Settings stored in your platform's config directory (override with `FPL_CLI_CONFIG_DIR`, and also home to the optional `previews/` intel files); generated data such as team ratings, chip plans and the league history ledger `league-recap` builds up is stored in your platform's data directory (override with `FPL_CLI_DATA_DIR`). In ephemeral environments (e.g. Claude Code on the web), point both at a persistent workspace. Both overrides must be absolute paths — see [Directories](docs/command-reference.md#directories). Saved reports (`--save`, `--output`) are written to a season subdirectory — `<output_dir>/2026-27/gw21-review.md` — so a new season never overwrites the last one's files; see [Report layout](docs/command-reference.md#report-layout).

**Required:** FPL classic entry ID or draft league + entry IDs.

| Feature | What it enables |
|---------|----------------|
| Custom Analysis | Captain, targets, differentials, waivers, allocate, ratings, value scores, Bayesian FDR |
| League ID | Standings, fines, league recaps |
| LLM providers | `--summarise` and `--scout` flags (Perplexity, Anthropic, OpenAI, or any compatible API) |
| FPL credentials | `fpl squad sell-prices` (browser scraping) |
| `FOOTBALL_DATA_API_KEY` | League table in `fpl review`; Championship form for promoted teams in pre-season ratings |

```bash
# LLM providers (for --summarise and --scout)
export PERPLEXITY_API_KEY="your-key"    # Research role
export ANTHROPIC_API_KEY="your-key"     # Synthesis role
```

See [Command Reference](docs/command-reference.md#configuration-reference) for the full settings.yaml schema and LLM provider setup.

## Development

```console
$ git clone https://github.com/rossgroomio/fpl-cli.git
$ cd fpl-cli
$ python3 -m venv .venv && source .venv/bin/activate
$ pip install -e ".[dev]"
$ fpl init
```

```console
$ ruff check fpl_cli/    # Lint
$ pyright fpl_cli/       # Type check
$ pytest tests/          # Tests
```

> [!NOTE]
> Football data provided by the [Football-Data.org API](https://www.football-data.org/). Player and fixture data is property of the Premier League. Expected goals data is property of [Understat](https://understat.com). Historical data from [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League) (MIT, 2022-25) and [olbauday/FPL-Core-Insights](https://github.com/olbauday/FPL-Core-Insights) (2025-26+). This tool fetches data at runtime and does not redistribute third-party data.

> [!WARNING]
> Browser scraping (`fpl squad sell-prices`) uses your FPL login to read sell prices. Automated access may violate FPL website terms - use at your own risk.

---

[Command Reference](docs/command-reference.md) | [Custom Analysis Guide](docs/custom-analysis.md) | [Architecture](docs/architecture.md) | [Agent Tools & Skills](.agents/TOOLS.md) | [Contributing](CONTRIBUTING.md)
