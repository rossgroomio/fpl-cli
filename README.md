# fpl-cli

FPL analysis toolkit for the gap between gameweeks. Aggregates five data sources into one terminal. Classic and Draft. Agent-friendly.

## The Problem

Before each deadline you're switching between the FPL app, Understat, FPL Review, Twitter, and a spreadsheet - and still making decisions on incomplete data. The FPL app shows points, not *why* you got them or what to change. Every other tool is Classic-only, so Draft players get nothing.

## The Solution

fpl-cli pulls data from the FPL API, Draft API, Understat (xG/xA), vaastav's historical dataset, and football-data.org into one interface. Filter players by any stat, track xG trends, spot underperformers, check fixture difficulty, monitor price movements - all from the terminal with `--format json` for scripting.

Optionally enable **custom analysis** for scored recommendations: captain picks ranked 0-100, transfer targets tiered by ownership, mathematically optimal squads via ILP solver, and Bayesian fixture difficulty ratings. These use custom scoring algorithms under active development - off by default, opt in when you're ready.

Both Classic and Draft formats from one tool.

## Demo

**xG analysis** - find underperformers due a correction and top xGI/90:

```
╭──────────────────────────────────────────────────────────╮
│ Underlying Stats Analysis (last 6 GWs)                   │
╰──────────────────────────────────────────────────────────╯

Top xGI per 90 (xG + xA):
 Player       Team   xG     xA    xGI/90  Goals  Assists
 Haaland      MCI   5.21   1.03    0.95     6      1
 Salah        LIV   3.82   2.15    0.91     4      3
 Mbeumo       BRE   3.45   1.87    0.82     2      2
 B.Fernandes  MUN   2.91   2.44    0.79     3      3
 Palmer       CHE   3.12   1.65    0.71     4      1

Underperformers (G+A < xGI, due a rise):
 Player       Team  G+A    xGI   Diff
 Saka         ARS    5   8.92   3.92
 Gordon       NEW    4   7.15   3.15
 Eze          CRY    3   5.88   2.88
```

**Player deep dive** - one command for everything about a player:

```
fpl player Salah -f -u

╭ Mohamed Salah (LIV) ─────────────────────────╮
│ MID · £13.2m · Form: 8.0 · Own: 42.1%        │
│ Status: Available                              │
╰───────────────────────────────────────────────╯

Upcoming Fixtures:
 GW32  FUL (H)  FDR 2
 GW33  tot (A)  FDR 4
 GW34  WHU (H)  FDR 2

Understat Analysis:
 npxG: 14.2  xA: 8.1  xGChain: 28.4  Shots: 89
```

## Quick Start

Requires Python 3.11+.

```bash
pipx install fplkit
fpl init
```

`fpl init` configures your FPL IDs and optional features. With just an entry ID you get the full data toolkit. See [Configuration](#configuration) for optional tiers.

**Alternatives:** `uv pip install fplkit` or `pip install fplkit`.

**Browser scraping** (`fpl squad sell-prices --refresh`) needs Playwright:

```bash
pipx install 'fplkit[scraper]'
playwright install chromium
```

## What You Can Do

### After the Gameweek - "How did I do?"

| Command | What you get |
|---------|-------------|
| `fpl status` | GW result, deadline countdown, rank movement, flagged players |
| `fpl review` | Full post-GW breakdown with player points, transfer assessment, league standings |
| `fpl league` | Live league standings with best/worst performers |
| `fpl league-recap` | Entertainment-first recap with awards - designed for the group chat |

```bash
fpl status                       # Quick pulse check
fpl review --save --summarise    # Full review with LLM narrative (requires API keys)
fpl league-recap --summarise     # Shareable recap with editorial
```

The `--summarise` flag on `review` and `league-recap` calls configured LLM providers. All other commands in this section use pure API data.

### Scouting - "Who should I look at?"

| Command | What you get |
|---------|-------------|
| `fpl stats` | Filterable player rankings by any stat |
| `fpl player NAME` | Deep dive on a specific player (fixtures, xG, history, Understat) |
| `fpl history` | Career arc data across 3 seasons for all players |
| `fpl xg` | xG/xA analysis - find over/underperformers |
| `fpl price-changes` | Price risers and fallers for the gameweek |
| `fpl price-history` | Season-long price trajectory with trend and momentum |

```bash
fpl stats -p MID -s form --available-only    # Midfielders by form, excluding injured
fpl player Salah -f -u                       # Salah with fixtures + Understat analysis
fpl price-history -n 4 -s price_slope        # Bandwagon detection (last 4 GWs)
```

See [Command Reference](docs/command-reference.md) for detailed flag documentation and scoring formulas.

### Before the Deadline - "What should I do?"

| Command | What you get |
|---------|-------------|
| `fpl squad` | Squad health: form, injuries, position analysis, recommendations |
| `fpl squad grid` | Fixture difficulty grid for your players (next 6 GWs) |
| `fpl preview` | Full pre-deadline analysis report |
| `fpl squad sell-prices` | Squad sell prices and budget breakdown *(classic)* |

```bash
fpl squad grid -n 8 -w Mbeumo   # 8-GW fixture grid with a watch list player
fpl preview --save --scout       # Full analysis + BUY/SELL research (requires API keys)
```

### Strategic Planning - "What's the landscape?"

| Command | What you get |
|---------|-------------|
| `fpl fdr` | Fixture difficulty runs, blanks, doubles, squad exposure |
| `fpl fixtures` | Next gameweek fixtures with FDR |
| `fpl chips` | Chip status: available, used, planned |
| `fpl chips timing` | Rule-based Free Hit / Bench Boost / Triple Captain signals |

```bash
fpl fdr --my-squad               # Your squad's exposure to blank/double GWs
fpl fdr --blanks                 # Confirmed + predicted blank/double GWs
fpl chips timing                 # Chip signals based on squad exposure
```

### Custom Analysis - "Give me recommendations"

These commands use fpl-cli's own scoring algorithms - off by default. Enable via `fpl init` or `custom_analysis: true` in settings.yaml.

| Command | What you get |
|---------|-------------|
| `fpl captain` | Ranked captain picks with matchup scoring (0-100) |
| `fpl targets` | Transfer targets tiered by ownership (template / popular / differential) |
| `fpl differentials` | Low-ownership high-potential picks with matchup scores |
| `fpl transfer-eval` | Compare OUT player vs IN candidates on outlook and lineup impact |
| `fpl waivers` | Free-agent waiver recommendations with drop suggestions *(draft)* |
| `fpl allocate` | Mathematically optimal 15-player squad via ILP solver *(classic)* |
| `fpl ratings` | Bayesian team strength ratings calculated from match results |

Enabling custom analysis also enriches mixed commands: `fpl stats` gains `--value` columns, `fpl xg` adds Value Picks, `fpl fdr` upgrades to Bayesian FDR with ATK/DEF split, and `fpl preview` includes positional fixture analysis.

```
Captain Picks - GW32 (All Players)

Top Captain Options:
 #  Player       Team  Score   Atk   Def  Form±   Pos±  Fixture
 1  B.Fernandes  MUN   100.0   8.8   5.0  +0.39  +0.63  LEE
 2  Gordon       NEW    81.0   6.2   4.6  -0.06  +0.11  cry
 3  Saka         ARS    79.0   7.1   5.0  +0.33  +0.63  BOU

Recommended Captain: B.Fernandes
Reasons: Strong attack matchup (8.8) · In great form (11.0) · Primary penalty taker
```

```
Optimal Squad (GW32-GW37)
    Player       Pos  Team  Price  Quality  Captain
 >  B.Fernandes  MID  MUN   £9.2m      100  GW32,GW35
 >  Saka         MID  ARS   £9.8m       93  GW34,GW37
 >  Haaland      FWD  MCI  £14.8m       91  GW33,GW36
 >  Gordon       MID  NEW   £7.4m       81
 >  Cunha        FWD  WOL   £7.0m       76
    ...

 Formation: 4-4-2  |  Budget: £99.9m / £100.0m (£0.1m remaining)
```

## Configuration

Run `fpl init` to configure interactively. Settings stored in your platform's config directory (override with `FPL_CLI_CONFIG_DIR`).

**Required:** Your FPL classic entry ID or draft league + entry IDs.

**Optional tiers** (each independent, enable any combination):
- **Custom Analysis** - captain picks, transfer targets, differentials, waivers, allocate, ratings, value scores, Bayesian FDR. Off by default - uses custom scoring algorithms under active development
- **League ID** - enables standings, fines, and league recaps
- **LLM providers** - enables `--summarise` and `--scout` flags (Perplexity, Anthropic, OpenAI, or any OpenAI-compatible API)
- **FPL credentials** - enables `fpl squad sell-prices` (browser scraping). Uses your own FPL login to read your sell prices from the FPL website. Automated access may violate FPL website terms - use at your own risk
- **FOOTBALL_DATA_API_KEY** - enables league table in `fpl review`

```bash
# LLM providers (for --summarise and --scout)
export PERPLEXITY_API_KEY="your-key"    # Research role
export ANTHROPIC_API_KEY="your-key"     # Synthesis role
```

See [Configuration Reference](docs/command-reference.md#configuration-reference) for the full `settings.yaml` schema.

## How It Works

fpl-cli fetches data from the FPL API (players, fixtures, teams), FPL Draft API (standings, waivers), Understat (npxG, xGChain), and vaastav's GitHub dataset (3 seasons of historical data). Pydantic models normalise FPL's jargon (`element` → player, `event` → gameweek). Scoring services combine these sources to produce ranked recommendations. Rich renders the output. LLM features are opt-in via pluggable providers - configure any OpenAI-compatible API.

## Design Decisions

- **Between-gameweek focus.** No live mid-GW scores - tools like LiveFPL serve that job.
- **Data first, opinions opt-in.** Core commands show aggregated data from multiple sources. Custom analysis (scoring, rankings, recommendations) is a separate toggle so users can trust the data layer without buying into experimental algorithms.
- **No transfer planner.** Multi-week transfer sequencing is better in a spreadsheet. The CLI provides the inputs (`fdr`, `chips timing`, `fixtures`).
- **Draft parity.** Most commands work for both classic and draft formats. Draft support focuses on free-agent pickups via the waiver system - trade recommendations between managers are out of scope.
- **Agent-friendly.** `--format json` on key commands with a consistent envelope. See [For Agent Builders](#for-agent-builders).
- **LLM features are opt-in.** Core analysis works without any API keys. LLM providers add narrative and research capabilities.

## Known Limitations

- **Classic league scoring only.** No Head-to-Head or H2H knock-out league scoring. Both classic and draft formats are supported.
- **One entry per format.** Configure one classic team and one draft league.
- **League standings show top 50.** Covers most invitational leagues. Larger leagues see partial results.
- **Pending transfers not visible.** The FPL API only exposes picks for completed gameweeks.

## Data Sources

- [FPL API](https://fantasy.premierleague.com/api/bootstrap-static/) - players, fixtures, teams, gameweeks
- [FPL Draft API](https://draft.premierleague.com/api/) - league standings, waivers, transactions
- [Understat](https://understat.com) - npxG, xGChain, xGBuildup
- [football-data.org](https://www.football-data.org/) - Premier League standings, team ratings prior fallback
- [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League) - 3 seasons of historical CSV data (MIT)
- Team Ratings - 4-axis strength ratings auto-calculated from fixture results

Football data provided by the [Football-Data.org API](https://www.football-data.org/). Player and fixture data is property of the Premier League. Expected goals data is property of [Understat](https://understat.com). Historical data sourced from [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League) (MIT). This tool fetches data at runtime and does not redistribute or bundle third-party data.

See [Command Reference](docs/command-reference.md) for detailed documentation on every command, scoring formulas, and the full configuration schema.

## For Agent Builders

### JSON Output

Commands with `--format json` emit a consistent envelope for scripting and agent consumption:

```json
{
  "command": "stats",
  "metadata": {"gameweek": null, "format": "classic", "custom_analysis": true, "filters": {}},
  "data": [...]
}
```

**JSON-capable commands:** `captain`, `chips`, `chips timing`, `differentials`, `fdr`, `fixtures`, `history`, `player`, `price-history`, `squad`, `squad grid`, `stats`, `status`, `targets`, `transfer-eval`, `xg`, `waivers`

Errors go to stderr as `{"command": "...", "error": "..."}` with exit code 1. Stdout always contains valid JSON or nothing - safe for piping to `jq`.

Commands affected by the custom analysis toggle include `"custom_analysis": true/false` in metadata so agents can detect schema variance.

```bash
fpl stats --format json -p MID -s expected_goal_involvements  # MID shortlist for agents
fpl player Salah -f -d -u -H --format json                   # All player data sections
fpl status --format json                                      # GW state for agents
fpl fdr --blanks --format json                                # Blank/double GW schedule
```

### Showcase Skills

`.agents/skills/` contains ready-made agent workflows that compose fpl-cli commands into end-to-end FPL analysis. These are reference implementations - use them directly or adapt them to your setup.

| Skill | What it does | Compatibility |
|-------|-------------|---------------|
| **gw-prep** | Full gameweek recommendations: fixtures, captain, transfers/waivers, bench order, chip timing. Dispatches parallel Classic + Draft analysis. | Claude Code (full), Codex/Cursor/Copilot (sequential) |
| **update-gw-prep** | Second-pass addendum after newsletters or new data arrives. Appends updates without modifying baseline recommendations. | All tools (full) |
| **squad-builder** | Build-from-scratch squad optimisation across 5 modes: Wildcard, Free Hit, Season Start (Classic/Draft), Re-draft. | Claude Code (full), Codex/Cursor/Copilot (sequential) |

Skills detect your configured format (classic, draft, or both) and skip irrelevant sections automatically. Each skill's `SKILL.md` contains the full workflow with `<!-- ADAPT: ... -->` comments marking customisation points.

Claude Code discovers skills via `.claude/skills/` (symlink to `.agents/skills/`). Other tools read `.agents/skills/` directly. For a full inventory of commands, agents, and skills, see [`.agents/TOOLS.md`](.agents/TOOLS.md).

### Output Style

`.claude/output-styles/fpl-mate.md` provides an opinionated FPL conversation mode - use it when you want to jam on transfer decisions, captaincy, or chip strategy with an AI that pulls data before giving views.

## Development

```bash
git clone https://github.com/rossgroomio/fpl-cli.git
cd fpl-cli
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
fpl init
```

```bash
ruff check fpl_cli/    # Lint
pyright fpl_cli/       # Type check
pytest tests/          # Tests
```

The `.agents/skills/` scripts import `fpl_cli` internals and require the editable install above - they won't work from a `pipx install`.
