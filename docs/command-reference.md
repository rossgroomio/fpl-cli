# Command Reference

Detailed flag documentation and configuration for fpl-cli.
For an overview of what each command does, see the [README](../README.md).
For scoring formulas and methodology, see the [Custom Analysis Guide](custom-analysis.md).

## Timestamps

All user-facing timestamps (gameweek deadlines, fixture kickoffs, `generated_at` stamps in reports) render in **UK local time** (`Europe/London`). The display switches between **GMT** and **BST** automatically based on the date of the timestamp, so summer deadlines appear as BST and winter deadlines as GMT. The timezone label is always shown alongside the time (e.g. `Sat 18 Apr, 18:30 BST`). Internal datetime math (countdowns, comparisons) remains in UTC.

## LLM Transparency

Most fpl-cli output is **deterministic computation** - fixed algorithms applied to data from the FPL API, Understat, and other sources. A handful of commands optionally call an LLM for narrative content:

| Command / Flag | LLM Role | What It Does |
|---|---|---|
| `preview --scout` | Research (Perplexity) | Web-grounded BUY/SELL recommendations from FPL community sources |
| `preview --dry-run` | *None* | Builds scout prompts without calling the LLM |
| `review --summarise` | Research + Synthesis | Community narrative (research) + personal analysis (synthesis) |
| `league-recap --summarise` | Synthesis (Anthropic) | Newsletter-style editorial naming names and calling out decisions |

Everything else - captain picks, targets, differentials, waivers, FDR, team ratings, squad allocation, all stats commands - is pure computation. No AI involved.

## Format & Gating

### Format Awareness

Commands are classified by format applicability:

| Category | Commands |
|---|---|
| **Classic only** | `captain`, `targets`, `differentials`, `chips`, `credentials` |
| **Draft only** | `waivers` |
| **General** | Everything else (format-gated sections within) |

`FormatAwareGroup` auto-hides inapplicable commands in `--help` based on configured format. Format resolved from settings (`classic_entry_id` / `draft_league_id`) or `FPL_FORMAT` env var.

### Custom Analysis Gating

Commands are independently classified by the `custom_analysis` toggle:

| Category | Commands | When opted out |
|---|---|---|
| **Pure-experimental** | `captain`, `targets`, `differentials`, `waivers`, `allocate`, `transfer-eval`, `ratings` | Hidden from `--help`; invoking one names the toggle |
| **Mixed** | `stats`, `xg`, `fdr`, `preview` | Experimental columns/sections stripped |
| **Data-only** | Everything else | No change |

Both filters (format and experimental) are independent and must both pass.

Running a gated command reports the gate and the `settings.yaml` fpl-cli is actually reading, rather than click's default "No such command" — which would otherwise suggest the very command it had just refused.

## Player Analysis

### Captain Picks

Rank captain options by combining matchup score, recent form, xGI, home advantage, and penalty taker status. Scores normalised to 0-100.

```bash
fpl captain            # Your squad
fpl captain --global   # All players (top 30 by form/xG)
fpl captain --format json
```

Output columns: Score, Atk, Def, Form±, Pos±.

Score combines position-weighted matchup quality, recent form (with trajectory adjustment), xGI per 90, and a consistency tiebreaker (CV-xGI percentile, phased in GW6-10). DGW players are scored across both fixtures. See [Captain Score](custom-analysis.md#captain-score) for the full formula, [Matchup Scoring](custom-analysis.md#matchup-scoring) for column definitions.

### Transfer Targets

Find high-performing players across all ownership levels.

```bash
fpl targets                  # All ownership levels
fpl targets --min-own 30     # Template players only (30%+ owned)
fpl targets -m 200           # Require 200+ minutes played
fpl targets --format json    # JSON envelope (metadata: {})
```

Groups players into tiers:
- **Template** (>30% owned): Consensus picks
- **Popular** (15-30% owned): Emerging picks
- **Differential** (<15% owned): Low-ownership value

Target score combines xG metrics, form, PPG, 3-GW matchup quality, and consistency (CV-xGI percentile bonus, phased in GW6-10), attenuated by position multiplier (GK 0.7, DEF 0.85), and normalised to 0-100. Subject to [early-season shrinkage](custom-analysis.md#early-season-confidence-gw1-10). See [Target Score](custom-analysis.md#target-score) for the full formula.

### Transfer Evaluation

Compare an OUT player against IN candidates on two scoring horizons.

```bash
fpl transfer-eval --out Palmer --in "Salah,Mbeumo,Diaz"
fpl transfer-eval --out Palmer --in Salah --format json
```

| Flag | Default | Description |
|------|---------|-------------|
| `--out` | *(required)* | Player to transfer out |
| `--in` | *(required)* | Comma-separated IN candidates |
| `--format` | `table` | `table` or `json` |

Output columns:
- **Outlook** - multi-GW quality delta (target score 0-100). Higher = better long-term hold.
- **This GW** - single-GW lineup impact delta (lineup score 0-100). Higher = better starter this week.
- **Fixtures** - next 3 opponents with FDR
- **Form** - FPL form (last 30 days PPG)
- **Status** - availability indicator
- **Avail** - historical availability rate (recency-weighted starts across previous seasons). Null when no historical data.
- **Quality** - price-independent player quality (0-100), normalised against a position-specific ceiling. Elite-within-position index — cross-position comparisons not meaningful (GK/DEF/MID/FWD use different ceilings by design). See [Quality & Value Scores](custom-analysis.md#quality--value-scores). Null when no Understat match.
- **Value** - quality per GBP million (`quality_score / price`). Higher = more output per pound. Null when no Understat match or price is 0. *(classic only)*
- **Price** - current price *(classic only)*
- **Budget** - affordability gap: `bank + sell_price - in_price` *(classic only, requires scraper cache)*

OUT player shows absolute scores. IN candidates show deltas for Outlook/This GW only (+15, -3); Quality and Value show absolute values for all players (no delta - value is a per-player efficiency metric). Sorted by Outlook delta descending.

Both scores use [early-season shrinkage](custom-analysis.md#early-season-confidence-gw1-10). Outlook uses the [ownership scoring family](custom-analysis.md#ownership-scoring). This GW uses [single-GW scoring](custom-analysis.md#single-gw-scoring).

**Draft note:** Outlook rankings may differ from `fpl waivers` output due to different weighting emphasis - target score uses more xG, less form than waiver score.

### Differentials

Find low-ownership players with high potential.

```bash
fpl differentials            # <5% owned, 60+ minutes played
fpl differentials -t 3       # <3% owned (ultra-differentials)
fpl differentials -m 200     # Require 200+ minutes played
fpl differentials --format json  # JSON envelope (metadata: {gameweek})
```

Differential score combines xG metrics, form, ownership bonus, 3-GW matchup quality, and consistency (inverted CV-xGI bonus - volatile players score higher, phased in GW6-10), attenuated by position multiplier (GK 0.7, DEF 0.85), and normalised to 0-100. Subject to [early-season shrinkage](custom-analysis.md#early-season-confidence-gw1-10). See [Differential Score](custom-analysis.md#differential-score) for the full formula.

### Waiver Recommendations

Analyse your draft squad and suggest free-agent pickups.

```bash
fpl waivers
fpl waivers --format json
```

Identifies squad weaknesses by position, ranks available free agents by waiver score, suggests who to drop for each pickup. This covers the waiver wire (unclaimed players) only - trade recommendations between managers are not in scope.

Waiver score combines xGI, form, PPG, 3-GW matchup quality, and consistency (CV-xGI percentile bonus, phased in GW6-10), attenuated by position multiplier (GK 0.7, DEF 0.85), and normalised to 0-100. Uses a stricter minutes factor than target/differential because draft waivers are a season commitment. Subject to [early-season shrinkage](custom-analysis.md#early-season-confidence-gw1-10). See [Waiver Score](custom-analysis.md#waiver-score) for the full formula.

## Fixture & Strategic Planning

### Squad Allocation (Classic only)

Select the mathematically optimal 15-player squad using an ILP (Integer Linear Programming) solver.

```bash
fpl allocate                        # Default: £100m budget, 6 GW horizon
fpl allocate --budget 95.0          # Custom budget (e.g., wildcard remaining)
fpl allocate --horizon 8            # Season start (8 GW lookahead)
fpl allocate --horizon 1 --bench-discount 0.01  # Free Hit (single GW, minimal bench)
fpl allocate --bench-boost-gw 35    # Bench Boost on GW35 (bench valued at 100% for that GW)
fpl allocate --bench-boost-gw 35 --horizon 4  # BB-focused (shorter horizon concentrates effect)
fpl allocate --free-transfers 3     # Weight near-term GWs more (3 banked FTs = more flexibility)
fpl allocate --sell-prices /tmp/sell-prices.json  # Use actual sell prices for WC/FH budgeting
fpl allocate --format json          # JSON output for scripting / skill integration
```

**Flags:**
| Flag | Default | Description |
|------|---------|-------------|
| `--budget` | 100.0 | Total budget in GBP millions |
| `--horizon` | 6 | Number of gameweeks to optimise over |
| `--bench-discount` | 0.15/0.05 | Bench player discount factor, applied uniformly (overrides per-position defaults) |
| `--bench-boost-gw` | - | GW to play Bench Boost; bench discount overridden to 1.0 for that GW. Use `--horizon 3-4` for BB-focused planning |
| `--free-transfers` | 1 | Banked free transfers (0-5). More FTs = solver weights near-term gameweeks more heavily, favouring short-term picks you can transfer out later |
| `--sell-prices` | - | Path to sell-prices JSON file (from `fpl squad sell-prices --format json`). Solver uses sell prices for owned players in budget constraint. Budget auto-computed as `sum(sell_prices) + bank` unless `--budget` is explicitly set |
| `--format` | table | `table` or `json` |

Scores ~500 eligible players, adjusts for fixture difficulty over the planning horizon, then solves for the budget-constrained optimum across all 7 valid formations. See [Squad Allocator](custom-analysis.md#squad-allocator) for scoring methodology, fixture coefficients, and solver detail.

**JSON output fields (horizon >= 2):** `id`, `web_name`, `team`, `position`, `price`, `quality_score` (0-100), `raw_quality` (float), `role` (starter/bench), `captain_gws`. Metadata includes `formation`, `budget_used`, `budget_remaining`, `captain_schedule`, `solver_status`.

**JSON output fields (horizon 1):** Same as above but the score field is `single_gw_score` (0-100) instead of `quality_score`.

**Score field semantics:** At `--horizon >= 2` the `quality_score` field is normalised against a position-specific VALUE-family ceiling, matching `fpl player` / `fpl stats --value` / `fpl transfer-eval` for cross-command consistency — elite GKs, DEFs, MIDs and FWDs each land in comparable 0-100 bands *within their own position*. At `--horizon 1` the field is named `single_gw_score` because it comes from a different scoring family (`GW_SELECTION_WEIGHTS` + fixture-matchup term) and is normalised against a single cross-position `STARTING_XI_CEILING`; DEFs and GKs display noticeably lower than MIDs/FWDs for the same real-world quality. The different name signals that the two fields are not comparable. Use `raw_quality` for a position-agnostic ranking at either horizon.

### Fixture Difficulty (FDR)

Analyse upcoming fixture runs with difficulty ratings, blank/double GW detection, and optional squad exposure.

**With custom analysis enabled:** FDR values derive from [Team Ratings](custom-analysis.md#team-ratings), auto-refreshed from fixture results on a rolling 12-GW window. Unified 1-7 scale where 1 = easiest. Position-specific FDR (`-p atk/def`) available.

**Without custom analysis:** Falls back to raw FPL API difficulty ratings (1-5 scale from `home_difficulty`/`away_difficulty`). Single FDR column, no ATK/DEF split. `--blanks` and `--my-squad` work in both modes.

```bash
fpl fdr                              # Next 6 GWs (difference mode, all positions)
fpl fdr -m opponent                  # Opponent-rating-only mode
fpl fdr -p atk                       # Best fixtures for FWD/MID
fpl fdr -p def                       # Best fixtures for DEF/GK
fpl fdr --from-gw 28 --to-gw 34     # Custom GW window (chip planning)
fpl fdr --my-squad                   # Squad exposure to blank/double GWs
fpl fdr --blanks                     # Blank/double GW schedule (confirmed + predicted)
fpl fdr --format json                # JSON envelope (metadata: {gameweek, format, mode, position})
fpl fdr --blanks --format json       # JSON envelope (metadata: {gameweek, mode: "blanks", from_gw, to_gw})
```

#### FDR Modes (`-m`)

- `difference` (default): Accounts for both team strength and opponent. A strong attack vs a weak defence scores easier than a weak attack vs the same defence.
- `opponent`: Based solely on the opponent's rating. Ignores the player's team strength.

#### Position Filters (`-p`)

- `all` (default): General FDR plus ATK and DEF columns.
- `atk`: Sort by best fixtures for FWD/MID (opponent's defensive weakness).
- `def`: Sort by best fixtures for DEF/GK (opponent's offensive threat).

#### Squad Exposure (`--my-squad`)

Requires `classic_entry_id` in settings. Fetches your current squad and cross-references it against confirmed and predicted blank/double GWs:

```
Squad Exposure:
  GW31 BLANK: 4/15 affected (3 starters) — Salah, TAA, Robertson, Gakpo
  GW33 DOUBLE: 9/15 affected (8 starters) — Palmer, Nkunku, ...
```

- Starter projection: 1 GK + up to 5 DEF + 5 MID + 3 FWD (max 11)
- Blanks shown in red/yellow; doubles in green/cyan
- Handles Free Hit chip reversion (uses GW before FH for actual squad)
- Primary use: timing Free Hit blanks and Bench Boost doubles

### Team Ratings

4-axis team strength ratings on a 1-7 scale derived from actual match results. The data source behind FDR, captain picks, squad grid, and other fixture-aware commands.

> **Not FPL's FDR.** The FPL website assigns static difficulty ratings that rarely change. fpl-cli instead calculates ratings from real match data on a rolling window, so they reflect current form rather than pre-season expectations.

```bash
fpl ratings                        # Display current ratings (auto-refreshes if stale)
fpl ratings update                 # Force recalculate from fixture results
fpl ratings update --use-xg        # Recalculate using Understat xG (less noise, full season)
fpl ratings update --since-gw 15   # Recent form only (actual goals)
fpl ratings update --dry-run       # Preview changes without saving
```

Before GW1 there are no results to rate teams on, so ratings are estimated from the previous season (promoted teams from Championship form) and every fixture-difficulty view says so until real results land.

Ratings are tied to the season that produced them. A file carried across a season boundary is ignored rather than served, because it rates the three relegated clubs and knows nothing about the three promoted ones. Ratings that cover the wrong set of clubs are called out by name — `team_ratings.yaml is missing COV, HUL, IPS and still rates BUR, WHU, WOL` — which catches a rollover that a "days old" check cannot: a file rebuilt in early August is new by date and still describes last season's league.

See [Team Ratings](custom-analysis.md#team-ratings) for calculation methodology, axes, early-season blending, pre-season estimates, and manual overrides.

### Chips

View and plan chip usage across the season.

```bash
fpl chips                                  # Show chip status
fpl chips add wildcard --gw 26             # Plan wildcard for GW26
fpl chips add freehit --gw 29 -n "BGW"     # Plan free hit with notes
fpl chips remove --gw 26                   # Remove planned chip
fpl chips timing                           # Rule-based FH/BB/TC signals
fpl chips sync                             # Sync used chips from FPL API
```

**Chip types:** `wildcard`, `freehit`, `bboost`, `3xc`

**Workflow:** `sync` (fetch usage) -> `timing` (analyse signals) -> `add` (record decision) -> `sync` (verify after playing).

#### Chip Timing Thresholds

| Chip | Trigger | Strength |
|------|---------|---------|
| FH | 5+ squad players in a blank GW | Strong |
| FH | 3+ squad players in a blank GW | Possible |
| BB | 8+ squad players in a double GW | Strong |
| BB | 6+ squad players in a double GW | Possible |
| TC | Best DGW candidate has avg FDR <= 3.0 | Strong |
| TC | Best DGW candidate has avg FDR <= 4.0 | Possible |

Thresholds apply to the full 15-player squad (not just projected starters). Chips already used are excluded. Planned chips highlighted `[planned]` inline. Stored in `data/chip_plan.json`.

## Player Data

### Player List

Query all players with filtering and sorting. Default: top 20 by total points.

```bash
fpl stats                                            # Top 20 by total points
fpl stats -p DEF -s goals_scored -n 10               # Top 10 defenders by goals
fpl stats -t LIV                                     # All Liverpool players
fpl stats -s now_cost -r -n 10                       # 10 cheapest players
fpl stats --min-minutes 900 -s expected_goals        # Top xG (min 900 mins)
fpl stats -p FWD -s form --available-only            # FWDs by form, excl. unavailable
fpl stats --format json -p MID -s expected_goal_involvements  # JSON for agents
fpl stats --value -p MID                             # MIDs ranked by value/£m
fpl stats --value --sort quality_score -p FWD        # FWDs ranked by within-position quality_score
fpl stats --value --window 3 -p MID                  # Rolling pts/£m over last 3 qualifying GWs
```

Filter by position (`-p`), team (`-t`), minimum minutes (`--min-minutes`). Sort by any stat field (`-s`). Use `-r` for ascending. Limit with `-n`. Use `--available-only` / `-a` to exclude injured, suspended, and unavailable players (doubtful kept).

**Value columns** (`--value` / `-v`): Adds Quality (0-100), Quality/£m, and Rolling pts/£m columns. Default sort switches to `quality_per_m` when active. Requires Understat data - players without a match show `-`. Use with a position filter for best results; scoring all players takes longer. `quality_score`, `quality_per_m`, `pts_per_m`, `form_per_m`, and `rolling_pts_per_m` are available as `--sort` fields when `--value` is active. Requires `custom_analysis: true` - silently ignored when off. See [Quality & Value Scores](custom-analysis.md#quality--value-scores) for methodology.

**Rolling window** (`--window` / `-w`): Sets the lookback window for `rolling_pts_per_m` (range 3-10, default from config). Only applies when `--value` is active. `rolling_pts_per_m` measures points per million over the last N qualifying fixtures (minutes > 0), capturing recent form-adjusted value.

### Historical Data

Career-arc analysis from the [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League) (2022-25) and [Core-Insights/Fantasy-Premier-League](https://github.com/Core-Insights/Fantasy-Premier-League) (2025-26+) datasets.

```bash
fpl player Salah --history   # Individual career arc
fpl history                  # All players (compact, for squad-builder)
fpl history --format json
```

**Signals:**
- **pts_per_90 trend** - Points per 90 minutes across seasons (improving/declining)
- **cost trajectory** - Price movement across seasons
- **xGI per 90 trend** - Expected goal involvement trend (from 2022-23)
- **minutes per start** - Durability proxy (injury/rotation risk)

### Price History

Season-long price trajectory and transfer momentum from historical gameweek-level data. Complements `fpl price-changes` (this-GW snapshot) with the historical arc.

```bash
fpl price-history                            # Full season, sorted by price change
fpl price-history -n 4 -s price_slope        # Fastest recent risers (bandwagon detection)
fpl price-history -n 6 -s transfer_momentum  # Highest net transfers over last 6 GWs
fpl price-history -n 4 -s price_acceleration # Players whose rise is speeding up
fpl price-history -p FWD -s price_slope      # Forward price risers by trend
fpl price-history -s price_change -r         # Biggest fallers (ascending sort)
fpl price-history --format json
```

#### Table Columns

- **GW{X} / Now** - Price at window start and current price. Column header shows earliest GW across displayed players.
- **+/-** - Total price change across the window.
- **Trend** - Rate of price change per GW (linear slope). Higher = rising faster.
- **Accel** - Quadratic coefficient measuring whether price movement is speeding up or slowing down. Positive = rises accelerating or falls decelerating.
- **Momentum / Net Transfers** - Net transfers (in minus out). Without `--last-n`: rolling 5-GW signal. With `--last-n`: sum over specified window.

#### Slope vs Acceleration

`price_slope` measures how fast a price is moving (first derivative). `price_acceleration` measures whether the rate of change is itself changing (second derivative). A player rising steadily at +£0.1m/GW has high slope but near-zero acceleration. Sort by `price_slope` for bandwagon detection. Sort by `price_acceleration` for emerging trends (flat-then-rising).

#### Sorting

Sort by: `price_change` (default), `price_slope`, `price_acceleration`, `transfer_momentum`, `price_current`. Descending by default. Use `-r` for ascending. Filter by position (`-p`) or team (`-t`). Limit results with `-l` (default: 30).

When historical data is stale (>3 GWs behind), trend/accel/momentum columns are hidden and the command falls back to live API price change only.

### Understat Metrics

Player analysis is enriched with data from [Understat](https://understat.com):

| Metric | Description |
|--------|-------------|
| **npxG** | Non-penalty expected goals. Shows true open-play attacking quality. |
| **xGChain** | Total xG of every possession chain a player is involved in. High xGChain with low xG/xA = consistently dangerous without finishing. |
| **xGBuildup** | Same as xGChain but excludes shooter and assister. Pure "table-setting" metric. |
| **penalty_xG** | xG minus npxG. Flags players whose xG is inflated by penalty duties. |

These metrics appear in `fpl player`, `fpl xg`, and gameweek reports. If Understat is unavailable, agents fall back to FPL-only xG data.

#### Quality and Value Scores

When a player has an Understat match, `fpl player` computes and displays two additional metrics:

| Field | Description |
|---|---|
| **quality_score** | 0-100 normalised player output quality. See [Quality & Value Scores](custom-analysis.md#quality--value-scores). |
| **quality_per_m** | `quality_score / price` (per £m). Within-position budget efficiency. See [Quality & Value Scores](custom-analysis.md#quality--value-scores). |
| **pts_per_m** | `total_points / price` (per £m). Raw season points efficiency. |
| **form_per_m** | `form / price` (per £m). Recent form efficiency. |
| **rolling_pts_per_m** | Points per £m over the last N qualifying fixtures (configurable via `--window`). Captures recent form-adjusted value. |
| **adj. npxG/90** | Fixture-adjusted non-penalty xG per 90: npxG normalised by opponent Elo over a rolling window. Shown alongside raw when the adjustment changes the value. See [Fixture-Adjusted npxG](custom-analysis.md#fixture-adjusted-npxg). |

`quality_score` and `quality_per_m` are `null` when no Understat match exists. In JSON output (`--format json`), they appear under `info.quality_score` and `info.quality_per_m`. In the Rich panel, they appear as `Quality: 85 | Value: 11.3/£m`.

#### FPL Predicted Points

The FPL API provides gameweek point predictions for every player:

| Field | Description |
|---|---|
| **ep_next** | FPL's predicted points for the next gameweek. Shown as `xPts` on the Points/PPG panel line. Available as a `--sort` field in `fpl stats`. |
| **ep_this** | FPL's predicted points for the current gameweek (freezes after GW deadline, enabling predicted-vs-actual comparison with `event_points`). JSON only - not shown in the panel. |

Both fields are `None` on the `Player` model when FPL provides no projection (e.g. unavailable/injured players, end of season). The Rich panel for `fpl player` omits the `xPts` segment of the Points line in that case, and the `fpl stats` table renders the cell as `—`. JSON output emits `null` when FPL provides no projection, so consumers can distinguish missing data from a genuine `0.0` projection — `info.ep_next`/`info.ep_this` for `fpl player`, and the top-level `ep_next`/`ep_this` record fields in `fpl stats --format json`.

#### Player Detail Flags

**`--detail` (`-d`)**: GW-by-GW match performance from the FPL API. Shows gameweek, opponent, minutes, goals, xG, assists, xA, and points for the last 10 matches. For FWD/MID players with sufficient recent history, also shows xGI sustainability: the per-match GI-xGI divergence and the resulting form modifier (e.g. `xGI Sustainability: +0.18/match -> 0.94x form`).

**`--understat` (`-u`)**: Combined Understat analysis: shot analysis (total shots, shots on target, average xG per shot, body part split, situation breakdown) and situation profile. Includes a data-through date caveat since Understat data can lag behind the live season.

## Squad

### Squad Analysis

Analyse squad health and fixture outlook.

```bash
fpl squad                              # Squad health (both formats)
fpl squad --format json                # JSON envelope (metadata: {gameweek, format})
fpl squad grid                         # Fixture difficulty grid (next 6 GWs)
fpl squad grid -n 8 -w Mbeumo          # 8-GW grid with watch list player
fpl squad grid --format json
```

### Sell Prices & Transfer Affordability

Scrape actual sell prices from the FPL website using browser automation.

```bash
fpl squad sell-prices              # Show cached squad budget
fpl squad sell-prices --refresh    # Re-scrape from FPL (requires login)
fpl squad sell-prices --visible    # Show browser for debugging
fpl squad sell-prices --format json > /tmp/sell-prices.json  # JSON output for allocator
```

**Why this matters:** Sell prices differ from market prices. You keep only half of any price rise:
- Bought Haaland at £14.0m, now £15.1m -> sell price is £14.5m

**Setup:**
```bash
playwright install chromium
fpl credentials set          # Store FPL email + password in system keyring
```

**Troubleshooting (TLS-inspecting proxies):** If `--refresh` fails with `ERR_CERT_AUTHORITY_INVALID` (corporate MITM proxy, Zscaler/Netskope, or sandboxed cloud environments like Claude Code on the web), set `FPL_BROWSER_IGNORE_CERTS=1`. Chromium uses its own cert store and ignores `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`, so this flag tells it to skip cert validation on launch. Opt-in only — leave unset on trusted networks.

Output: free transfers, bank balance, squad sell prices, total team value. Data cached to `team_finances.json` in the data directory (see [Directories](#directories)) for 12 hours.

**Wildcard / Free Hit workflow:** Use `--format json` to export sell prices, then pass to `fpl allocate --sell-prices` for accurate budgeting:
```bash
fpl squad sell-prices --format json > /tmp/sell-prices.json
fpl allocate --sell-prices /tmp/sell-prices.json  # Budget auto-computed from sell values + bank
```

## Reports

### Gameweek Preview

Pre-gameweek analysis covering fixtures, team form, squads, and transfer activity.

```bash
fpl preview                  # Full pre-GW analysis
fpl preview --save           # Save report to output directory
fpl preview --save --scout   # Also run deep research via research provider
fpl preview --dry-run        # Build scout prompts without calling LLMs
```

**Sections:** fixture analysis with FDR, team form (all 20 PL teams, last 6 matches), classic squad with injury status and price changes, draft squad, top xGI/90 performers.

**Scout analysis** (`--scout`): Uses the research provider (LLM) to generate FPL expert-style BUY/SELL recommendations from web and social sources. Generates two versions: referenced (with citations) and clean (for LLM use).

### Gameweek Review

Post-gameweek analysis covering both classic and draft.

```bash
fpl review                        # Review last completed gameweek
fpl review -g 20                  # Review specific gameweek
fpl review --save --summarise     # Save with LLM-generated summary
fpl review --debug                # Save LLM prompts/responses to data/debug/
fpl review --dry-run              # Build prompts without calling LLMs
```

**Classic:** team summary, player-by-player breakdown (captain doubled/tripled), transfer assessment, league standings with nearby rivals, best/worst performers.

**Draft:** squad breakdown, transaction assessment, league standings, best/worst performers.

**Results:** all fixtures with scores, goal scorers, assists, and bonus points.

**LLM summary** (`--summarise`): Community narrative via research provider, personal analysis via synthesis provider.

### League Recap

Entertainment-first post-gameweek report for the whole league.

```bash
fpl league-recap                # Recap last completed gameweek
fpl league-recap --save         # Save report
fpl league-recap --summarise    # Add LLM editorial narrative
fpl league-recap --draft        # Use draft league
```

**Awards:** GW winner/loser, biggest bench haul, best/worst captain, transfer/waiver genius and disaster.

**Standings movement:** position changes derived from point differentials, per-manager highlights.

**Fines:** evaluates fines for every manager (not just you) when configured.

**LLM editorial** (`--summarise`): Newsletter-style narrative via synthesis provider. Names names, calls out decisions.

## Season Preview Intel

Hand-curated per-team notes covering what the API and historical data cannot see before
a season starts: who is nailed on, who is injured into the autumn, who took over set
pieces, how a squad looks after the summer window.

The content is yours. Nothing ships but an annotated example — preview prose belongs to
whoever wrote it, so there is nothing to distribute. Sources are interchangeable: a
200-word blurb and a long data piece fill the same schema at different fidelity.

```bash
fpl intel                       # Coverage across the league and what it permits
fpl intel --show-decay          # When each kind of intel expires
fpl intel -g 5                  # Show intel as it will look at GW5
fpl intel --format json         # For scripts and agent skills
fpl intel schema                # The file format, every field explained
fpl intel init                  # Scaffold one empty file per Premier League team
fpl intel init --force          # Overwrite existing files
fpl intel show ARS              # One team's intel, aged to the current gameweek
fpl intel resolve ARS           # Match player names to FPL codes (dry run)
fpl intel resolve ARS --write   # Write the codes back, preserving your comments
fpl intel resolve ARS --all     # Re-resolve players that already have a code
```

**Location:** `<config dir>/previews/{TEAM}.yaml`, one file per team, named by FPL short
name (`ARS.yaml`). See [Directories](#directories).

### Decay

A preview is not one thing with one expiry. Each kind of claim is aged out at the point
something better supersedes it, so files stay on disk untouched all season and stop
influencing decisions on their own.

| Section | Full confidence | Expires | Superseded by |
|---------|-----------------|---------|---------------|
| `injuries` | GW1 | GW2 | the FPL API's own `news` and `chance_of_playing` fields |
| `transfers` | GW3 | GW4 | the summer window shutting; the roster is then authoritative |
| `projected_xi` | GW3 | GW7 | real `minutes` |
| `role_notes` | GW4 | GW9 | observed position and minutes |
| `set_piece_duty` | GW6 | GW13 | observed returns |
| `team_strength` | GW6 | GW13 | team ratings, which stop blending a prior after GW12 |
| `narrative` | GW6 | GW13 | as above |

Between full confidence and expiry the value tapers linearly and is reported per section
as `section_confidence`. A categorical field such as `status: starter` cannot be scaled
numerically, so the confidence is emitted alongside it for the consumer to weigh.

### Coverage gate

A partially-filled preview set is biased: written-up teams carry "nailed on, takes
corners" annotations and the rest carry nothing, so absence of a flag reads as absence
of merit. `metadata.coverage.usable_as` reports what the current set permits:

| Value | Condition | Permitted use |
|-------|-----------|---------------|
| `full` | 75%+ of teams covered | Support or oppose a pick |
| `negative_filter_only` | below that | Downgrade only — injuries, rotation risk. Never promote. |
| `none` | nothing loaded, or all expired | Ignore entirely |

Stubs from `fpl intel init` never count toward coverage until they are filled in.

### Files that are skipped

A file is ignored, with the reason printed to stderr and carried in
`metadata.warnings`, when it is unreadable, is not a mapping, declares an unknown
`schema_version`, is missing `team`, `source` or `published`, or belongs to a previous
season. The season check reads the explicit `season` label when present and falls back
to the `published` date — this is the guard against building a squad on last August's
opinions. A preview set that has drifted across promotion and relegation is reported
too.

### Name resolution

Preview prose names players the way a reader would — "Bruno Guimaraes" where the game
shows "Bruno G." — so `fpl intel resolve` matches names against the team's squad and
writes `element_code` (stable across seasons) back into the file. Accents and
punctuation are folded, so `Ødegaard` and `Odegaard` resolve alike. An exact match on a
display or full name wins outright; otherwise every query token must appear in the
player's combined names. **Ambiguity is reported, never guessed** — a silently wrong
code attaches intel to the wrong player. Writes are round-trip YAML, so hand-written
comments and formatting survive.

## Configuration Reference

Configuration uses two layers, deep-merged at runtime:

1. **`config/defaults.yaml`** (committed) - project defaults, no personal data
2. **`settings.yaml`** (user overrides) - in your platform config directory (`~/Library/Application Support/fpl-cli/` on macOS, `~/.config/fpl-cli/` on Linux, override with `FPL_CLI_CONFIG_DIR`)

Run `fpl init` to configure interactively. Only set values in `settings.yaml` that differ from defaults.

### Directories

fpl-cli writes to three directories, each resolved via `platformdirs` and overridable with an env var:

| Directory | Contents | Override |
|-----------|----------|----------|
| Config | `settings.yaml`, `.env` (credentials and API keys), `team_ratings_overrides.yaml`, optional `team_managers.yaml` (layered over the shipped copy per club) and `fixture_predictions.yaml` (replaces the shipped copy), the optional `previews/` directory of [season preview intel](#season-preview-intel), plus the `output/` and `research/` report directories | `FPL_CLI_CONFIG_DIR` |
| Data | Generated files: `team_ratings.yaml`, `team_ratings_prior.yaml`, `player_prior.yaml`, `chip_plan.json`, `team_finances.json` | `FPL_CLI_DATA_DIR` |
| Cache | Disposable API response caches | `FPL_CLI_CACHE_DIR` |

**Ephemeral environments** (Claude Code on the web, CI, containers): the default config and data locations live inside the container and vanish with it. Point `FPL_CLI_CONFIG_DIR` and `FPL_CLI_DATA_DIR` at a persistent workspace directory so settings, credentials, generated reports, and generated data (team ratings, priors, chip plans, sell prices) survive between sessions. The cache is disposable by design and can stay container-local.

Three things to know when setting the overrides:

- **Every override must be an absolute path.** A relative value such as `./config` would be resolved against the current working directory, so the CLI would read a different directory depending on where you ran it from — and config would silently load from one directory only. Relative values are rejected with an error that names the variable and the absolute path it would have meant from where you stood.
- **`FPL_CLI_CONFIG_DIR` must come from the real environment**, not from `.env` — the config dir is where `.env` is found, so it has to be known first. `FPL_CLI_DATA_DIR` and `FPL_CLI_CACHE_DIR` can be set either way.
- **fpl-cli only sets `0700` permissions on a directory it creates itself.** Point an override at an existing directory (a shared workspace, a synced vault) and its mode is left as its owner set it.

A directory override that cannot be created is reported as an error naming the variable, rather than falling back silently. Set `FPL_CLI_CONFIG_DIR` at a directory with no `settings.yaml` and fpl-cli warns once on stderr and runs on shipped defaults — an empty platform default is the normal pre-`fpl init` state and stays quiet, but an override you set deliberately is almost certainly meant to point at your config.

### `settings.yaml` (user overrides)

```yaml
fpl:
  classic_entry_id: 1234567
  draft_league_id: 12345
  draft_entry_id: 123456
  classic_league_id: 654321   # Optional - enables standings, fines, league recaps

use_net_points: false          # Include transfer hits in GW points rankings (classic only)
custom_analysis: true          # Enable custom scoring algorithms (captain, targets, value scores, Bayesian FDR)

reports:
  output_dir: "./reports"

# Fines - opt-in, configured via `fpl init` or manually
fines:
  escalation_note: "Fines double each GW if not honoured"
  classic:
    - type: last-place
      penalty: "Pint on video"
    - type: red-card
  draft:
    - type: last-place
    - type: below-threshold
      threshold: 25
```

### `config/defaults.yaml` (committed)

Provides default LLM providers, thresholds, and data source settings. Override any value in `settings.yaml`:

```yaml
use_net_points: false          # Include transfer hits in GW points rankings (classic only)
custom_analysis: false         # Off by default; enable via fpl init or settings.yaml
rolling_window: 5              # Qualifying fixtures for rolling_pts_per_m (3-10)

data_sources:
  cache_ttl: 3600
  rate_limit: 30

thresholds:
  transfer_xg_threshold: 0.15
  price_alert_threshold: 1
  differential_threshold: 5.0
  semi_differential_threshold: 15.0
  captain_differential_threshold: 10.0

llm:
  research:
    provider: perplexity        # perplexity | anthropic | openai
    model: sonar-pro
    timeout: 120
    query_defaults:
      search_recency_filter: week
  synthesis:
    provider: anthropic
    model: claude-sonnet-5
    timeout: 60
    query_defaults:
      max_tokens: 4096
```

### LLM Providers

fpl-cli uses two LLM roles: **research** (web-grounded analysis) and **synthesis** (personal commentary). Configure via `fpl init` or env vars:

```bash
# Default: Perplexity for research, Anthropic for synthesis
export PERPLEXITY_API_KEY="your-key"
export ANTHROPIC_API_KEY="your-key"

# Swap to any OpenAI-compatible API
export FPL_SYNTHESIS_PROVIDER=openai
export FPL_SYNTHESIS_MODEL=gpt-4o
export OPENAI_API_KEY="your-key"

# Local model (Ollama, etc.)
export FPL_SYNTHESIS_PROVIDER=openai
export FPL_SYNTHESIS_MODEL=llama3
export FPL_SYNTHESIS_BASE_URL=http://localhost:11434/v1
```

### Fine Rule Types

`last-place`, `red-card`, `below-threshold`. The `use_net_points` setting controls whether transfer hits are included in GW points rankings across `league`, `review`, and fines (classic only).

### Other API Keys

- `FOOTBALL_DATA_API_KEY` - League table in `fpl review`, and Championship form for
  promoted teams in the pre-season prior (football-data.org). Without it, promoted teams
  share one undifferentiated bottom-of-table estimate.

## Known Limitations

- **Classic league scoring only.** No Head-to-Head or H2H knock-out league scoring. Both classic and draft formats are supported.
- **One entry per format.** Configure one classic team and one draft league.
- **League standings show top 50.** Covers most invitational leagues. Larger leagues see partial results.
- **Pending transfers not visible.** The FPL API only exposes picks for completed gameweeks.
- **Read-only.** The CLI authenticates with FPL only for price scraping (via Playwright). It will not set your lineup, make transfers, or submit waiver claims on your behalf.
