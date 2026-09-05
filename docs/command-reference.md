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
| **Mixed** | `stats`, `xg`, `fdr`, `fixtures`, `preview` | Experimental columns/sections stripped |
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

**Table mode splits the same way.** Without `--format json` the output you asked for goes
to stdout and everything else goes to stderr — warnings, progress notices, and the reason
a command exited nonzero. `fpl squad grid 2>/dev/null` prints a grid or prints nothing;
it never prints half an explanation. That holds for every command, so redirecting either
stream means the same thing whichever one you run:

```bash
fpl stats >players.txt          # players in the file, any complaint on the terminal
fpl stats 2>/dev/null           # quiet, and empty if it failed
```

## Player Analysis

### Captain Picks

Rank captain options by combining matchup score, recent form, xGI, home advantage, and penalty taker status. Scores normalised to 0-100.

```bash
fpl captain            # Your squad
fpl captain --global   # All players (top 30 by form/xG)
fpl captain --format json
```

Your squad is the default, so `classic_entry_id` is required without
`--global`: an absent one is an error naming the flag, not a global list
returned as though it were your options. `metadata.my_squad_mode` says which
list a JSON payload holds. Before the first deadline there is no squad to
rank, so the global list is all there is and
`metadata.warnings` carries `captain_global_fallback` to say so (stderr in
table mode), and `captain_no_next_gameweek` where there is no next gameweek to
captain for at all — a finished season, or one whose next gameweek is not
published yet. An entry ID that does not resolve is a failure, not a fallback,
and is reported in the same words `fpl squad` uses for it.

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

Target score combines xG metrics, form, PPG, 3-GW matchup quality, and consistency (CV-xGI percentile bonus, phased in GW6-10), attenuated by position multiplier (GK 0.7, DEF 0.85), and normalised to 0-100. Before GW10 the quality half of that carries the [early-season prior blend](custom-analysis.md#early-season-confidence-gw1-10) — last season's pedigree weighted by confidence, so a quiet-starting elite is not ranked below a one-game wonder — while the matchup and consistency terms stay pure observation. The command says so while the blend is live: JSON `metadata.warnings` carries `early_season_prior_informed` (or `early_season_small_sample` before GW6, when last season's history could not be loaded and the ranking is pure observation), and table mode prints the same notice to stderr. See [Target Score](custom-analysis.md#target-score) for the full formula.

### Transfer Evaluation

Compare an OUT player against IN candidates on two scoring horizons.

```bash
fpl transfer-eval --out Palmer --in "Salah,Mbeumo,Diaz"
fpl transfer-eval --out "Henderson (CRY)" --in "Sels,Petrovic"   # shared surname
fpl transfer-eval --out Palmer --in Salah --format json
```

| Flag | Default | Description |
|------|---------|-------------|
| `--out` | *(required)* | Player to transfer out (name, player ID, or `Name (TEAM)`) |
| `--in` | *(required)* | Comma-separated IN candidates (name, player ID, or `Name (TEAM)`) |
| `--format` | `table` | `table` or `json` |

A name two players answer to exactly — Dean and Jordan Henderson are both
`Henderson` — is an error naming both, not a silent pick of whichever the API
lists first. Add the club (`Henderson (CRY)`) or use the player ID to choose.

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

Outlook uses the [ownership scoring family](custom-analysis.md#ownership-scoring) and This GW the [single-GW scoring](custom-analysis.md#single-gw-scoring) family. Before GW10 Outlook and Quality both carry the [early-season prior blend](custom-analysis.md#early-season-confidence-gw1-10) — Outlook against the target family's anchor, matching `fpl targets`, and Quality against the value family's, matching `fpl player` and `fpl stats --value` — while This GW keeps position-mean shrinkage. Before GW10 JSON `metadata.warnings` carries the same early-season notice as every other prior-blended surface (`early_season_prior_informed`, or `early_season_small_sample` when last season's history could not be loaded), and table mode prints it to stderr. It names both blended columns — `quality_score` and `target_score` — and only `target_score` when no Understat match produced a quality score; `lineup_score` is deliberately absent, being the one column here that still shrinks.

**Draft note:** Outlook rankings may differ from `fpl waivers` output due to different weighting emphasis - target score uses more xG, less form than waiver score.

### Differentials

Find low-ownership players with high potential.

```bash
fpl differentials            # <5% owned, 60+ minutes played
fpl differentials -t 3       # <3% owned (ultra-differentials)
fpl differentials -m 200     # Require 200+ minutes played
fpl differentials --format json  # JSON envelope (metadata: {gameweek})
```

Differential score combines xG metrics, form, ownership bonus, 3-GW matchup quality, and consistency (inverted CV-xGI bonus - volatile players score higher, phased in GW6-10), attenuated by position multiplier (GK 0.7, DEF 0.85), and normalised to 0-100. Before GW10 the quality half of that carries the [early-season prior blend](custom-analysis.md#early-season-confidence-gw1-10); the ownership, matchup and consistency bonuses stay pure observation. The command carries the same early-season notice as `fpl targets` (`metadata.warnings` in JSON, stderr in table mode), naming `differential_score`. See [Differential Score](custom-analysis.md#differential-score) for the full formula.

### Waiver Recommendations

Analyse your draft squad and suggest free-agent pickups.

```bash
fpl waivers
fpl waivers --format json
```

Identifies squad weaknesses by position, ranks available free agents by waiver score, suggests who to drop for each pickup. This covers the waiver wire (unclaimed players) only - trade recommendations between managers are not in scope.

Waiver score combines xGI, form, PPG, 3-GW matchup quality, and consistency (CV-xGI percentile bonus, phased in GW6-10), attenuated by position multiplier (GK 0.7, DEF 0.85), and normalised to 0-100. Keepers swap the xGI term for saves/90, defensive quality and clean-sheet rate, read from the main-game player each draft element is joined to. Uses a stricter minutes factor than target/differential because draft waivers are a season commitment. Before GW10 the quality half of the score carries the [early-season prior blend](custom-analysis.md#early-season-confidence-gw1-10); the matchup, position-need, team-stacking and consistency terms stay pure observation. The command carries the same early-season notice as `fpl targets` (`metadata.warnings` in JSON, stderr in table mode), naming `waiver_score`. On this path the prior can go missing two ways — last season's history could not be loaded, or the draft element found no main-game counterpart to join to — and either reads as `early_season_small_sample`. See [Waiver Score](custom-analysis.md#waiver-score) for the full formula.

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
- **Defenders and goalkeepers** need four signals the position ceilings budget for that a past season does not obviously carry: defensive contribution per 90, and the keeper block of saves per 90, clean sheets per 90 and xGC per 90. Core-Insights publishes all four on the season row, so a season inside its two-season window is scored against the real ceiling. For an older season nothing recorded them — defensive contribution is a 2025-26 scoring introduction — and reading the gap as zero would be a verdict on the player rather than on the data, so the ceiling shrinks instead to the headroom the row can reach and the score means "how good was this season, on the signals we have". Without those four terms both positions were scored against ceilings they could not reach: over the real 2025-26 season the best defender in the league read 77 and the best keeper 45, so neither position could ever clear the bar however good their last healthy season.
- **Price percentile within position** is the last resort, for a player with no such season. Price tracks ownership churn and editorial pricing rather than output, so it is a floor, not a judgement.

Each entry reports which branch judged it (`quality.basis`: `prior`, `season-quality` or `price`). A second, higher bar (`history_stash_strength` / `price_stash_percentile`) marks the players worth holding a squad place for while they are still unfit; combined with the shorter `stash_window_gameweeks`, that is what a draft skill escalates from a watch into a stash claim.

**`--all`** lists every flagged player with the bar bypassed, and deliberately does not persist the snapshot — a filter-bypassed list as next week's baseline would make the following ordinary run report everyone it re-excluded as newly dropped.

**`--enrich`** is opt-in and bounded. It shortlists the entries FPL is silent or stale about (`enrich_stale_news_days`, capped at `enrich_max_players`), queries the research LLM provider for each, and shows what comes back **beside** the FPL news, never over it: where both state a date, both are carried. It needs a Perplexity API key (see [LLM Providers](#llm-providers)) and skips with a note on stderr when none is configured. Answers are cached per season and gameweek in the cache directory. Intel that came back without a source citation is marked as such and is not enough on its own to justify an irreversible move.

**Pacing and rate limits.** The shortlist is paced rather than burst: at most `enrich_concurrency` searches are in flight at once and two searches start no closer together than `enrich_query_spacing_seconds`, because a provider quota counts starts per minute, not searches in flight. A search the provider rate-limits (HTTP 429) is retried with backoff inside the provider itself, honouring `Retry-After` (see [LLM Providers](#llm-providers)); whatever is still refused once the whole shortlist has settled is tried once more after a pause of at least 15 seconds, or the provider's own `Retry-After` up to a minute. A player still unanswered after that is reported as rate-limited rather than as having no answer — `metadata.enrichment_rate_limited` is true and the stderr note says so — and is not cached, so re-running `--enrich` a minute later queries only the gaps. Both pacing knobs are worth lowering on a lower provider tier.

**Week-over-week changes.** Each run stores the watchlist it produced in `returnee_snapshot.json` in the data directory (see [Directories](#directories)) and diffs against the last watchlist stored in an *earlier* gameweek, marking who is newly flagged, whose chance moved, whose return date was set, moved or missed, and who left the list. The file keeps two states for that: the baseline every run in this gameweek compares against, and this gameweek's own state, which becomes the baseline once the gameweek rolls over. So every run in a gameweek reports the same changes — the second run of the week, including the one `--enrich` makes, sees what the first one saw rather than diffing against what it just stored. `metadata.transitions_baseline_gameweek` names the gameweek being compared against. A file from a previous season is discarded rather than read — player IDs are reshuffled at the season boundary. With nothing stored from an earlier gameweek there is nothing to compare against, and the run says so instead of reporting no changes.

JSON `metadata` carries `window`, `escalation_window`, `stash_upgrade_margin`, `transitions_available`, `transitions_baseline_gameweek`, `quality_bar_available`, `quality_bar_applied`, and the `enrichment_*` fields (`requested`, `available`, `note`, `count`, `rate_limited`); `data` is `{entries, departures}`.

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

**JSON output fields (horizon >= 2):** `id`, `web_name`, `team`, `position`, `price`, `quality_score` (0-100), `raw_quality` (float), `role` (starter/bench), `captain_gws`. Metadata includes `formation`, `budget_used`, `budget_remaining`, `captain_schedule`, `solver_status`, and `warnings` — the early-season quality notice before GW10 (`early_season_prior_informed`, or `early_season_small_sample` when last season's history could not be loaded and the solver ranked on pure observation), empty otherwise and at horizon 1; table mode prints it to stderr.

**JSON output fields (horizon 1):** Same as above but the score field is `single_gw_score` (0-100) instead of `quality_score`.

**Score field semantics:** At `--horizon >= 2` the `quality_score` field is normalised against a position-specific VALUE-family ceiling, matching `fpl player` / `fpl stats --value` / `fpl transfer-eval` for cross-command consistency — elite GKs, DEFs, MIDs and FWDs each land in comparable 0-100 bands *within their own position*. At `--horizon 1` the field is named `single_gw_score` because it comes from a different scoring family (`GW_SELECTION_WEIGHTS` + fixture-matchup term) and is normalised against a single cross-position `STARTING_XI_CEILING`; DEFs and GKs display noticeably lower than MIDs/FWDs for the same real-world quality. The different name signals that the two fields are not comparable. Use `raw_quality` for a position-agnostic ranking at either horizon. Before GW10 both fields carry the value family's [prior blend](custom-analysis.md#early-season-confidence-gw1-10) at `--horizon >= 2` — the solver ranks on the blended score, in place of the position-mean shrinkage the single-GW family applies — while `--horizon 1` stays a pure single-gameweek projection.

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

- `all` (default): FDR plus ATK and DEF columns. FDR is the mean of ATK and DEF in the selected mode, so the three columns read on one model and the "Teams with Easiest Fixture Runs" ranking agrees with them.
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

### Gameweek Fixtures

The fixture list for one gameweek with an FDR beside each side.

```bash
fpl fixtures                         # Next gameweek
fpl fixtures -g 32                   # A specific gameweek
fpl fixtures -m opponent             # Opponent-rating-only mode
fpl fixtures --format json           # JSON envelope (metadata: {gameweek, fdr_mode, warnings})
```

The FDR is the same general figure `fpl fdr` and `fpl preview` show — the mean of the
fixture's ATK and DEF [positional FDRs](custom-analysis.md#position-specific-fdr), scored
at the venue and in the selected [mode](#fdr-modes--m), on the 1-7 scale. It read the
opponent's venue-blind average rating until #202, so the same match carried two different
numbers under the same header depending on which command printed it.

A fixture involving a club the ratings do not cover scores the neutral 4.0, not the FPL
API's `home_difficulty` — that would sit on a 1-5 scale inside a 1-7 column. When the
ratings cannot support difficulty at all (missing, last season's, or flat), table mode
prints the stderr notice `fpl fdr` prints and JSON mode adds a `team_ratings_unusable`
entry to `metadata.warnings`, so a table of flat 4.0s is not read as analysis.

**Without custom analysis:** the raw FPL API difficulty (1-5), styled and labelled as in
`fpl fdr`. `-m` has nothing to apply to on that scale, so passing it explicitly prints a
stderr note rather than changing the table. `--format json` names the scale either way:
`metadata.fdr_scale` is `team_ratings_1_7` or `fpl_api_1_5`, alongside `custom_analysis`
and an `fdr_mode` that is `null` when it does not apply. The command was ungated until
#202, which meant the default configuration showed a 1-7 ratings FDR here and a 1-5 API
one in the preview's fixtures table — the same two-numbers-one-match split, one scale
further apart.

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
| TC | Best DGW candidate's double averages FDR <= 3.0 | Strong |
| TC | Best DGW candidate's double averages FDR <= 4.0 | Possible |

The TC figure is the mean general FDR of the candidate's own fixtures in that gameweek - the same venue-aware ATK/DEF mean `fpl fdr` and `fpl preview` show - so the candidate with the easiest double wins. The thresholds are more lenient than the `fpl fdr` colouring (2.5/3.0) because a double doubles the upside.

Two candidates are skipped rather than graded on a figure that doesn't mean what it says. A club with no fixture in the gameweek has nothing to average - a predicted double can sit beyond the six-gameweek FDR window. And a club the ratings file doesn't know scores the neutral 4.0 on every fixture, which would clear the "possible" threshold on a placeholder; that is the season-rollover case where a ratings file passes its date checks while knowing nothing about the promoted clubs. If no candidate is scoreable there is no TC signal.

A predicted double is only scoreable on the leg the fixtures API already lists, so its figure covers one match rather than the pair. The detail says so (`Salah (FDR 2.0, 1 of 2 scheduled)`), and `--format json` carries the count as `fixtures_scored`.

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

**Early-season notice:** before GW10 `quality_score` is a prior-informed estimate — the observed score blended with last season's pts/90 pedigree (price for players without PL history), weighted by how far the season has run and the player's track record, so a quiet-starting elite keeps most of their standing and one good game does not saturate the scale. Table mode prints a stderr notice quoting the observation's weight band for that gameweek (25-50% going into GW2); JSON mode adds an `early_season_prior_informed` entry to `metadata.warnings` (alongside the existing `cross_position_ranking_not_meaningful` code when sorting `--value` without a position, and `understat_team_unmatched` for any club Understat carried no players for). When last season's history cannot be loaded the scores are pure observation and the notice says so instead (`early_season_small_sample`, before GW6 only): hot starters saturate the scale while elite players with a quiet start read low. `--sort ep_next` gives FPL's own projection for the coming gameweek in either case, but not an independent one this early: it tracks `form` almost exactly until FPL's fixture factor moves off 1.0 (see [FPL Predicted Points](#fpl-predicted-points)). See [Early-Season Confidence](custom-analysis.md#early-season-confidence-gw1-10) and [the early-season caveat](custom-analysis.md#quality--value-scores).

**Rolling window** (`--window` / `-w`): Sets the lookback window for `rolling_pts_per_m` (range 3-10, default from config). Only applies when `--value` is active. `rolling_pts_per_m` measures points per million over the last N qualifying fixtures (minutes > 0), capturing recent form-adjusted value.

### Historical Data

Career-arc analysis across a four-season window ending at the season in progress. The newest two seasons come from the [olbauday/FPL-Core-Insights](https://github.com/olbauday/FPL-Core-Insights) dataset (all it publishes, refreshed several times a day), the two before them from [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League). The window rolls forward each July.

```bash
fpl player Salah --history   # Individual career arc
fpl history                  # All players (compact, for squad-builder)
fpl history --format json
```

In `--format json`, each entry in `seasons` carries `team` as the FPL club code (`teams[].code` in the bootstrap), which is stable across seasons and the same whichever dataset served the season. It is not that season's 1-20 team id, so resolve it through the bootstrap's `code` field rather than `id`.

**Signals:**
- **pts_per_90 trend** - Points per 90 minutes across seasons (improving/declining)
- **cost trajectory** - Price movement across seasons
- **xGI per 90 trend** - Expected goal involvement trend across the window
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

### Underlying Stats (xG)

```bash
fpl xg                      # xG/xA over the last 6 gameweeks
fpl xg -n 10                # Widen the window to 10 gameweeks
fpl xg --all                # Whole season instead of a recent window
fpl xg --format json        # Machine-readable envelope
```

Ranks players on underlying output - xGI per 90, and over/underperformers against it - plus value picks at low ownership when custom analysis is on.

#### Minutes floor

A player has to clear a minutes bar to be analysed at all, and that bar is 60 minutes per gameweek in the window (450 for `--all`). Neither is reachable early in the season: after `N` finished gameweeks nobody can have played more than `N x 90` minutes, so the default 360-minute floor is arithmetically impossible until four gameweeks have finished. At GW3 it admitted nobody, and the command returned an empty analysis that read exactly like "no player is worth showing".

The floor applied is `min(60 x window, 45 x gameweeks played)` - half the minutes the calendar has allowed - and the window itself is clamped to the gameweeks played. At GW3 that analyses the two gameweeks played against a 90-minute bar instead of six against 360. The configured bar binds again once the season catches up: from GW9 for the default six-gameweek window (360 / 45 = 8 finished gameweeks) and from GW11 for `--all` (450 / 45 = 10), after which behaviour is identical to a fixed threshold.

The panel header names the floor that was applied, `metadata` carries `window_label`, `gameweeks_played` and `min_minutes`, and `metadata.warnings` carries an `early_season_minutes_floor` notice while the bar is scaled. `fpl targets --min-minutes` and `fpl differentials --min-minutes` are untouched: a floor the reader asked for explicitly is applied as asked.

When nothing qualifies, `data.empty_reason` says which of the floor and the data caused it - `below_minutes_floor` (players have played, none clears the bar) or `no_minutes_played` (nothing has been played yet) - and table mode prints that sentence instead of three empty tables. `fpl targets` and `fpl differentials` carry and render the same field, and `fpl preview`'s Performance Stats section names the analysed window and prints the reason when it is empty.

### Understat Metrics

Player analysis is enriched with data from [Understat](https://understat.com):

| Metric | Description |
|--------|-------------|
| **npxG** | Non-penalty expected goals. Shows true open-play attacking quality. |
| **xGChain** | Total xG of every possession chain a player is involved in. High xGChain with low xG/xA = consistently dangerous without finishing. |
| **xGBuildup** | Same as xGChain but excludes shooter and assister. Pure "table-setting" metric. |
| **penalty_xG** | xG minus npxG. Flags players whose xG is inflated by penalty duties. |

These metrics appear in `fpl player`, `fpl xg`, and gameweek reports. If Understat is unavailable, agents fall back to FPL-only xG data.

**When a whole club fails to join.** FPL and Understat spell some clubs differently, so a `TEAM_NAME_MAP` gap costs one club's entire squad its npxG, xGChain and every score built on them, while the other 19 look fine. The commands that enrich from the live league payload — `fpl stats --value`, `fpl player`, `fpl allocate`, `fpl xg`, `fpl targets`, `fpl differentials` — report it once per club: on stderr in table mode, and as an `understat_team_unmatched` entry in JSON `metadata.warnings`, one per club, naming both the FPL club and the Understat name it was mapped to. `fpl doctor --providers` checks the same thing ahead of time. A club missing from a *past* season's payload is not this: `fpl returnees` scores a player's current club against the season they played, so the three clubs promoted since are absent from it every year by definition, and that stays a debug line rather than a warning about a map that is fine.

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

`quality_score` and `quality_per_m` are `null` when no Understat match exists. In JSON output (`--format json`), they appear under `info.quality_score` and `info.quality_per_m`. In the Rich panel, they appear as `Quality: 85 | Value: 11.3/£m`. Before GW10 `quality_score` is the value family's prior-informed estimate, and the command says so the same way `fpl stats --value` does: `metadata.warnings` carries `early_season_prior_informed` when last season's pedigree was blended in, or `early_season_small_sample` (before GW6) when it could not be loaded and the score is pure observation; table mode prints the notice to stderr. See [Early-Season Confidence](custom-analysis.md#early-season-confidence-gw1-10).

#### FPL Predicted Points

The FPL API provides gameweek point predictions for every player:

| Field | Description |
|---|---|
| **ep_next** | FPL's predicted points for the next gameweek. Shown as `xPts` on the Points/PPG panel line. Available as a `--sort` field in `fpl stats`. **Early season it is not an independent read:** FPL's projection tracks `form` almost exactly until the fixture factor moves off 1.0 — at GW4 of 2026/27, 603 of 652 players had `ep_next == form`, and every row of a positional `fpl stats -s ep_next` shortlist did. The differences are doubtful players (scaled by chance of playing) and unplayed new signings (seeded above their 0.0 form). Sorting on it that early buys the availability scaling, not a second opinion. |
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
fpl squad --classic                    # Pin the format instead of inferring it
fpl squad grid                         # Fixture difficulty grid (next 6 GWs)
fpl squad grid -n 8 -w Mbeumo          # 8-GW grid with watch list player
fpl squad grid -w "Henderson (CRY)"    # Club picks one of two players of that name
fpl squad grid --format json
```

**`--classic` / `--draft`**: which squad to read. With neither, the format is
inferred from the configured IDs — a draft-only config gives the draft squad,
which is what an unadorned `fpl squad` should do for a draft-only manager, and
what a config that has lost its `classic_entry_id` also looks like. A script
or skill that means one format should say so: `fpl squad --classic` reports
the missing `classic_entry_id` as an error rather than answering with another
league's roster. The two flags are mutually exclusive, and both apply to
`fpl squad grid` — before the subcommand or after it, `fpl squad --classic
grid` and `fpl squad grid --classic` mean the same thing. Table mode names the
format in the heading (`Squad Analysis (Draft)`); JSON carries it in
`metadata.format`.

An entry ID that no longer resolves is reported as such rather than as
"no squad submitted yet" — classic entry IDs are reissued each season, so a
404 on the picks endpoint is checked against `entry/<id>/` before it is
blamed on the calendar. `fpl squad grid` and `fpl captain` share that
diagnosis, so the same broken config reads the same way from any of the three.
Run `fpl doctor` to check the configured IDs.

A `--watch` name that does not land on one player is skipped rather than
fatal — both when nothing matches it and when two players answer to it
exactly (Dean and Jordan Henderson are both `Henderson`). The reason is
warned on stderr, naming both candidates in the ambiguous case, and the grid
still renders for the rest. Add the club (`Henderson (CRY)`) or use the
player ID to choose.

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
refused rather than trusted — the page usually rendered late or the login silently
failed. Table mode labels the panel `Squad Budget (Suspect)`, shows the numbers and keeps
any valid cache, because a reader can see the label and count the rows. `--format json`
gets an `error` envelope and exit 1 instead: `fpl allocate --sell-prices` budgets from
`data` and would take a three-player squad at face value. Re-run with `--refresh`, or
`--visible` to watch the browser. With nothing cached and no `--refresh`, the command
likewise exits 1 with an `error` envelope rather than exiting 0 having shown nothing.

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

**Teams with Easy Fixtures:** ranked on FDR, the mean of the ATK and DEF columns in difference mode, so the ordering matches the columns beside it and `fpl fdr`. The footer names the mode. Per-fixture FDR in the Gameweek Fixtures table is the same figure.

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

**Blanks and doubles:** a zero from a player whose club had no fixture is marked `[BGW]` rather
than read as a choice that failed, and such a player is kept off the Blankers list entirely; a
player whose club played twice is marked `[DGW]`. Which clubs those were is read off the
gameweek being reviewed rather than off the clubs as they stand today, so reviewing an earlier
gameweek judges a player transferred since on the fixtures he actually had at the time. FPL
writes those marks per fixture as it finishes, so a gameweek with a fixture still to complete
cannot answer; there the current clubs answer instead, which can differ from that gameweek's
clubs once a transfer has happened in between.

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

**Standings movement:** position changes derived from point differentials, per-manager highlights. Both tables — this gameweek's and the one before it — are ranked the same way, so managers level on points share a place on each and no arrow is reported for a tie nobody left.

**Fines:** evaluates fines for every manager (not just you) when configured and records the
ruling against the gameweek. A backfilled gameweek is ruled too — the detailed replay rules every configured rule, the coarse tier rules the ones
derivable from cohort points (`last-place`, `below-threshold`) and records that it could not
rule `red-card`, which needs a squad the manager-history endpoint does not return. See
[Season Fines](#season-fines) for the table this builds up.

**Season fines:** the season-to-date *table* appears on console and in the saved report only
at the two milestone gameweeks — GW19 (the chip-availability boundary, which is also the
halfway point) and the finale. Every other week those surfaces stay a this-week view, and
[`fpl league-fines`](#season-fines) answers the season question on demand.

The editorial is deliberately not gated with them. A table and a sentence are different
things: with `--summarise`, the narrative gets the season totals every week and may drop one
into a paragraph where it sharpens what happened ("Bob's fourth last-place of the season"),
taking the numbers verbatim and repeating any qualification the tally carries. It is offered
as optional colour, never a required beat, so a gameweek the total adds nothing to simply
goes unmentioned. Each total names the gameweeks behind it (`Bob: 2 (2 last-place; fined in
GW3, GW10)`), and this gameweek's own fines are handed over with their place in the season
already worked out — "this gameweek's last-place fine is Bob's first of the season" — so the
narrative never has to count fines itself to say whether one is a repeat. That ordinal is
only stated where every earlier gameweek actually ruled that rule against that manager;
where the span holds a gap that could hide an earlier fine — one never captured, one at a
fidelity that could not rule it, or a season that began before the ledger did — the line
names the gap and forbids numbering the fine instead. `--format json` is
ungated too — `metadata.season_fines` is emitted every
week, so a scripted consumer never sees it appear and disappear on a calendar it cannot see.

**LLM editorial** (`--summarise`): Newsletter-style narrative via synthesis provider. Names names, calls out decisions. The editorial is an add-on: if the synthesis provider has no usable API key the recap still renders, still saves its report and still captures the ledger, with the reason on stderr and a `synthesis_provider_unavailable` warning in JSON. `synthesis_summary` is `null` on such a run — the warning is what distinguishes it from a run that never asked for an editorial.

**Streaks:** notable open streaks print under `Streaks:` on console — leaders only, so console stays a highlights view — and in full as a `# League History` section in the saved report. Each is reported as an observed count over its true span (e.g. "3 in the last 11, with 8 not recorded") rather than a bare "in a row" once any gameweek went uncaptured. A streak surfaces once its run reaches the condition's own minimum: 2 gameweeks for weeks on top, gameweek wins, last-place finishes, captain blanks and transfer hits; 3 for waiver hauls and backfires. Bottom-half gameweeks and green-arrow droughts never surface as streaks at all — both restate where the table already shows a manager is, so their run exists to drive the season count's firing rule rather than to be read on its own.

**Replayed gameweeks keep who a player was:** a backfill resolves every pick against today's bootstrap, so a player transferred or renamed since would otherwise land on a past gameweek's row wearing his current club and current name. The recorded name, club and position are kept instead, and a code the gameweek already resolved is never dropped just because the live lookup has stopped finding him. Everything the gameweek itself can answer — points, cards, the pick flags, whether his club had a fixture — is re-derived by the replay as normal, so a repair still improves what it can. The reading goes back to the *earliest* row recorded for that gameweek rather than the newest, so a gameweek an older version already restamped is repaired rather than having the restamp carried forward. A gameweek with nothing recorded yet — a first capture, or a coarse tier being upgraded, which stores no squad — has nothing to keep, and there the current clubs stand.

**Blank gameweeks:** a captain who scored nothing because his club had no fixture is never counted as a captain blank — the missing return was a structural impossibility, not a choice that failed. Which clubs those were is read off the gameweek being captured rather than off the squad as it stands today: FPL's live data marks every player whose club had a fixture that week, so replaying an earlier gameweek records the fixture a player actually had at the time rather than the one his current club had. That matters for anyone transferred between clubs since — without it, a replay erases a real blank, or invents one the player could never have had, and the captain-blank streak and season count move with it. A gameweek still in play cannot answer, because those marks are written per fixture as it finishes; there the clubs in the current bootstrap answer instead, which for a gameweek being played now are the same thing.

**Season counts:** alongside each streak's currently-open run, every condition keeps a season occurrence total that survives resets, so "their sixth gameweek win of the season" is a stated fact, not an inference. Each condition has its own rule for when a total is worth saying out loud — a rare, discrete event affords a generous rule, while a standing table position half the league increments every week needs a strict one:

| Count | Surfaces in an ordinary gameweek when | Others shown alongside |
|---|---|---|
| Gameweek wins | Total hits a multiple of 3; in the season's second half, also a manager's first win | — |
| Last-place finishes | Total hits a multiple of 3; in the second half, also a manager's first | — |
| Captain blanks | Someone's total hits a multiple of 5 | Others blanking that week whose total is ≥ 3 |
| Gameweeks with a transfer hit | Someone's total hits a multiple of 3 | Others taking a hit that week whose total is ≥ 2 |
| Waiver hauls / backfires (draft) | Total hits a multiple of 5 | — |
| Gameweeks on top of the league | Total hits a multiple of 5 | — |
| Gameweeks in the bottom half | Second half of the season only, and someone's total hits a multiple of 10 | Others dropping in that week whose total is within 5 of a milestone |
| Gameweeks without a green arrow | An *unbroken* drought reaches 5 or 10 gameweeks. Gameweeks that began in first place are never counted | — |

League positions used by these conditions are competition-ranked: managers level on points share a place and the next distinct total skips the places they consumed (1, 2, 2, 4), so a tie is never split by the order the cohort happened to arrive in. Classic breaks a points tie on fewest transfers season-to-date, which the ledger does not record, so two managers level on points are genuinely indistinguishable here — a shared lead credits both with the week on top, and a tie straddling the halfway line puts neither in the bottom half. A live draft capture takes the league's own rank instead, which already applies the head-to-head points-for tie-break.

The green-arrow drought reads the current run rather than the season total, since only an unbroken drought tells a story, and it stops firing past 10 so a manager rooted to the bottom of the table never re-announces the same non-fact. A gameweek that *began* in first place never counts towards the drought at all: the leader had nowhere to climb, so the missing green arrow is a structural impossibility rather than a failure, and it is recorded as an unjudged gameweek instead. That gate reads where the gameweek started, not where it finished, so climbing to the summit still breaks a drought — and a manager who falls off the top holds too, since they had nowhere to climb from either and the drop is already told by standings movement. Where a count carries others alongside it, the qualifying rule matches how that count grows: captain blanks and hits stay rare enough that a fixed floor keeps its meaning all season, while bottom-half totals climb every week for half the league, so the company is measured relative to the milestone instead — the managers genuinely level with it, not everyone who has ever been down there. A firing count appears in the report's `## Season Counts` subsection and in the editorial's League History section; everything else stays off both. The full set-piece comes twice a season: at the two milestone gameweeks (GW19 and the finale) both surfaces carry every nonzero count, the same rhythm as the printed fines table. The table above governs the ordinary weeks only — the milestone is deliberately the whole picture, so a count held back all season still appears there, and the second-half rule on bottom-half gameweeks does not keep them out of the GW19 table that closes the first half. Every line states its span and any not-judged gameweeks beside the number; a held gameweek (unknown capture, fixture-less blank, condition not applicable) is never counted and never read as innocence. Totals are computed from ledger rows already on disk, so the first run after upgrading counts back to the partition's first captured gameweek rather than starting from zero. What it counts back over is those rows as they were written: the ledger is append-only, and the two conditions that read a league position (gameweeks on top, gameweeks in the bottom half) take the position stored on each row, which only a fresh capture or an explicit `--backfill-detail` ever derives. A gameweek captured before the competition-ranking fix therefore keeps whatever place its tie was split into, and re-backfilling those gameweeks is what picks the fix up for them.

**Unavailable:** a manager whose position or points total can't be derived this run (e.g. a replayed draft gameweek with no earlier rows) is named under `Unavailable:` on console and in the report rather than silently dropped from Standings Movement.

**JSON:** `--format json` emits one row per manager — the same shape written to the
ledger, built from the rows this run assembled, so manager data is present even when the
store could not be written. `metadata` carries `coverage` (per gameweek: fidelity-tier
counts, unknown managers, whether the file was readable), `season_phase`, `notes_pack`
(every entry, including those below their reporting minimum and every nonzero season
count whether or not it grew this gameweek), `season_fines` (the whole
season tally, emitted every week regardless of the milestone gate the printed surfaces use), `synthesis_summary` (with
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

Three fields say nothing rather than something convenient, because a row outlives the
API that could correct it.

`team_value` is the figure FPL reports as the team's value, and **FPL counts the bank in
it**. A squad worth £99.0m with £1.0m unspent is stored as `team_value: 1000, bank: 10` —
prices are in £0.1m units, so that reads £100.0m and £1.0m. The players alone are
`team_value - bank`: 990, or £99.0m. Both numbers are stored exactly as the API reports
them, so either view is derivable from the row. The field was named `squad_value` before
schema version 2, where the name claimed an exclusion the number never made — a row read
from before that version has the name corrected on the way in.

On the league's first scored gameweek there is no previous table, so
`previous_league_position` is empty rather than repeating the current position, which
would be indistinguishable from a manager who genuinely held their place. And a draft row
leaves `transfer_cost` empty rather than zero: draft charges nothing for a squad change,
so there is no hit to have avoided.

Two rank fields carry the manager's FPL-wide standing, distinct from any position
inside the league: `global_rank` is the cumulative rank for the season to date, and
`global_gw_rank` (schema version 3) is the rank for that gameweek's points alone — the
API's `overall_rank` and `rank` respectively. Both are destroyed at the July rollover
like everything else on this row, which is why `global_gw_rank` exists at all: it was
the one per-gameweek figure the ledger captured `global_rank` for but not itself, until
schema version 3 added it. A row written before that version reads it back as empty
rather than guessed.

`fines` records the fines ruled against that manager that gameweek, keyed by manager
rather than display name so a mid-season rename cannot split a tally and two managers
sharing a name cannot merge into one. Beside it, `fine_rules_evaluated` (schema version
4) records which rule types were actually ruled, whether or not any triggered — without
it an empty `fines` list means three different things at once (nobody was fined, no
rules were configured, no rule was ever checked), and [Season Fines](#season-fines)
would score all three as innocence. A list names exactly the rules ruled, `[]` means
nothing was configured, and empty means nothing is recorded either way: an unknown
capture row, or a row written before schema version 4.

Rows are append-only. Re-running a gameweek that has not changed writes nothing; a
re-run whose numbers differ (bonus points settled, a failed fetch repaired, a coarse
gameweek filled in) appends a superseding row and leaves the old one in place. A file
that cannot be parsed is never reset or overwritten: the run says which file and what to
do about it, still prints the recap from live data, and exits 0.

Each row carries the schema version it was written under. A row from an older version is
brought up to the current shape as it is read, and the line on disk is left as it is; a
row from a newer version — an install ahead of this one, sharing a synced data directory —
is skipped with a warning and preserved untouched rather than read wrongly or discarded.

Two fidelity tiers, both recorded on the row:

| Tier | Source | Carries |
|---|---|---|
| Coarse | Classic manager-history endpoint, one request per manager for the whole season | Points, cumulative total, transfer count and cost, bench points, team value, bank, world rank (season and gameweek), and the fines derivable from cohort points alone (`last-place`, `below-threshold`) |
| Detailed | A live recap run, or `--backfill-detail` replaying a past gameweek | Everything above plus captain, vice, full squad, and transfer or waiver detail |

Classic gaps fill at the coarse tier automatically. `--backfill-detail` upgrades them,
at the cost of one request per manager per gameweek, which is why it is opt-in. Draft
has no manager-history endpoint at all, so a draft gameweek that was never captured can
only be rebuilt with `--backfill-detail`, and only while the season is live.

When a gameweek is missing, coarse, unreadable, or holds a manager whose data could not
be fetched, the run says so on stderr and names the remedy — for an unreadable gameweek
that is the file's own path and the `mv` that retires it, said once however many parts of
the run read that file. A fully captured season stays quiet.

Ephemeral environments (Claude Code on the web, CI, containers) must point
`FPL_CLI_DATA_DIR` at a persistent workspace or the ledger dies with the container. The
first time a season's partition is created, the run prints where it went.

#### Season phase

Every recap is stamped with where its gameweek sits in the season's arc. The phase sets
the editorial's tone in the retrospective prompt — it is scene-setting context for the
writer, never printed in the saved report — and the finale is the one phase whose notes
pack rescans every captured gameweek instead of a trailing six-gameweek window.

| Phase | Gameweeks | Framing it states |
|---|---|---|
| `opener` | The first gameweek (GW1) | "the season opener" |
| `pre_chip_boundary` (classic) | Up to the gameweek before the chip split (GW2-18) | "before the GW19 chip-availability boundary" |
| `pre_chip_boundary` (draft) | Up to the gameweek before the chip split (GW2-18) | "before the season's halfway point (GW19)" |
| `midpoint` (classic) | Chip split to the start of the run-in (GW19-31) | "the season midpoint, past the GW19 chip boundary and before the run-in" |
| `midpoint` (draft) | Chip split to the start of the run-in (GW19-31) | "the season midpoint, past the GW19 halfway point and before the run-in" |
| `run_in` | The last six gameweeks before the final one (GW32-37) | "in the run-in to the season finale (GW38)" |
| `finale` | The final gameweek and anything past it (GW38+) | "the season finale" |

Draft has no chip mechanics, so `pre_chip_boundary` and `midpoint` drop the chip framing
for a draft league in favour of the same halfway-point wording, plain of any chip
language.

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
| `league_history_store_unreadable` | The gameweek's file could not be read or written; it is left untouched and the recap still renders from live data. One warning per affected gameweek, and the message names the file and the `mv` that retires it |
| `league_history_coverage` | One line per coverage gap: gameweeks missing, held at the coarse tier, or holding unknown managers. An unreadable gameweek is not a gap — it is reported as `league_history_store_unreadable`, and `--backfill-detail` skips it rather than writing to a file it cannot parse |
| `league_history_unmatched_players` | A draft squad player could not be matched to a main-game player, so their recorded points are zero rather than a real score |
| `league_history_transfer_detail_short` | Fewer transfers were captured than the manager's recorded count, so the stored list is incomplete rather than empty |
| `league_history_standings_truncated` | The standings response covered only part of the league, so the gameweek is recorded for that subset only |
| `league_history_backfill_manager_unreachable` | One manager's history could not be fetched; their gameweeks stay unknown and are re-attempted next run |
| `league_history_backfill_replay_failed` | One gameweek could not be replayed in detail; the others are unaffected |
| `league_history_backfill_write_failed` | A backfilled gameweek could not be written; the rest of the backfill continues |
| `league_history_identity_carried` | A finished gameweek kept the name, club or position it already had recorded for one or more players rather than the ones today's bootstrap gives them, or restored a player reference this capture had lost. Raised by a re-capture of a finished gameweek as well as by a replay; any one of the four on its own raises it |
| `synthesis_provider_unavailable` | `--summarise` was asked for but the synthesis provider had no usable key; everything else in the recap, the capture included, ran normally |

None of these change the exit code — `league-recap` exits 0 whenever the recap itself
rendered, and a skipped editorial (`synthesis_provider_unavailable`) is no exception.

Three things do exit 1, all emitting the shared `{"command", "error"}` envelope on stdout
under `--format json` (see [JSON Output](#json-output)): an unreachable FPL API, a
gameweek that could not be resolved at all, and a reconciliation failure. Only the second
softens on the table path, where it prints the same message and exits 0. The distinction
matters when scripting a retry — an outage is worth retrying, the other two are not.

### Season Fines

Who owes what this season, folded out of the fines each `league-recap` recorded. The recap
prints this table only at GW19 and the finale (though its editorial may reference the totals
any week); this command answers the same question any time.

```bash
fpl league-fines                  # Season totals through the latest recorded gameweek
fpl league-fines -g 12            # Tally through GW12 only
fpl league-fines --season 2025-26 # An earlier season - the ledger partitions by season
fpl league-fines --draft          # Use draft league
fpl league-fines --format json    # JSON envelope for scripting/agents
```

Reads the ledger and nothing else, so it makes no network calls and works for any season
still on disk. Nothing is re-ruled: a fine is counted exactly as the gameweek recorded it,
so changing a `below-threshold` value in settings moves future rulings and leaves history
alone. Backfill holds the same line — a repair carries an already-recorded ruling forward
untouched, and re-rules a gameweek only when it genuinely fills something in (a manager
repaired out of an unknown row, or a coarse gameweek upgraded to a fidelity that can rule
more), in which case it re-rules the whole cohort together so a cohort-relative rule like
`last-place` cannot end up recorded against two managers in one gameweek.

**Counts, not money.** `penalty` is free text, so "4 last-place, 1 red-card" is supportable
and "£14 owed" is not — that would need a numeric amount stamped onto the row at capture
time. Settlement and the configured `escalation_note` are not modelled.

**Coverage is stated, not assumed.** A zero is only trustworthy when the gameweeks behind
it were ruled, so every gameweek that was not is named beneath the table: never captured,
unreadable, captured but reaching nobody, holding no record of what was ruled, recording a
ruling on no rules at all, or captured at a fidelity that could not rule a given rule. A manager's own gaps are named
too — an unknown capture row means nothing was ruled against them that week — and their
"GWs ruled" count carries an asterisk whenever their span holds one. A mid-season joiner
keeps their real, lower totals and is qualified rather than scaled up; a manager who has
since left the league keeps the fines already ruled against them.

**JSON:** `--format json` emits one entry per recorded manager, fined or not, each with
`counts` (one key per rule type, zero included), `total`, `fined_gameweeks`,
`ruled_gameweeks`, `unruled_gameweeks`, `first_recorded_gameweek`, `last_recorded_gameweek`
and `is_fully_ruled`. `metadata` carries `season`, `fpl_format`, `league_id`, `gameweek`
(the through point), `start_gameweek`, `rule_types`, `total_fines` and `qualifiers` — the
same sentences printed beneath the table. Exits 1 with the shared `{"command", "error"}`
envelope when no league id is configured for the format, or the season label is malformed.

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
- `returnee_snapshot.json` — the returnee radar's week-over-week snapshot: season label matches (a previous season's is discarded and rebuilt on the next `fpl returnees` run), and reports the gameweek it currently holds

**Environment** — which directory each of config/data/cache resolved to and whether an `FPL_CLI_*` override is in effect, plus whether `settings.yaml` exists.

Each finding is classified as **broken** (wrong answers now — fix it today), **stale** (self-corrects or needs one routine refresh), **skipped** (not configured/present), or **unchecked** (API unreachable). Exits non-zero when anything is broken, so it can gate scripts.

**`--providers`** checks the external data sources instead of the local setup. None of them version anything, and the tool degrades gracefully everywhere, so upstream drift otherwise surfaces as plausible but wrong output — a renamed stat field zeroes every player's xG without an error. Each probe asserts shape and volume, not just reachability, and where a column check is not the contract it runs the parser itself:

- **FPL API** — bootstrap has 20 teams / a sane player count / 38 gameweeks, and every stat field the tool reads is present in the raw data (a missing one would silently read as 0)
- **Draft API** — bootstrap resolves with 20 teams and a sane player count
- **vaastav dataset** — for each season it serves (the two oldest of the four-season window), `players_raw.csv` exists upstream, covers the columns the parser reads, and clears a row-count floor
- **Core-Insights dataset** — same for `players.csv` and `playerstats.csv` in each season it serves (last season and the one in progress; it is the sole source for both), plus the current season's per-gameweek files at the latest finished gameweek. `players.csv` must also parse into the player lookup every other file joins on, and the per-gameweek files are run through the parsers the scoring commands use rather than a header check: the columns can all be present over values nothing survives (Elo published blank at the start of a season empties the match join), so the probe asserts the parse yields records and names the signals a zero costs. A file missing or parsing to nothing only for the newest gameweek is a publishing lag or a backfill in progress (stale); the same problem two gameweeks running is a layout or format change (broken)
- **Understat** — league data is non-empty, and every current club resolves to rows in Understat's own data (an unresolved club silently loses xG enrichment); early in the season an unresolved name is reported as stale, since Understat only lists a club once it has ingested a match for it. The club test is the enrichment's own gate (`understat_club_rows`), called on the same league payload the scoring commands scan, so the probe and the runtime cannot reach different verdicts about the same club — a second copy of the gate written beside the probe is exactly how they diverged in #229
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
  enrich_concurrency: 4          # Most --enrich searches in flight at once
  enrich_query_spacing_seconds: 1.0  # Least time between two --enrich search starts

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

**Rate limits.** Every provider retries an HTTP 429 before reporting it: three retries with exponential backoff and jitter (roughly 1-2s, 2-4s, then 4-8s), or the server's `Retry-After` when it sends one, within a 30-second budget for the whole wait. Each retry is announced on stderr with the wait, so a rate-limited request is never a silent hang. A `Retry-After` the budget cannot cover is not waited out: the request fails at once as rate-limited, distinct from every other error and carrying the server's hint, so a command that batches queries (`fpl returnees --enrich`) can retry that subset later rather than lose it, and a command that makes one query degrades within seconds rather than a minute. Any other error status is reported at once.

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
