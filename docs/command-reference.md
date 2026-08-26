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
| `returnees --enrich` | Research (Perplexity) | Fresher return timing for flagged players FPL is silent or stale about, attached beside the FPL news rather than over it |

Everything else - captain picks, targets, differentials, waivers, FDR, team ratings, squad allocation, the returnee watchlist itself, all stats commands - is pure computation. No AI involved.

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

## JSON Output

`--format json` puts **both** envelopes on **stdout** and every human-readable line on
**stderr**, so one stream is the machine output and the other is the commentary. A
consumer reads stdout for the payload and checks the exit code to know which envelope
it got:

| | Success | Failure |
|---|---|---|
| stdout | `{command, metadata, data}` | `{command, error}` |
| stderr | warnings and progress, if any | warnings and progress, if any |
| exit code | 0 | 1 |

```bash
fpl captain --format json >out.json 2>err.txt || jq -r .error out.json
```

The failure message lives in the envelope, not on stderr. Under `--format json` stderr
carries only the prose the command had already written — a warning, or the reason it
gave up on a step — which may or may not name the cause the envelope names, and for many
commands is empty. Script against `error` and the exit code, never against stderr.

Warnings never change the exit code — a command that produced its payload exits 0 and
reports the problem in `metadata.warnings` (and on stderr), rather than failing. A few
commands deliberately soften the *table* path to exit 0 where the JSON path exits 1
(`league-recap` on an unresolvable gameweek prints the message and returns); the JSON
contract above is the one to script against.

The table applies to every way a command can end, not just the ones it was written for.
A command that cannot reach the FPL API, that needs an entry ID you have not configured,
or that has nothing cached to show reports it as an `error` envelope and exits 1 — where
several used to print the reason to stdout, or exit 0 with nothing on it at all.

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

### Injury Returnees

Track injured and suspended players due back soon — early enough to claim one in draft before rivals notice, and to plan around one in classic.

```bash
fpl returnees                     # Watchlist for the default window
fpl returnees --window 3          # Only returns expected within the next 3 gameweeks
fpl returnees --all               # Every flagged player, quality bar bypassed
fpl returnees --enrich            # Search the web for fresher return timing
fpl returnees --format json       # JSON envelope
```

**The window.** `--window N` (1-38) keeps a player whose expected return lands within the next N gameweeks, counting from the next gameweek inclusive. It defaults to `returnee_radar.window_gameweeks` in [`defaults.yaml`](#configdefaultsyaml-committed). A player whose return date is unknown is always inside the window: nobody knows when they are back, which is the reason to watch them rather than a reason to drop them.

**What FPL actually tells you.** Availability news comes in four shapes, and only two of them carry a date: `{reason} - Expected back {D} {Mmm}` and `Suspended until {D} {Mmm}`. The other two — `{reason} - {NN}% chance of playing` (the percentage is `chance_of_playing_next_round` restated) and `{reason} - Unknown return date` — state no timing at all. On a live snapshot of the player data taken in August 2026, 79 of 609 players were flagged and 10 of those 79 carried a parseable return date; among the long-term injuries the radar exists for (status `i`), 7 of 54 did. **Date-unknown is the normal output of this command, not an edge case**, and it is why `--enrich` exists.

**The quality bar is source-aware.** A returnee has no current-season form and no cumulative minutes to be judged on — structurally, not accidentally. `player_prior` only assigns `source: "history"` to a player with 450+ minutes in the previous season; everyone else falls to `source: "price"`, where `prior_strength` is capped at 0.5. Gating the whole list on one `prior_strength` threshold would therefore exclude exactly the players who missed most of last season through injury. So:

- **History-sourced** players are gated on `prior_strength` (`history_watchlist_strength`).
- **Price-sourced** players are scored through the same VALUE quality function the rest of the tool uses, over their most recent season carrying real minutes, and compared against `price_watchlist_percentile` as a fraction of the position ceiling.
- **Price percentile within position** is the last resort, for a player with no such season. Price tracks ownership churn and editorial pricing rather than output, so it is a floor, not a judgement.

Each entry reports which branch judged it (`quality.basis`: `prior`, `season-quality` or `price`). A second, higher bar (`history_stash_strength` / `price_stash_percentile`) marks the players worth holding a squad place for while they are still unfit; combined with the shorter `stash_window_gameweeks`, that is what a draft skill escalates from a watch into a stash claim.

**`--all`** lists every flagged player with the bar bypassed, and deliberately does not persist the snapshot — a filter-bypassed list as next week's baseline would make the following ordinary run report everyone it re-excluded as newly dropped.

**`--enrich`** is opt-in and bounded. It shortlists the entries FPL is silent or stale about (`enrich_stale_news_days`, capped at `enrich_max_players`), queries the research LLM provider for each, and shows what comes back **beside** the FPL news, never over it: where both state a date, both are carried. It needs a Perplexity API key (see [LLM Providers](#llm-providers)) and skips with a note on stderr when none is configured. Answers are cached per season and gameweek in the cache directory. Intel that came back without a source citation is marked as such and is not enough on its own to justify an irreversible move.

**Week-over-week changes.** Each run stores the watchlist it produced in `returnee_snapshot.json` in the data directory (see [Directories](#directories)) and the next run diffs against it, marking who is newly flagged, whose chance moved, whose return date was set, moved or missed, and who left the list. The snapshot is rewritten only when the gameweek changes, so a second run inside one gameweek still diffs against last week rather than against itself. A file from a previous season is discarded rather than read — player IDs are reshuffled at the season boundary. The first run has no history to compare against and says so.

JSON `metadata` carries `window`, `escalation_window`, `stash_upgrade_margin`, `transitions_available`, `quality_bar_available`, `quality_bar_applied`, and the `enrichment_*` fields (`requested`, `available`, `note`, `count`); `data` is `{entries, departures}`.

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

**Stale predictions:** `--blanks` mixes confirmed fixtures with predicted ones, and the
predictions carry a `last_updated` date. When it is old enough that the service distrusts
it, table mode prints a stderr notice and JSON mode adds a `fixture_predictions_stale`
entry to `metadata.warnings` — the `predicted_blanks` and `predicted_doubles` entries are
the affected ones. Confirmed blanks and doubles come from the fixture list and are
unaffected either way.

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

Until completed results can rate teams, ratings are estimated from the previous season (promoted teams from Championship form) and every fixture-difficulty view says so. That covers pre-season and the gap between GW1 kicking off and its first results landing. `fpl ratings update` falls back to the same estimate rather than reporting that there is nothing to calculate and leaving a stale file in place - including when the file on disk still rates last season's clubs, which is non-empty but leaves the promoted sides unrated.

A club that has played only one venue is rated on it. Once GW1 finishes, every club has exactly one result, so requiring both a home and an away record would drop the entire league and report ten finished fixtures as nothing to calculate from. The unplayed venue is estimated from the played one, rescaled by the gap between home and away scoring across the window, and the command names the clubs whose second venue is an estimate. The `Games` column shows what was actually played (`1H/0A`), and single-gameweek evidence still carries only 1/7 of the weight against the prior.

Early-season results are shrunk toward that previous-season prior, by the automatic refresh and by `fpl ratings update` alike: a one-gameweek sample carries 1/7 of the weight, six gameweeks half, and the prior drops out entirely once GW12 has completed. Shrinkage is gated on how far the season has run, so a narrow window late on is not blended -- `--since-gw 30` at GW34 saves recent form alone. Inside the early-season window the weight follows the size of that window rather than the gameweek number, so `--since-gw 8` at GW10 is weighted as three gameweeks of evidence. Blended files are stamped `calculated_blended` / `understat_xg_blended` / `auto_calculated_blended` so `fpl ratings` shows that shrinkage was applied. While the sample is shorter than six gameweeks the prior still carries most of the weight, and every fixture-difficulty view says so - `Ratings are mostly last season's prior - 1 gameweek of results carries 14% of the weight`. That replaces the pre-season estimate warning, which stops applying the moment a gameweek completes.

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

**Early-season notice:** before GW6 quality scores are dominated by tiny samples (form and ppg reflect only the opening gameweek(s)), so hot starters saturate the scale while elite players with a quiet start read low. Table mode prints a stderr notice; JSON mode adds an `early_season_small_sample` entry to `metadata.warnings` (alongside the existing `cross_position_ranking_not_meaningful` code when sorting `--value` without a position). `--sort ep_next` gives FPL's own prior-informed ranking in the meantime. See [the early-season caveat](custom-analysis.md#quality--value-scores).

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
| **ep_this** | FPL's predicted points for the current gameweek, **reliable only before the gameweek's first kickoff**. FPL rolls a player's `ep_this` forward to equal their `ep_next` once their match finishes, so once matches are under way the field holds a mix of current-GW and next-GW predictions depending on which teams have played — and sorting or comparing on it is not meaningful. JSON only - not shown in the panel. |

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

If instead it fails with `ERR_CONNECTION_RESET` or `ERR_TUNNEL_CONNECTION_FAILED` during the TLS handshake, the proxy is rejecting the browser's ClientHello itself (some legacy middleboxes RST any hello over 512 bytes or one carrying the Encrypted-ClientHello extension, which the newest bundled Chrome always sends). Point the scraper at a browser that emits a smaller, ECH-free hello:

- `FPL_BROWSER_EXECUTABLE` — absolute path to a browser binary (e.g. an older bundled Chromium such as `chromium-1194` under Playwright's browsers path). Mutually exclusive with `FPL_BROWSER_CHANNEL`.
- `FPL_BROWSER_CHANNEL` — a Playwright channel (`chrome`, `chromium`, `msedge`) instead of an explicit path. Mutually exclusive with `FPL_BROWSER_EXECUTABLE` — setting both raises an error rather than silently picking one.
- `FPL_BROWSER_ARGS` — extra launch flags, space-separated (e.g. `--disable-features=EncryptedClientHello`). Parsed with POSIX shell-quoting rules, even on Windows — escape backslashes in a path (`C:\\Users\\foo`) or use forward slashes.

```bash
export FPL_BROWSER_EXECUTABLE=/path/to/chromium-1194/chrome-linux/chrome
export FPL_BROWSER_ARGS="--disable-features=EncryptedClientHello"
fpl squad sell-prices --refresh
```

Disabling ECH may additionally require a browser-level managed policy (`EncryptedClientHelloEnabled: false`) since the flag alone does not always strip it. All four env vars are opt-in — leave them unset on trusted networks.

Output: free transfers, bank balance, squad sell prices, total team value. Data cached to `team_finances.json` in the data directory (see [Directories](#directories)) for 12 hours.

**Suspect scrapes:** a scrape that returns far fewer players than a squad holds is
reported rather than trusted — the page usually rendered late or the login silently
failed. Table mode labels the panel `Squad Budget (Suspect)` and keeps any valid cache;
JSON mode still emits the data, with a `scrape_suspect` entry in `metadata.warnings`, so
a consumer can tell the difference a table reader can see. Re-run with `--refresh`, or
`--visible` to watch the browser. With nothing cached and no `--refresh`, the command
exits 1 with an `error` envelope rather than exiting 0 having shown nothing.

**Wildcard / Free Hit workflow:** Use `--format json` to export sell prices, then pass to `fpl allocate --sell-prices` for accurate budgeting:
```bash
fpl squad sell-prices --format json > /tmp/sell-prices.json
fpl allocate --sell-prices /tmp/sell-prices.json  # Budget auto-computed from sell values + bank
```

## Reports

### Report layout

Every generated report is written to a directory named for the season it covers:

```
<output_dir>/2026-27/gw21-review.md
<output_dir>/2026-27/gw21-league-recap.md          # -league-recap-draft.md for draft
<output_dir>/2026-27/gw21-preview.md
<research_dir>/ai-scout-reports/2026-27/gw22-scout-preview.md
```

The season segment is the hyphenated label (`2026-27`), derived from the date using the same July cutover as the rest of the tool, and appended automatically — you do not put it in `reports.output_dir` yourself. It is what keeps the reports apart: the filenames carry a gameweek but no season, so in a flat directory the 2026-27 GW21 report would overwrite the 2025-26 one, silently and unrecoverably.

Two details worth knowing:

- **`--output` is partitioned too.** `fpl review --save --output ~/somewhere` writes to `~/somewhere/2026-27/`, not `~/somewhere/`. A scripted destination gets the same protection as a configured one.
- **Pointing a directory at the current season is harmless.** If `reports.output_dir` already ends in the current season label, it is used as-is rather than nested a second time.
- **A directory left pointing at a *past* season warns.** `output_dir: ~/fpl-reports/2025-26` in 2026-27 writes to `~/fpl-reports/2025-26/2026-27/` and says so on stderr. The nesting is deliberate: reusing the stale directory would file this season's reports under last season's name, which is the mislabelling the layout exists to prevent. Drop the season from the setting and it is appended correctly.

**Known limitation.** The season label comes from the date, on the same July cutover as the rest of the tool, not from the gameweek being written. A season that overruns that cutover -- as 2019-20 did, delayed into July 2020 -- is stamped with the following season's label, and its late gameweeks collide with that season's own. See [#91](https://github.com/rossgroomio/fpl-cli/issues/91).

Reports written before this layout existed are left where they are. Nothing moves them, and nothing overwrites them — new reports simply land in the season subdirectory alongside. Move them into a matching season directory if you want them tidied.

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
fpl league-recap                  # Recap last completed gameweek
fpl league-recap --save           # Save report
fpl league-recap --summarise      # Add LLM editorial narrative
fpl league-recap --draft          # Use draft league
fpl league-recap --backfill-detail  # Rebuild earlier gameweeks in full detail
fpl league-recap --format json     # JSON envelope for scripting/agents
```

**Awards:** GW winner/loser, biggest bench haul, best/worst captain, transfer/waiver genius and disaster.

**Standings movement:** position changes derived from point differentials, per-manager highlights.

**Fines:** evaluates fines for every manager (not just you) when configured.

**LLM editorial** (`--summarise`): Newsletter-style narrative via synthesis provider. Names names, calls out decisions. The editorial is an add-on: if the synthesis provider has no usable API key the recap still renders, still saves its report and still captures the ledger, with the reason on stderr and a `synthesis_provider_unavailable` warning in JSON. `synthesis_summary` is `null` on such a run — the warning is what distinguishes it from a run that never asked for an editorial.

**Streaks:** notable open streaks (weeks on top, win/loss runs, captain blanks, green-arrow droughts, waiver activity, and more) print under `Streaks:` on console — leaders only, so console stays a highlights view — and in full as a `# League History` section in the saved report. Each is reported as an observed count over its true span (e.g. "3 in the last 11, with 8 not recorded") rather than a bare "in a row" once any gameweek went uncaptured.

**Unavailable:** a manager whose position or points total can't be derived this run (e.g. a replayed draft gameweek with no earlier rows) is named under `Unavailable:` on console and in the report rather than silently dropped from Standings Movement.

**JSON:** `--format json` emits one row per manager — the same shape written to the
ledger, built from the rows this run assembled, so manager data is present even when the
store could not be written. `metadata` carries `coverage` (per gameweek: fidelity-tier
counts, unknown managers, whether the file was readable), `season_phase`, `notes_pack`
(every entry, including those below their reporting minimum), `synthesis_summary` (with
`--summarise`), `warnings`, and `first_capture_store_path` — always present, carrying the
partition directory on its first capture and `null` on every run after that. Warning
codes are listed under [Capture warnings](#capture-warnings).

#### League history

Every run records what it computed, one row per manager per gameweek, under
`<data dir>/league_history/<season>/<format>-<league id>/gwNN.ndjson`. There is no flag
to switch this on and no separate command: the recap already fetched everything a row
needs. The store exists because the FPL API keeps per-gameweek detail only for the
current season — at the July rollover everything collapses to one aggregate row per
season, and for draft the per-gameweek numbers are then gone for good.

A new season adds a partition rather than replacing the last one, so the store grows for
as long as you keep running recaps and nothing prunes it — a few megabytes per league per
season. Worth knowing if `FPL_CLI_DATA_DIR` points into a synced folder. Streak counters
are cached separately, under `<data dir>/league_history_counters/`, and rebuilt from the
ledger whenever that cache is missing, stale, or unreadable — it is safe to delete, and
is never read as a source of truth.

Rows are append-only. Re-running a gameweek that has not changed writes nothing; a
re-run whose numbers differ (bonus points settled, a failed fetch repaired, a coarse
gameweek filled in) appends a superseding row and leaves the old one in place. A file
that cannot be parsed is never reset or overwritten: the run says which file and what to
do about it, still prints the recap from live data, and exits 0.

Two fidelity tiers, both recorded on the row:

| Tier | Source | Carries |
|---|---|---|
| Coarse | Classic manager-history endpoint, one request per manager for the whole season | Points, cumulative total, transfer count and cost, bench points, squad value, bank |
| Detailed | A live recap run, or `--backfill-detail` replaying a past gameweek | Everything above plus captain, vice, full squad, and transfer or waiver detail |

Classic gaps fill at the coarse tier automatically. `--backfill-detail` upgrades them,
at the cost of one request per manager per gameweek, which is why it is opt-in. Draft
has no manager-history endpoint at all, so a draft gameweek that was never captured can
only be rebuilt with `--backfill-detail`, and only while the season is live.

When a gameweek is missing, coarse, unreadable, or holds a manager whose data could not
be fetched, the run says so on stderr and names the remedy. A fully captured season
stays quiet.

Ephemeral environments (Claude Code on the web, CI, containers) must point
`FPL_CLI_DATA_DIR` at a persistent workspace or the ledger dies with the container. The
first time a season's partition is created, the run prints where it went.

#### Season phase

Every recap is stamped with where its gameweek sits in the season's arc. The phase sets
the framing line in the report and the editorial's tone, and the finale is the one phase
whose notes pack rescans every captured gameweek instead of a trailing six-gameweek
window.

| Phase | Gameweeks | Framing it states |
|---|---|---|
| `opener` | The first gameweek (GW1) | "the season opener" |
| `pre_chip_boundary` | Up to the gameweek before the chip split (GW2-18) | "before the GW19 chip-availability boundary" |
| `midpoint` | Chip split to the start of the run-in (GW19-31) | "the season midpoint, past the GW19 chip boundary and before the run-in" |
| `run_in` | The last six gameweeks before the final one (GW32-37) | "in the run-in to the season finale (GW38)" |
| `finale` | The final gameweek and anything past it (GW38+) | "the season finale" |

Boundaries derive from the season-length and chip-split constants rather than fixed
dates, so a season of a different length moves them and the stated gameweek numbers move
with it. `metadata.season_phase` carries the live value, and is `null` on any run that built no
notes pack — an unreadable store, or no league id configured for the format.

#### Capture warnings

Every capture problem is reported on stderr as prose and, under `--format json`, in
`metadata.warnings` as a `{"code", "message"}` pair. The prose is rewritable; the codes
are stable, so scripts should key on those. One non-capture code shares the list, since
it shares the channel: `synthesis_provider_unavailable`.

| Code | Raised when |
|---|---|
| `league_history_league_id_missing` | No league id is configured for this format, so the gameweek was not recorded at all |
| `league_history_store_unreadable` | The gameweek's file could not be read or written; it is left untouched and the recap still renders from live data |
| `league_history_coverage` | One line per coverage gap: gameweeks missing, held at the coarse tier, holding unknown managers, or unreadable |
| `league_history_unmatched_players` | A draft squad player could not be matched to a main-game player, so their recorded points are zero rather than a real score |
| `league_history_transfer_detail_short` | Fewer transfers were captured than the manager's recorded count, so the stored list is incomplete rather than empty |
| `league_history_standings_truncated` | The standings response covered only part of the league, so the gameweek is recorded for that subset only |
| `league_history_backfill_manager_unreachable` | One manager's history could not be fetched; their gameweeks stay unknown and are re-attempted next run |
| `league_history_backfill_replay_failed` | One gameweek could not be replayed in detail; the others are unaffected |
| `league_history_backfill_write_failed` | A backfilled gameweek could not be written; the rest of the backfill continues |
| `synthesis_provider_unavailable` | `--summarise` was asked for but the synthesis provider had no usable key; everything else in the recap, the capture included, ran normally |

None of these change the exit code — `league-recap` exits 0 whenever the recap itself
rendered. The single exit-1 case is a gameweek that could not be resolved at all under
`--format json`, which emits the shared `{"command", "error"}` envelope on stdout (see
[JSON Output](#json-output)); the table path prints the same message and exits 0.

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
fpl intel resolve ARS --all --write  # ...and save corrections over existing codes
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

This table is the one prose copy of the schedule; `tests/test_doc_consistency.py` checks
it against `SECTION_DECAY` in code, and `fpl intel --show-decay` prints the live version.
Everything else (the agent skills included) reads the schedule from the JSON payload
(`metadata.decay_schedule`, `metadata.sections_live`) rather than restating the numbers.

Between full confidence and expiry the value tapers linearly and is reported per section
as `section_confidence`. A categorical field such as `status: starter` cannot be scaled
numerically, so the confidence is emitted alongside it for the consumer to weigh. Each
emitted preview also carries `sections_present` — the unexpired sections that file
actually holds data for, so a consumer need not re-derive that from the payload keys.

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
season. The season check reads the explicit `season` label when present (any
start/end-year spelling — `2026-27`, `2026/2027`, `26-27` — is accepted) and falls back
to the `published` date, with May and June of the season's start year counting as
current since that is when season previews are written. This is the guard against
building a squad on last August's opinions. A file whose `team` is still the `EXAMPLE`
template sentinel is skipped with a warning, and a preview set that has drifted across
promotion and relegation is reported too; clubs not in the current league never count
toward coverage. Two teams sharing a `predicted_finish` draws a warning (not a skip):
finishes extracted from a single source's predicted table should form a permutation, so
a duplicate usually means a row was misread at ingest.

### Name resolution

Preview prose names players the way a reader would — "Bruno Guimaraes" where the game
shows "Bruno G." — so `fpl intel resolve` matches names against the team's squad and
writes `element_code` (stable across seasons) back into the file. Accents and
punctuation are folded, so `Ødegaard` and `Odegaard` resolve alike (including
non-decomposable letters: `Łukasz` matches `Lukasz`). An exact match on a
display or full name wins outright; otherwise every query token must appear in the
player's combined names. **Ambiguity is reported, never guessed** — a silently wrong
code attaches intel to the wrong player. Writes are round-trip YAML, so hand-written
comments and formatting survive. A plain `--write` never touches an existing code — a
hand-corrected code survives a re-run — while `--all --write` saves a re-resolved code
over a differing existing one.

## Configuration Reference

Configuration uses two layers, deep-merged at runtime:

1. **`config/defaults.yaml`** (committed) - project defaults, no personal data
2. **`settings.yaml`** (user overrides) - in your platform config directory (`~/Library/Application Support/fpl-cli/` on macOS, `~/.config/fpl-cli/` on Linux, override with `FPL_CLI_CONFIG_DIR`)

Run `fpl init` to configure interactively. Only set values in `settings.yaml` that differ from defaults.

### Setup Health Check

```bash
fpl doctor                      # Check IDs, data files, and directories
fpl doctor --providers          # Probe the external data sources instead
fpl doctor --format json        # Machine-readable report (for agents/scripts)
```

Rolling a setup into a new season silently invalidates IDs and per-team files: a dead draft league returns nothing, entry and league IDs reissued over the summer resolve to a stranger's team or league, and a per-team file rebuilt in August can still describe last season's twenty clubs. None of these error — they produce plausible output. `fpl doctor` checks all of it in one pass:

**IDs in `settings.yaml`** — each configured ID is resolved against the live API and the team/league name reported back, so a wrong-but-valid ID is visible:

- `classic_entry_id` resolves, reporting the team and manager name, **and belongs to `classic_league_id`** (via the entry's own `leagues.classic`) — catching an ID reissued over the summer that now points at someone else's team. The reissued-ID verdict only fires when the league itself checked out, so a stale league ID cannot condemn a correct entry
- `classic_league_id` resolves, reporting the league name back. No season assertion: classic league IDs come from a sequence that restarts each July, so `created` always lands in the current season and last season's ID resolves to a *different* league rather than going dead. The stamp proves nothing here — the entry's membership check above is what proves the pairing is still yours
- `draft_league_id` resolves; flagged when its draft was held in a previous season (draft leagues are recreated each season)
- `draft_entry_id` resolves **and belongs to `draft_league_id`** (via the entry's `league_set`), catching a recycled ID that points at someone else's team — the recycled-ID verdict only fires when the league itself checked out, so a stale league ID cannot condemn a correct entry

**Data files:**

- `team_ratings.yaml` — season stamp and team set vs the live league (problems here are stale, not broken: the ratings service already ignores or rebuilds bad files)
- `team_managers.yaml` — merged shipped + user copy covers exactly the current twenty clubs
- `previews/` — optional season preview intel: a file for a club not in the current league is flagged (it loads and inflates the coverage gate), and files the loader skipped (previous season, malformed) are surfaced with `fpl intel` as the follow-up
- `team_finances.json` — `scraped_at` falls within the current season
- `player_prior.yaml` — season label matches (auto-invalidated otherwise)
- `returnee_snapshot.json` — the returnee radar's week-over-week snapshot: season label matches (a previous season's is discarded and rebuilt on the next `fpl returnees` run)

**Environment** — which directory each of config/data/cache resolved to and whether an `FPL_CLI_*` override is in effect, plus whether `settings.yaml` exists.

Each finding is classified as **broken** (wrong answers now — fix it today), **stale** (self-corrects or needs one routine refresh), **skipped** (not configured/present), or **unchecked** (API unreachable). Exits non-zero when anything is broken, so it can gate scripts.

**`--providers`** checks the external data sources instead of the local setup. None of them version anything, and the tool degrades gracefully everywhere, so upstream drift otherwise surfaces as plausible but wrong output — a renamed stat field zeroes every player's xG without an error. Each probe asserts shape and volume, not just reachability, and where a column check is not the contract it runs the parser itself:

- **FPL API** — bootstrap has 20 teams / a sane player count / 38 gameweeks, and every stat field the tool reads is present in the raw data (a missing one would silently read as 0)
- **Draft API** — bootstrap resolves with 20 teams and a sane player count
- **vaastav dataset** — each historical season's `players_raw.csv` exists upstream, covers the columns the parser reads, and clears a row-count floor
- **Core-Insights dataset** — same for `players.csv` and `playerstats.csv` (the sole current-season source), plus the per-gameweek files at the latest finished gameweek. `players.csv` must also parse into the player lookup every other file joins on, and the per-gameweek files are run through the parsers the scoring commands use rather than a header check: the columns can all be present over values nothing survives (Elo published blank at the start of a season empties the match join), so the probe asserts the parse yields records and names the signals a zero costs. A file missing or parsing to nothing only for the newest gameweek is a publishing lag or a backfill in progress (stale); the same problem two gameweeks running is a layout or format change (broken)
- **Understat** — league data is non-empty, and every current club's name resolves to a team in Understat's own data (an unresolved club silently loses xG enrichment); early in the season an unresolved name is reported as stale, since Understat only lists a club once it has ingested a match for it
- **football-data.org** — configured, standings has 20 rows, and every served TLA maps onto a live FPL short name (a failed join here doesn't produce a missing rating — it silently re-rates the club as promoted)

The same classification applies, and transient unreachability is always **unchecked**, never broken. A scheduled CI job runs `fpl doctor --providers --format json` weekly and fails only on broken, so provider drift with no commit behind it still surfaces within days.

### Directories

fpl-cli writes to three directories, each resolved via `platformdirs` and overridable with an env var:

| Directory | Contents | Override |
|-----------|----------|----------|
| Config | `settings.yaml`, `.env` (credentials and API keys), `team_ratings_overrides.yaml`, optional `team_managers.yaml` (layered over the shipped copy per club) and `fixture_predictions.yaml` (replaces the shipped copy), the optional `previews/` directory of [season preview intel](#season-preview-intel), plus the `output/` and `research/` report directories (each partitioned by season -- see [Report layout](#report-layout)) | `FPL_CLI_CONFIG_DIR` |
| Data | Generated files: `team_ratings.yaml`, `team_ratings_prior.yaml`, `player_prior.yaml`, `chip_plan.json`, `team_finances.json`, `returnee_snapshot.json` (the [returnee radar's](#injury-returnees) week-over-week snapshot), the [`league_history/`](#league-history) ledger and its rebuildable `league_history_counters/` cache | `FPL_CLI_DATA_DIR` |
| Cache | Disposable API response caches | `FPL_CLI_CACHE_DIR` |

**Ephemeral environments** (Claude Code on the web, CI, containers): the default config and data locations live inside the container and vanish with it. Point `FPL_CLI_CONFIG_DIR` and `FPL_CLI_DATA_DIR` at a persistent workspace directory so settings, credentials, generated reports, and generated data (team ratings, priors, chip plans, sell prices, league history) survive between sessions. The [league history ledger](#league-history) is the one thing there that a later run cannot rebuild: the API keeps per-gameweek detail only for the current season, so a container that vanishes mid-season takes those gameweeks with it. The cache is disposable by design and can stay container-local.

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
  output_dir: "./reports"      # Reports are written to <output_dir>/<season>/ - see "Report layout"

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

returnee_radar:                  # `fpl returnees` - see Injury Returnees above
  window_gameweeks: 6            # Watchlist window, overridden per run by --window
  stash_window_gameweeks: 2      # Shorter window that escalates a watch into a stash
  history_watchlist_strength: 0.75   # prior_strength bar, history-sourced player (0-1)
  history_stash_strength: 0.85       # prior_strength bar to escalate one
  price_watchlist_percentile: 0.80   # Quality/price bar, price-sourced player (0-1)
  price_stash_percentile: 0.90       # Same measure, escalation bar
  stash_upgrade_margin: 5.0      # Quality points a returnee must beat the incumbent by
                                 # (published as metadata.stash_upgrade_margin)
  enrich_stale_news_days: 7      # --enrich re-checks a dated player whose news is older
  enrich_max_players: 8          # Most players one --enrich run will search for

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
- **League standings tables show top 50.** Covers most invitational leagues. Larger leagues see partial
  results, and `fpl status` omits the this-week league position for them (it cannot be ranked from one page).
  Your true rank and the exact league size are still reported, since both come from your entry rather than
  the standings page. The [league history ledger](#league-history) inherits the limit: a gameweek captured
  from a truncated page is recorded for that subset only, and says so with the
  `league_history_standings_truncated` warning.
- **Pending transfers not visible.** The FPL API only exposes picks for completed gameweeks.
- **Read-only.** The CLI authenticates with FPL only for price scraping (via Playwright). It will not set your lineup, make transfers, or submit waiver claims on your behalf.
