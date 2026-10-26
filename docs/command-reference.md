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
reports the problem in `metadata.warnings` (and on stderr), rather than failing. Nor does
the format change it: whatever a command answers with an `error` envelope it also refuses
in table mode, with the same reason on stderr and the same exit 1. Several used to soften
the table path to exit 0 — `review` and `league-recap` on a gameweek neither could
resolve, `fpl history` on a provider that refused, `fpl chips timing` with no
`classic_entry_id` and `fpl waivers` with no `draft_league_id` — and none do now.

**Feed coverage warnings.** Every command that scores players (`captain`, `targets`,
`differentials`, `waivers`, `transfer-eval`, `stats --value`, `player`, `allocate`, and the
gw-prep scripts behind bench order and starting XI) measures how much of the player pool
each data feed actually reached, and reports a feed that reached too little of it as a
`metadata.warnings` entry (stderr in table mode) rather than scoring on quietly. The
tripwires before this fired only on total loss, so a feed that reached half the pool, or a
field that read 0 where it should have read "unknown", degraded every score with nothing
said. One code per feed, each naming what reached whom, the consequence for the scores, and
the likely cause:

| Code | Feed | Judged over | Scores without it |
|---|---|---|---|
| `understat_coverage_low` | Understat npxG | players with minutes; floor 80% once three gameweeks have finished, 50% before | FPL xGI in place of npxG and xGChain |
| `adjusted_npxg_coverage_low` | opponent-adjusted npxG (Core-Insights) | players with 4+ appearances in the signal's 12-gameweek window; floor 75% | raw Understat npxG |
| `consistency_coverage_low` | the match files' half of the consistency signals (Core-Insights) | players with 6+ appearances of 60+ minutes in the window; floor 75% | consistency from the FPL history alone: no opponent adjustment, no involvement rate for attackers, no save consistency for keepers |
| `history_prior_coverage_low` | last season's history behind the early-season prior | every player, before GW10 only; floor 25% | pure observation until GW10 |
| `gw_history_coverage_low` | per-gameweek history (`element-summary`) | players with minutes; floor 95% | neutral form trajectory, no consistency signal |
| `fixture_ratings_coverage_low` | team ratings | next-gameweek fixtures; any unrated side | a neutral 4.0 fixture difficulty |

A share is judged only over 25 or more eligible players, so `fpl stats --value --team ARS`
does not call two name misses a feed failure; fixtures are judged one by one. The two
Core-Insights rows read eligibility off the FPL per-gameweek history rather than the
season's `starts`, because the signals are rolling windows (ten starts by GW12 and none
since is unreachable at GW30 by design), and they leave the latest gameweek out of the
count: FPL writes a history row from kick-off while Core-Insights publishes after the
gameweek, so a player who reached the minimum only with it is not expected to be covered
yet. A command that fetches match data without the per-gameweek history has nobody to
judge them over and says nothing. The prior row counts the set as the loader returned it,
so a half-joined previous season reads as the share it reached rather than as nobody,
while the scores themselves treat a set under the floor as no prior at all. `fpl player`
reports only the prior's coverage, the one feed it reads over the whole pool rather than
the players shown, and only beside a `quality_score` it computed. `fpl waivers` adds
`draft_squads_unread` when a rival's squad could not be fetched, since that rival's
players then read as available and a claim on one would fail; the draft columns of
`fpl player` and `fpl stats`, which read the same ownership map, carry the same entry.
`fpl player` adds `draft_provenance_unavailable` when the league's history could not be
shown complete, so its draft line says only who owns the player and `info.draft` is null
(see [Draft ownership and provenance](#draft-ownership-and-provenance)); `fpl stats` prints the
same message on stderr in table mode, the one mode that reads the history.

The `error` string is the reason itself, not a category for it. `fpl history` and
`fpl price-history` used to answer an unreachable provider with a fixed `Failed to fetch
historical data` / `Failed to fetch price history data` while the upstream's own message
went to stderr — readable to someone watching the terminal, invisible to the consumer told
to script against `error`. Both now carry the cause, as `fpl chips timing` carries the
fixture agent's.

The table applies to every way a command can end, not just the ones it was written for.
A command that cannot reach the FPL API, that needs an entry ID you have not configured,
that has nothing cached to show, or that finds a `settings.yaml` block it cannot read
reports it as an `error` envelope and exits 1 — where several used to print the reason to
stdout, or exit 0 with nothing on it at all. A malformed `fines:` rule is the one to know
about: the block is read on nearly every command, so the envelope naming it can come back
from `fpl status` or `fpl league-recap` as readily as from `fpl league-fines`, and the
message names the rule and the valid set.

A relative `FPL_CLI_CONFIG_DIR` / `FPL_CLI_DATA_DIR` / `FPL_CLI_CACHE_DIR` override is
rejected before any command runs, so this one case has no parsed `--format` to read when it
fails — the CLI scans the raw command line for `--format json` instead. The envelope's
`command` names your subcommand where that is unambiguous (`fpl status`, or a bare group like
`fpl chips`), and falls back to `"fpl"` for a subgroup's own subcommand (`fpl chips timing`,
`fpl squad grid`, `fpl intel show`) or a line that was nothing but options — those commands
don't name their own envelope after either word you typed, and guessing wrong would be worse
than saying so.

**Table mode splits the same way.** Without `--format json` the output you asked for goes
to stdout and everything else goes to stderr — warnings, progress notices, and the reason
a command exited nonzero. `fpl squad grid 2>/dev/null` prints a grid or prints nothing;
it never prints half an explanation. That holds for every command that takes `--format`.
Three that do not — `fpl chips sync`, `fpl chips add` and `fpl preview` — still print the
reason they gave up on stdout and exit 0, so a redirect captures the explanation instead
of the output and `2>/dev/null` will not quieten it. Everywhere else, redirecting either
stream means the same thing whichever one you run:

```bash
fpl stats >players.txt          # players in the file, any complaint on the terminal
fpl stats 2>/dev/null           # quiet, and empty if it failed
```

### Player identifiers

The main game and the draft game number their players independently. The two numberings
agree for most of the pool and part ways over the players each game registered after the
season's start, since each numbers its additions on its own — so an id
read across the boundary does not miss, it lands on someone else
(#209 inside the waiver scoring; #418 where the gw-prep skill kept a returnee only if its
`fpl returnees` id appeared in the `fpl waivers` pool, so an unowned late addition read as
rival-owned). Every payload therefore follows one convention:

- A bare `id` on a player row is the element id of **the game the command serves**: the
  draft game's for a draft command (`fpl waivers`), the main game's for every classic or
  format-agnostic command, whatever `metadata.format` says.
- `code` is the identifier both games share, and the only field to join two commands'
  player rows on. Never join on `id`, and never on name and club: the main game renamed
  Savinho to Sávio mid-season while the draft game kept the old spelling (#168).
- A `code` that is not known is `null`, never `0` or a guess: a shared `0` would join every
  such player to every other.

Clubs follow the same rule one level up. Each game publishes its own `teams` list and numbers
its 20 clubs 1-20 for the season; the numberings agree today, but nothing ties them together,
and a draft club id read as a main-game one would name a different club rather than missing.
What both lists share is the club's permanent `code` (Arsenal is id 1, code 3), so every
draft path translates the draft game's club id by `code` before reading main-game fixtures,
form or team data (#423), and a `team_id` on any payload's player row is **the main game's**,
`fpl waivers` included. A matched player's club is his main-game player's; an unmatched one's is
his draft club translated by code. A club the main game does not list comes out as `null`, never
the raw draft id, and `fpl waivers` says so with a `draft_club_untranslated` entry in
`metadata.warnings` (stderr in table mode) naming the clubs. `fpl doctor --providers` reports a
`Draft club ids` row that names any club the two lists disagree on.

Which namespace each payload's `id` is in, and whether the row carries `code`:

| Command | Player rows | `id` is | `code` |
|---|---|---|---|
| `waivers` | `data.pool[]`, `top_targets[]`, `targets_by_position.*[]`, `current_squad[]`, `recent_releases[]` | draft | yes |
| `waivers` | `data.recommendations[].target` / `.drop` | no id (name, club, position) | no |
| `returnees` | `data.entries[]` | main | yes |
| `returnees` | `data.departures[]` | main | yes; null only for a player who has left the player pool and was stored before the radar recorded `code` |
| `stats` | `data[]` | main, under a draft config too | yes |
| `player` | `data[].info` | main, under a draft config too | yes; `info.draft` (#460) names managers by draft `entry_id` and carries no player id |
| `captain` | `top_picks[]`, `differential_picks[]`, `all_candidates[]`, `recommendation` | main | yes |
| `differentials` | `differentials.*[]`, `differential_captains[]` | main | yes |
| `targets` | `targets.*[]`, `players[]` | main | yes |
| `xg` | `players[]` | main | yes |
| `xg` | `underperformers[]`, `overperformers[]`, `value_picks[]`, `top_xgi_per_90[]` | no id | no |
| `allocate` | `data[]` | main | yes |
| `squad sell-prices` | `data[]` | main (the picks `element`) | yes; null where the scrape could not read the player list, or on a cache scraped before `code` was recorded, and `metadata.warnings` then carries `sell_prices_code_missing` (re-run `--refresh`) |
| `transfer-eval` | `data.out_player`, `data.in_players[]` | main, in draft format too | yes |
| `price-history` | `data[]` | main, named `element` | yes, from the trend data's own player list for the same season; null for a player missing from it |
| `history` | `data[]` | no id | yes |
| `league-recap` | `squad[]`, `captain`, `vice_captain`, `transfers[]`, `transactions[]`, `lost_claims[]`, `fines[].players[]` | no id | yes (`code`, `player_in_code`, `player_out_code`) |
| `league-recap` | `metadata.standout_players.top_scorers[]`, `.blanks[]` | main, under `--draft` too | yes |
| `intel`, `intel show`, `intel resolve` | player notes, `intel resolve` rows | no id | yes, null where unresolved |
| `squad grid` (classic and `--draft`) | `data[]` | no id (name, position, club) | no |
| `squad` | `position_analysis.*.players[]`, `injury_risks[]`, `form_analysis.*[]` | no id | no |
| `fdr --my-squad` (classic and `--draft`) | `squad_exposure[].players` | no id (names) | no |
| `chips timing` | `data[].players` | no id (names) | no |
| `status` | `classic.flagged_players[]`, `draft.flagged_players[]` | no id | no |

The gw-prep scripts (`bench_order.py`, `starting_xi.py`, `transfer_eval.py`) take `Name (CLUB)`
on the command line, resolve it against the main game and emit main ids with `code` beside
them (`optimal_order`, `availability_risks` and `formation_context.sole_coverage`;
`starting_xi`, `bench` and `excluded_players`; `out_player` and `in_players`);
`validate_draft_waivers.py` matches a claim to the pool on normalised name at a
position and never reads `id`. The gw-prep skill's one cross-command join — the returnee
radar against the waiver pool — is done by `claimable_returnees.py`, on `code`. Every row
above that emits `id` carries `code` beside it (#419), and a new payload with player rows
carries `code` from the start.

`tests/test_cli_json_contract.py` holds this table to the code (#422). Every `--format json`
command has to be named in it or declared to carry no player rows, every row whose `id` is
main or draft has to say `code` is carried, and each of those commands and gw-prep scripts
needs a test that runs its real output through `assert_player_rows_carry_code()`
(`tests/conftest.py`). A new command, or a JSON mode on one that has none today, fails
there until it is classified here.

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
- **Budget** - affordability gap: `bank + sell_price - in_price`, the OUT player's sell price recomputed at today's price (see [Sell Prices](#sell-prices--transfer-affordability)). `metadata.warnings` (stderr in table mode) carries `sell_prices_as_scraped` when that price could not be checked and `sell_prices_player_not_cached` when the OUT player is not in the cached squad *(classic only, requires scraper cache)*

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

Ownership is read from every rival's actual squad; a squad that could not be fetched is reported as `draft_squads_unread` in `metadata.warnings` (stderr in table mode), naming the entry, because its players would otherwise read as available. Waiver score combines xGI, form, PPG, 3-GW matchup quality, and consistency (CV-xGI percentile bonus, phased in GW6-10), attenuated by position multiplier (GK 0.7, DEF 0.85), and normalised to 0-100. Keepers swap the xGI term for saves/90, defensive quality and clean-sheet rate, read from the main-game player each draft element is joined to. Defenders swap it for DC/90 from the same join (the draft bootstrap has defensive contribution only as a season total); every JSON row's `dc_per_90` is that main-game rate, display-only outside the defender score, and a defender the join misses scores at 0. The same join carries every player's history, prior, adjusted npxG and consistency signals, so a player in the unowned pool the join misses is named in a `draft_player_unjoined` entry in `metadata.warnings` (stderr in table mode), which lists only the terms live that gameweek (the prior until GW10, consistency from GW6, adjusted npxG when fixture adjustment is on) and gives how many of each position with the first three names in ranked order (a squad or released defender it misses keeps 0 without an entry). Keepers and defenders always count, since they also lose their position-specific term; midfielders and forwards count when a shared term is live. The miss can move a score up as well as down, so without the entry it would read as a ranking on merit; an empty main-game player list gets one sentence saying no draft player could be joined. See [Join misses](custom-analysis.md#waiver-score). Uses a stricter minutes factor than target/differential because draft waivers are a season commitment. Before GW10 the quality half of the score carries the [early-season prior blend](custom-analysis.md#early-season-confidence-gw1-10); the matchup, position-need, team-stacking and consistency terms stay pure observation. The command carries the same early-season notice as `fpl targets` (`metadata.warnings` in JSON, stderr in table mode), naming `waiver_score`. On this path the prior can go missing two ways — last season's history could not be loaded, or the draft element found no main-game counterpart to join to — and either reads as `early_season_small_sample`. See [Waiver Score](custom-analysis.md#waiver-score) for the full formula.

JSON `data` carries `top_targets` and `targets_by_position` (ranked), `pool` (every unowned player, one `{id, code, player_name, position, team_short}` row each, unranked and untruncated), `current_squad` and `recent_releases`, every row of which carries `id`, the **draft** game's element id, and `code`, the identifier both games share. Every row outside `pool` also carries `appearances`, the gameweeks the player featured in, derived from total points over ppg as the main game derives it (at least 1 for any player with minutes): the minutes factor reads it, and from GW6 a player with none loses his per-90 and matchup terms. (`recommendations[].target` / `.drop` name a player by name, club and position and carry neither.) A consumer matching the pool against a main-game payload — the gw-prep skill does, against `fpl returnees` — joins on `code`: the two games number their elements independently, and a player added mid-season holds a different id in each, so an `id` join lands on a different player rather than missing (#418). See [Player identifiers](#player-identifiers).

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
| `--sell-prices` | - | Path to sell-prices JSON file (from `fpl squad sell-prices --format json`). Solver uses sell prices for owned players in budget constraint. Budget auto-computed as `sum(sell_prices) + bank` unless `--budget` is explicitly set. The file's `sell_prices_as_scraped` warnings are repeated in `metadata.warnings` (stderr in table mode), since the budget is built on those prices |
| `--format` | table | `table` or `json` |

Scores ~500 eligible players, adjusts for fixture difficulty over the planning horizon, then solves for the budget-constrained optimum across all 8 valid formations. See [Squad Allocator](custom-analysis.md#squad-allocator) for scoring methodology, fixture coefficients, and solver detail.

**JSON output fields (horizon >= 2):** `id`, `code`, `web_name`, `team`, `position`, `price`, `quality_score` (0-100), `raw_quality` (float), `role` (starter/bench), `captain_gws`. Metadata includes `formation`, `budget_used`, `budget_remaining`, `captain_schedule`, `solver_status`, and `warnings` — the early-season quality notice before GW10 (`early_season_prior_informed`, or `early_season_small_sample` when last season's history could not be loaded and the solver ranked on pure observation), empty otherwise and at horizon 1; table mode prints it to stderr.

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
fpl ratings update --refresh-prior # Rebuild last season's prior from scratch
```

Until completed results can rate teams, ratings are estimated from the previous season (promoted teams from Championship form) and every fixture-difficulty view says so. That covers pre-season and the gap between GW1 kicking off and its first results landing. `fpl ratings update` falls back to the same estimate rather than reporting that there is nothing to calculate and leaving a stale file in place - including when the file on disk still rates last season's clubs, which is non-empty but leaves the promoted sides unrated.

Wherever it applies that prior, `fpl ratings update` says which clubs the prior rates from outside their Premier League record - `Last season's prior rates COV, HUL, IPS from Championship results re-expressed in Premier League units - per-club inputs in .../team_ratings_prior.yaml` - and that file carries an `inputs` block naming, per club, the basis (`premier_league` / `championship` / `promoted_fallback` / `incomplete_record`), the matches behind it and the per-game rates it was ranked on (for a Championship side, both as played and as converted). A promoted club is one with no completed record in last season's Premier League pool; a club returning after a single season away is treated the same as any other promoted side, whatever Understat serves for the season it spent in the Championship (#235). A club whose record was served but is not a season's worth, and has no Championship record either, is rated neutral mid-table rather than as a promoted side, and more than three clubs absent from the pool is warned about as a continuing club that failed to join.

The prior is saved to `team_ratings_prior.yaml` and reused between runs, and it rebuilds itself when the league's clubs change, when the methodology moves on, or when an input it went without becomes available. That last case is what `FOOTBALL_DATA_API_KEY` controls: without a key there is no Championship record, so every promoted side shares one flat bottom-of-table estimate, and the summary says so with the remedy attached - `COV, HUL, IPS on the flat promoted estimate (no Championship record; set FOOTBALL_DATA_API_KEY to rate them on last season's Championship results)`. Setting the key then takes effect on the next command, with no file to delete.

A rebuild can only ever improve the prior, never replace it with a worse one. It is refused if it comes back empty, if it fell through to a noisier source than the cached prior used, or if it leaves more clubs on an estimate than the cached prior did - so Understat failing mid-run, or football-data rate-limiting, leaves the saved prior standing. Each input change buys one rebuild attempt, recorded whether or not it improved anything, so a provider that stays down costs one attempt rather than one per command. When a key is set and promoted sides are still on the flat estimate, the summary says so and names the retry - `` COV, HUL, IPS on the flat promoted estimate (football-data.org served no Championship record for them; `fpl ratings update --refresh-prior` retries) ``. `--refresh-prior` forces a rebuild for a reason provenance cannot infer - a source that answered, but answered wrongly - and is held to the same guard, so it too can only end with the prior it started with or a better one.

A club that has played only one venue is rated on it. Once GW1 finishes, every club has exactly one result, so requiring both a home and an away record would drop the entire league and report ten finished fixtures as nothing to calculate from. The unplayed venue is estimated from the played one, rescaled by the gap between home and away scoring across the window, and the command names the clubs whose second venue is an estimate. The `Games` column shows what was actually played (`1H/0A`), and single-gameweek evidence still carries only 1/7 of the weight against the prior.

Early-season results are shrunk toward that previous-season prior, by the automatic refresh and by `fpl ratings update` alike: a one-gameweek sample carries 1/7 of the weight, six gameweeks half, and the prior drops out entirely once GW12 has completed. Shrinkage is gated on how far the season has run, so a narrow window late on is not blended -- `--since-gw 30` at GW34 saves recent form alone. Inside the early-season window the weight follows the size of that window rather than the gameweek number, so `--since-gw 8` at GW10 is weighted as three gameweeks of evidence. Blended files are stamped `calculated_blended` / `understat_xg_blended` / `auto_calculated_blended` so `fpl ratings` shows that shrinkage was applied. While the sample is shorter than six gameweeks the prior still carries most of the weight, and every fixture-difficulty view says so - `Ratings are mostly last season's prior - 1 gameweek of results carries 14% of the weight`. That replaces the pre-season estimate warning, which stops applying the moment a gameweek completes.

Ratings are tied to the season that produced them. A file carried across a season boundary is ignored rather than served, because it rates the three relegated clubs and knows nothing about the three promoted ones. Which season is "in progress" comes from GW1's deadline, not the July clock, so a season overrunning 1 July keeps its own ratings instead of having them discarded with a third of the run-in still to play. Ratings that cover the wrong set of clubs are called out by name — `team_ratings.yaml is missing COV, HUL, IPS and still rates BUR, WHU, WOL` — which catches a rollover that a "days old" check cannot: a file rebuilt in early August is new by date and still describes last season's league.

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

**Draft column:** with a draft league configured and the format not classic, the table adds who owns each player, tagged with how they came by him where the league's history says (`Alice R3`, `Bob GW4 W`; see below). JSON carries no draft column and reads no history.

### Draft ownership and provenance

With a draft league configured and the format not classic, `fpl player` prints a `Draft:` line
and `fpl stats` a Draft column. Both say who owns each player, read from every manager's squad,
and both say how he came to stand there (#460), from the same replay of the league's draft,
accepted waiver and free-agent moves and processed trades that the draft recap's Standout
Players lines use ([How a draft player came to his manager](#league-recap)). The history is read
alongside the squads and run to the coming gameweek, the one FPL's own ownership list describes,
so a waiver run or free-agent move already processed for it is in the history even while the
squads of the current gameweek do not show it yet.

`fpl player` puts it in brackets after the owner, and beneath, when the player's history has a
step the draft line does not name (anything beyond his latest step and his draft pick), the whole
chain on one line:

```text
Draft: Owned by Alice (round-3 pick)
Draft: Owned by Bob (GW4 waiver pickup; Cam's round-5 pick)
Draft: Owned by Dan (GW9 free-agent signing; undrafted)
Draft: Owned by Dan (acquired from Alice in a GW7 trade; Alice's round-2 pick)
Draft: Available (released by Dan in GW5; Dan's round-9 pick)
Draft: Available (undrafted)
Draft history: Alice's round-9 pick; released GW2; waiver to Bob GW5; released GW6; waiver to Cam GW7
```

The label is the latest link that brought him to his owner, so a manager who dropped their own
pick and signed him again reads as the signing, with the pick behind it. A player nobody holds
reads as his last exit and whether he was drafted. The draft named is the one in force (the
latest completed), so after a redraft a pick says "in the GW20 redraft", a player it passed over
is "undrafted in the GW20 redraft" whatever the first draft did, and the chain starts at the
redraft. An auto-pick says so (`round-3 auto-pick`). Where the draft's picks are lost (neither the
API nor a saved snapshot has them) the drafter is rebuilt from the squads at the draft's
gameweek and the round is not guessed: `drafted, round unknown`, `Dan's pick, round unknown`.
Managers are named by the league's entries, never with a pronoun.

JSON keeps `info.draft_ownership` (`Owned by Alice` or `Available`) and adds `info.draft` beside
it, null where the history cannot say:

| Field | Meaning |
|-------|---------|
| `owner` | `{entry_id, name}` of the manager holding him, null for a free agent |
| `acquisition` | `draft`, `waiver`, `free_agent` or `trade` for an owned player; `released` or `undrafted` for a free agent |
| `gameweek` | The gameweek of that latest link: the draft's own for a pick, the release's for a free agent, null for `undrafted` |
| `draft_id`, `draft_gameweek`, `redraft` | The draft in force, and whether the league has drafted more than once |
| `drafter`, `round`, `pick`, `overall_pick`, `auto_pick` | His pick in the draft in force, whatever has happened since (a waiver pickup still names who drafted him); all null when nobody drafted him |
| `round_unknown` | True where he was drafted but the draft's picks are lost: `drafter` is set, the four pick fields are null |
| `counterparty` | The manager he came from in a trade, or who released him for a free agent; null otherwise |
| `history` | The whole chain, oldest first: `{kind, gameweek, manager, from_manager, round, pick, overall_pick, auto_pick}` per step, `kind` one of `draft`, `waiver`, `free_agent`, `trade`, `release` |

`fpl stats` keeps to one short tag beside the owner, with a key under the table: `R3` for a
round-3 pick (`R?` with the round unknown), `GW4 W` for a waiver pickup, `GW9 FA` for a
free-agent signing, `GW7 T` for a trade. Free agents stay `Available`.

A player is described only where the owner shown is the history's, and so FPL's own, so a line
never pairs one manager with another's pick: a player the squads put with another manager (a move
processed for the coming gameweek that the current squads do not show yet), or show available
because their squad could not be read, gets the plain line. Where the history cannot be shown
complete (a log could not be read, or the replay did not end where FPL's ownership list says
everyone is), nobody is described, the lines read as they did before, and a
`draft_provenance_unavailable` warning says why. It costs the draft's choices, the trades and the
transaction log, read alongside the squads, plus one squad per manager at the draft's gameweek
when its picks are lost. Those squads are read once: the first lookup whose history checks out,
once that gameweek is under way, keeps them in the cache dir, and later lookups rebuild from the
copy under the same checks. `fpl stats` refuses an unknown `--team` before reading any of it.

### Historical Data

Career-arc analysis across a four-season window ending at the season in progress. The newest two seasons come from the [olbauday/FPL-Core-Insights](https://github.com/olbauday/FPL-Core-Insights) dataset (all it publishes, refreshed several times a day), the two before them from [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League). The window rolls forward each July.

```bash
fpl player Salah --history   # Individual career arc
fpl history                  # All players (compact, for squad-builder)
fpl history --format json
```

In `--format json`, each entry in `seasons` carries `team_code`, the FPL club code (`teams[].code` in the bootstrap), which is stable across seasons and the same whichever dataset served the season. It is not that season's 1-20 team id, so resolve it through the bootstrap's `code` field rather than `id`. The same number is also emitted as `team`, which is **deprecated** and kept only for consumers written before `team_code` existed: every other payload's `team` is a short name like `"ARS"`, so the name misdescribes the value (#423). Read `team_code`; `team` will be removed in a future release.

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

**When the payload stops carrying npxG.** Understat's `npxG` field reads as unknown rather than 0 when it is absent or blank: `npxG`, `npxG_per_90`, `penalty_xG` and `penalty_xG_per_90` are then `null` in `fpl player`'s JSON (`n/a` in its panel), the quality score falls back to FPL xGI for that player exactly as it does with no match, and the feed coverage warning above names the gap once it reaches most of the pool. A 0 would have zeroed the attacking component of every quality score without a word.

**When one player fails to join.** A player whose club Understat *does* carry can still fail to match on name, and that is not always a defect: Understat has not ingested the gameweek in progress, names the player outright differently (FPL's `A.Becker` is Understat's `Alisson`), or simply has no row for them. Those read as `null` quality and value scores rather than as zeros, so the player is ranked as "no data" rather than as bad. A name the two sources merely *spell* differently does join, through a last-resort pass that stays inside the player's club and needs their minutes to agree and no second candidate: a transliteration variant (`Yarmoliuk` and `Yehor Yarmolyuk`), a double-barrelled or middle name one source drops (`Gannon-Doak` and `Ben Doak`, `Walle Egeli` and `Sindre Egeli`), or a mononym inside a longer name (`Jair Cunha` and `Jair`). A shared word alone is never enough — `João Pedro` and `Pedro Neto` share one — so a player whose row is genuinely missing stays `null` rather than wearing a club-mate's numbers. A few percent of a club's players missing this way is normal and stays a debug line naming each one with a running rate (`No Understat row matched ... — 3 of 351 FPL players with minutes unmatched so far this run`); the rate is what to watch, since a jump in it means the two sources have stopped agreeing about names rather than that one more cameo went un-ingested. `fpl doctor --providers` reports the same rate as an `Understat name join` row, which is where to look for it without turning on debug logging. Understat serves apostrophes HTML-escaped (`O&#039;Shea`), which used to fail every apostrophe name silently; the payload is now decoded when it is fetched.

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

Output: free transfers, bank balance, squad sell prices, total team value. Data cached to `team_finances.json` in the data directory (see [Directories](#directories)) until the next `--refresh`.

**Price changes since the scrape:** prices move overnight, so a cached run fetches today's
prices and recomputes each sell price from its purchase price: keep half of any rise,
rounded down, and take all of any fall. It applies the rule to a player only once it has
checked it against FPL's own figure: the cache stores each player's price at the scrape,
and the rule must reproduce the sell price FPL showed at that price. Each sell price that
moved is named on stderr. JSON rows carry `scraped_sell_price` (FPL's figure) beside
`sell_price` (the one to budget on). A player it cannot check keeps the scraped price and
is named in a `sell_prices_as_scraped` warning, on stderr in both modes and in
`metadata.warnings` under `--format json`: a price check that fails, an unknown purchase
price, a player missing from today's list, or any player at all in a cache scraped before
prices were stored, which one `--refresh` fixes. Not being able to fetch today's prices
(the lookup waits 10 seconds) gives one such warning covering the whole squad and still
exits 0. The squad, bank and free transfers stay as scraped, so a transfer made on the
website since needs a `--refresh`. `fpl transfer-eval` uses the same recomputed sell price
and warns when the OUT player's is unchecked or missing from the cache, and
`fpl allocate --sell-prices` repeats the file's `sell_prices_as_scraped` warnings in its
own output.

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

The season segment is the hyphenated label (`2026-27`), appended automatically — you do not put it in `reports.output_dir` yourself. It is what keeps the reports apart: the filenames carry a gameweek but no season, so in a flat directory the 2026-27 GW21 report would overwrite the 2025-26 one, silently and unrecoverably.

`review`, `league-recap` and `preview` derive the label from GW1's deadline rather than the date, so a season that overruns the July cutover -- as 2019-20 did, delayed into July 2020 by COVID -- still writes to its own directory instead of colliding with the following season's ([#91](https://github.com/rossgroomio/fpl-cli/issues/91)). `fpl status`'s `metadata.season` -- the season label skills are meant to read (see below) -- derives it the same way. A caller with no bootstrap data to hand falls back to the date, on the same July cutover as the rest of the tool.

Two details worth knowing:

- **`--output` is partitioned too.** `fpl review --save --output ~/somewhere` writes to `~/somewhere/2026-27/`, not `~/somewhere/`. A scripted destination gets the same protection as a configured one.
- **Pointing a directory at the current season is harmless.** If `reports.output_dir` already ends in the current season label, it is used as-is rather than nested a second time.
- **A directory left pointing at a *past* season warns.** `output_dir: ~/fpl-reports/2025-26` in 2026-27 writes to `~/fpl-reports/2025-26/2026-27/` and says so on stderr. The nesting is deliberate: reusing the stale directory would file this season's reports under last season's name, which is the mislabelling the layout exists to prevent. Drop the season from the setting and it is appended correctly.

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

**A `--save` that did not save** prints `Failed to save report: <reason>` on stderr as soon as it fails, still runs `--scout` if it was asked for (the scout writes to its own directory, so a failed report is no reason to skip it), and then exits **1**. It used to print that line on stdout and exit 0. `Generating report...` is on stderr too; preview's other progress lines are still on stdout.

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

**League position:** your own position and the `Pos` column of Nearby Rivals are competition-ranked from the cumulative totals, the same numbering `fpl league`'s table shows and `fpl league-recap` writes to the ledger: managers level on points share a place and the next distinct total skips the places they consumed (1, 2, 2, 4). The standings API hands out strictly sequential ranks instead, splitting a tie on fewest transfers season-to-date — a figure no column here shows — so the same gameweek used to place the same tie differently depending on which report you read.

**Draft:** squad breakdown, transaction assessment, league standings, best/worst performers.

A waiver claim a rival won is reported under **Claims Lost** rather than discarded, and reaches the personal analysis alongside the moves that landed, with the priority it was made at. A gameweek whose only waiver activity was a claim you lost is not a gameweek without waiver activity, and "No waivers this week" is now said only when nothing was submitted either. A recommendation you acted on and were beaten to reads as "claimed, lost to a rival" in the recommendation comparison rather than "not executed" — naming the player you actually claimed rather than the one advised, since conditional chains share a drop and the two need not be the same, and saying so where they differ. A recommendation you claimed, lost, and then covered with a fallback still reports the attempt alongside the move that landed. A claim denied because an earlier accepted claim of yours had already dropped the nominated player is a conditional-chain cascade, not an attempt, and is not reported as one. Recommendations that share a drop are graded as that chain: the move you made is credited to the claim it filled, so a fallback that landed after the top claim lost reads as followed, and a claim below the one that landed reads as "not needed: Nyoni landed" rather than "not executed" (named by player, since the waiver and free-agent tables number their rows separately). A claim you skipped on the way to one you made is a miss. Where you left the chain, the first claim after the last one you made is the miss, or the different move when the drop went to a player the chain never named, and the claims below it read as fallbacks never reached or not needed.

**Results:** all fixtures with scores, goal scorers, assists, and bonus points.

**LLM summary** (`--summarise`): Community narrative via research provider, personal analysis via synthesis provider. The summary is an add-on: the review itself needs no key, so a role whose provider has no usable key is skipped with the reason on stderr (`Community narrative skipped: ...` / `Personal analysis skipped: ...`) and the review still prints, still saves its report and still exits **0**. The two roles resolve independently, so one key buys the half it belongs to rather than nothing. A saved report generated from such a run names each skipped half and why in a warning callout above the summary, so a section absent for want of a key never reads as one deliberately left out.

**Trimmed tables:** the community narrative's Standout Performers and Disappointments tables are
checked against the player lists the research prompt was given. A row naming a player who is not
on those lists is removed, and the table then carries `[table trimmed: N unlisted row(s)
removed]` beneath it, counted per table. "Unlisted" means not on the supplied lists, and the
guard removed the rows, not the model, so a shortened table is not the model's own choice.
Style differences (spacing, apostrophes, accents, case) are never grounds for removal. Under
`--debug`, each removed row is named in `data/debug/research_corrections.txt` as
`<player>: stripped (not in provided list)`.

**Next Week:** the personal analysis is given next gameweek's fixtures — every club's opponent
and venue, and each of your own players' FDR for their own position — so the section's start,
bench and transfer calls are grounded in what the squad actually plays rather than extrapolated
from one week of returns. FDR follows the same split as `fpl fixtures` and `fpl fdr`: the 1-7
team-ratings scale with custom analysis on, the FPL API's 1-5 difficulty with it off, and the
block carries the same scale footer those tables print, unrated-club caveat included — a
neutral 4.0 meaning "no data" reads exactly like a genuinely average fixture otherwise. Ratings
that cannot support difficulty at all are named in the block as well, rather than reaching the
analysis as figures to reason from.

Where there is no next gameweek to look at — the final gameweek of a season, or fixtures that
could not be fetched — the section is narrowed instead: fixture-independent observations only
("Tzolis has now blanked twice"), with selection and transfer calls left to `fpl gw-prep`.
Reviewing an older gameweek gets the gameweek that followed it, marked as already played so
the section reads as what that week suggested at the time rather than as a forecast.

The answer is checked against the data it was given, the way the community narrative already
is: an FDR figure the section cites that the block never printed, or any fixture claim at all
on a run with no fixture data, is reported alongside the completeness findings below — on
stderr, and as a warning callout in the saved report.

**Incomplete summaries:** the personal analysis is checked against the sections its own prompt
asked for before anything is written. A response that comes back empty, is missing a `## `
heading, ends without terminal punctuation, or that the provider cut off at its token ceiling
(`max_tokens`, or `length` from an OpenAI-compatible API) is retried **once**, with the prompt
followed by what the last draft lacked — and if the second attempt is no better, the less damaged
of the two stands and the run says so on stderr, naming what is missing. A response the provider
refused (`refusal`, or `content_filter` from an OpenAI-compatible API) is never retried, even when
it is also missing sections: re-sending a refused request to the same model usually draws another
refusal. It is reported the same way, refusal included. Any other stop the provider names (one
from a custom endpoint, say) is reported too, but earns no retry on its own: only what the text
lacks does. The retry is only ever asked for the sections found missing, never a verdict for a
format the run has no data for. A retry that fails outright, or whose answer cannot be checked,
keeps the first attempt rather than losing it. This is the same single retry `league-recap` gives
its editorial. Under `--debug` the detail lands in
`data/debug/synthesis_corrections.txt` alongside `research_corrections.txt`, and
`synthesis_prompt.txt` holds the prompt that drew the saved `synthesis_response.txt` — the
retry's, feedback included, when the retry is the one kept. `synthesis_meta.json` beside them
records how many answers were judged (`attempts`: 1, or 2 after a retry), which of them was kept
(`shipped_attempt`: a retry is kept only when it is less damaged) and the token `usage`
(`input_tokens`, `output_tokens`) of every answer judged, so one run can be read against another. A saved report
generated from such a response carries the same list as a warning callout above the summary, so
a verdict the model dropped never reads as one deliberately omitted. The callout does not depend
on there being a summary to qualify: a response that returned no text at all — the shape a hard
`max_tokens` truncation takes when the model was still thinking at the ceiling — is the one that
most needs explaining, and is reported as a single "the response is empty" finding rather than
as every section it happens to be short of. A verdict for a format the run has no squad data for is not counted as
missing — the prompt tells the model to analyse only the format it was given.

A first call that never lands at all — a timeout, a 500, a 429 that outlasted its retries, a revoked
key — is reported through the same callout, naming the failure. The run still exits **0** with
the rest of the review intact, but the saved report says the analysis was attempted and lost
rather than leaving a reader to wonder whether the template ever had one.

**Blanks and doubles:** a zero from a player whose club had no fixture is marked `[BGW]` rather
than read as a choice that failed, and such a player is kept off the Blankers list entirely; a
player whose club played twice is marked `[DGW]`. Which clubs those were is read off the
gameweek being reviewed rather than off the clubs as they stand today, so reviewing an earlier
gameweek judges a player transferred since on the fixtures he actually had at the time. FPL
writes those marks per fixture as it finishes, so a gameweek with a fixture still to complete
cannot answer; there the current clubs answer instead, which can differ from that gameweek's
clubs once a transfer has happened in between.

**Who counts as a blanker:** a high-ownership player who scored at most 2 points in a gameweek
his club played — and who has played at least a minute this season. A squad player who has never
taken the pitch is left out: his ownership is high precisely because nobody expects him to play,
so a cheap backup keeper at a big club would otherwise clear the ownership bar every week of the
season while disappointing nobody. The gate is season minutes rather than the gameweek's, so a
regular starter dropped for one week still surfaces — that is a selection worth knowing about.
Each row shows the minutes played in the gameweek, in the table, in the saved report and in the
data the narrative is written from, so a benching reads differently from a full ninety that
returned nothing.

**A gameweek it will not review** — one still being played, an id the season does not have,
a season with nothing finished yet — is refused with the reason on **stderr** and exit **1**.
`review` has no `--format json`, so stdout carries the review or nothing at all, and `fpl review
2>/dev/null` is silent on a refusal rather than printing half an explanation. It used to exit 0
with the reason on stdout, as did `league-recap`, which shares the resolver. A missing provider
key was refused the same way until it stopped being a refusal at all — see the LLM summary above.

**Notices** — the line that says an entry id is not configured or not found in the draft league,
the pair that says why a past gameweek has no league table, the note that a new mini-league's
standings are not published yet, and the `Fetching your team data...` progress line — go to
**stderr**, for classic and draft alike. A section heading stays on stdout only with the content it
introduces: for a past gameweek, or a mini-league whose table is not published yet, there is no
League section on stdout at all and stderr says why.

The same holds for the global sections and the extras: `Dream team not available` and a failed
dream-team fetch or blankers calculation go to stderr, and the `### Dream Team` / `### Blankers`
heading is left off stdout when there is nothing under it. `No high-ownership blankers this GW` is
the Blankers section's answer, so it stays on stdout. `Generating report...` and every progress
line `--summarise` prints (including the `--debug` and `--dry-run` file notes) are on stderr too.
`Report saved to:` stays on stdout for now.

**A `--save` that did not save** exits **1** with `Failed to save report: <reason>` on stderr. It
used to print that line on stdout and exit 0, so a script checking the exit code could not tell.

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

**A `--save` that did not save** exits **1** with `Failed to save report: <reason>` on stderr,
the same as `review` and `preview`. Under `--format json` stdout is the `{command, error}`
envelope instead of the recap, and the message names the codes of any warnings the ledger capture
raised (`... (the ledger capture also reported: league_history_store_unreadable)`), since the
payload that would have carried them is not emitted. When `--summarise` produced an editorial, it is printed on stderr first, since the report was its only destination. The capture itself runs before the report is
written, so the failure does not undo it.

**When to run it:** any time after the gameweek has finished, but the fullest capture is the
window between its last whistle and the next deadline. Only in that window does the league
table still describe the gameweek being recapped, so only there can the recap cross-check
its own headline numbers against the table and fill a manager it could not fetch from the
table's row. Once the next gameweek's deadline passes the table describes *that* gameweek,
and the recap takes the same replay path an explicitly older `-g` takes: every figure comes
from each manager's own gameweek history, positions and cumulative totals are derived
rather than read off the table (for draft, summed from the ledger), and a manager the replay
cannot answer for — their fetch failed, or it came back without their gameweek history — is
recorded unknown rather than filled in from the table. The run says so on stderr and raises
a `league_standings_moved_on` warning under `--format json`. The gameweek is recorded
either way — an in-progress gameweek is never recappable, so the recap of the one before it
has to keep working while the next one plays.

The start of that window has a catch of its own. FPL marks a gameweek finished once bonus is
confirmed, but refreshes the league tables afterwards, and the refresh can trail by hours, so a
recap run straight after the last match may read a table that has not caught up: classic's
totals, draft's ranks. Before a live capture collects, the recap reads the recapped game's
processing feed (each game publishes its own, and they refresh separately) and, when the
feed's own match days for the recapped gameweek show bonus unconfirmed or the tables still
"Updating", says on stderr that positions and totals may be stale and to re-run before the
next deadline, with a `league_tables_updating` warning under `--format json` and a warning
callout at the top of a saved report, above any editorial. It warns rather than refuses: the
refresh can sit on "Updating" for hours, a draft gameweek missed while live cannot be
recaptured, and the later run supersedes this one in the ledger. Only a definite signal
warns: a feed that cannot be read within five seconds, has been reshaped, reports a state
other than "Updated" or "Updating" (the empty value seen between gameweeks among them) or
holds no day for the recapped gameweek says nothing, and a replay does not read the feed at
all, since by then it has moved on.

`fpl status` reports the same two signals for the current gameweek, beside "Finished" and as
`bonus_confirmed` / `league_tables_updated` in `data.gameweek_info`, so a script can wait on
`league_tables_updated` before recapping. Each is `true` only when the feed of every game
shown says so (the classic feed alone when no format is configured), `false` as soon as one
says otherwise, and `null` otherwise: while the gameweek is still in progress (when the feed
can report the days played so far as settled), and whenever some game's feed could not
answer and none said no, so `null` with both formats configured can mean one game confirmed
and the other could not be read.

**Report title:** a saved recap opens with `# Gameweek 4 Recap: <league name>` — the gameweek
spelled out and the league's name exactly as configured — whether or not it carries an editorial,
so the first line of every recap reads the same shape every week and in both formats
([#349](https://github.com/rossgroomio/fpl-cli/issues/349)). The title is written by the report,
never by the model: with `--summarise` the editorial is asked for one `##` headline beneath it — a
hook for the gameweek, never the gameweek number, the word "recap" or the league's name — and
every heading it writes anyway is held to that shape rather than trusted, wherever it sits. One
that only restates the title (`# GW4 Recap: <league name>`, in any order, spelling or emphasis)
is dropped, the opening one is kept as the `##` headline with the title's parts removed, a later
`#` heading is demoted, and a fenced code block is left alone. Only a title-shaped part joined by
a colon, pipe or spaced dash is removed — a gameweek callback inside a hook ("Bob Never Learns -
Gameweek 7") stays. The metadata lines sit between the title and the headline, so the editorial —
headline and prose — is one contiguous block to copy out.

**Awards:** GW winner/loser, biggest bench haul, best/worst captain, transfer/waiver genius and disaster.
Biggest Bench Haul ranks on what the bench actually cost: how many more points the best legal XI
from the same 15 would have scored than the XI that counted, with the player whose points were
doubled (the captain, or the vice when the captain did not play) held in place so the multiplier is
never quietly reassigned — not on the bench's raw points sum, which counts a keeper who could not
have played alongside the one who did and outfielders no legal formation had room for, and so
overstated every week and could hand the award to the wrong manager (issue #350). A manager with
points on the bench but no better XI is not a candidate, and the detail names the swap rather than
the bench ("Groß (17) and Gvardiol (11) benched while Rice (3) and Colwill (2) played"). Ties go
the way of the XI that played: a bench player level with a starter is never named, and of two
formations level on points the one that changes fewer players is the one described. The ledger row
and `--format json` keep `bench_points` as the raw sum — the API's own `points_on_bench`, the one
figure the coarse tier can also record — and the gain is re-derived from `squad` rather than stored.
The draft waiver awards count and label waiver claims and free-agent signings separately (e.g. "across
2 moves: 1 waiver, 1 free agent") rather than folding both into "waivers". A draft recap has a third
waiver award, **Most Contested**, on a week where at least three managers claimed the same player:
the player the most managers claimed, with who won him and every manager beaten to him at the
priority they gave the claim ("Elanga was claimed by 4 managers: Dan won him; Alice (priority 1),
Bob (priority 1) and Cam (priority 2) were beaten to him"). A two-way race is every lost claim by
definition, so it earns no award — the editorial still sees it (below). A tie on claimants names every
player tied, capped like the other awards' ties. Someone has to have won him: a race whose winner's
move the recap cannot see — their picks fetch failed, or the row could not be placed — never counts
towards the award, and reaches the editorial with its winner unidentified and only the managers the
recap can see counted, rather than being dropped. Races are derived from the lost claims the recap already records,
grouped by player, with the winner read off the accepted move that brought him in (the raw move, so a
winner who moved him on again the same gameweek still won him); a manager who lost several claims on
one player — a conditional chain offering different drops for him — is named once, at the highest
priority they gave him, and the winner is never also among the beaten, however many of their later
claims for him were denied once the first landed. A beaten manager's priority is their own ranking of the claims they submitted
that week, not the league's waiver order. Nothing about races reaches `--format json`, whose rows
already carry the `lost_claims` and `transactions` they are regrouped from.
A tied award records every manager who tied but bounds the prose it prints them in: at most three
picks, at most three managers named under each, and the rest counted rather than listed, so the line
is the same length whether five managers tied or fifty. A captain tie the whole league is in is not
an award at all — it collapses to one line counting each pick and naming no manager ("Captaincy was
a wash — all 19 captains scored 2 pts (Haaland ×14, Salah ×5)"), and where Best and Worst Captain
cover the same managers only Best is reported: the same fact under two headings is printed once.

**Standings movement:** position changes derived from point differentials, per-manager highlights. Both tables — this gameweek's and the one before it — are ranked the same way, so managers level on points share a place on each and no arrow is reported for a tie nobody left.

**Standout players:** the week's top scorers and costliest blanks among the players in the
league's squads, each with the managers he counted for
([#455](https://github.com/rossgroomio/fpl-cli/issues/455)). Nothing extra is fetched: the section
reads the same squads and live points the Biggest Bench award does, so a figure here cannot
disagree with an award. Points are the player's own, before any captaincy. The top scorers are the
five highest-scoring players in any manager's squad, a player left on a bench included ("Groß
(Brighton) scored 17 points, but Alice left him on the bench."), ties going to the one who counted
for more managers; the blanks are up to three players who counted for someone and scored 2 points
or fewer. In classic the blank that counted for the most managers comes first, then the lowest. In
draft a player has one owner, so that would leave only the lowest scores, which are squad fillers
nobody drafted with any hope; draft blanks are ordered instead by what the manager could expect of
the player. Before GW19 that is FPL's pre-season draft rank, best first; the draft game sets it
before the season, on last season's points and its view of each player's potential, and never
updates it, so from GW19 the season's points per game takes over. The ordering is the heading's to
say ("Blanks among the best-ranked players, by FPL's pre-season player ranking", or "Blanks among
this season's best performers, by points per game") and the JSON's, never the line's: a draft blank
reads like a classic one ("B.Fernandes (Manchester United) managed 1 point for Alice."). Given the
rank on the line, the editorial wrote it up as this league's "number 2 draft pick", though the rank
is one list shared by every draft league, which is also why the heading never calls it a draft. A
draft blanks list only holds players its ordering can rank: a
player with no draft rank, or from GW19 one with fewer than 5 appearances behind his average, is
left off, so the list can run short of three rather than put a two-game cameo's average under a
heading that calls it the best. The average is today's, which on the recap of the gameweek just
finished runs exactly to it; a replay (an older `-g`, or a run after the next deadline) would count
the gameweeks since, so it keeps the draft rank all season. On GW6 of 2026-27 the lowest scores were Bentancur, Struijk
and Aina; the best-ranked blanks were Fernandes, Saka and Gabriel. A blank under the hour says how
long he played ("managed 1 point in 27 minutes"), and one who did not play at all says so, since a
starter nobody could cover is the manager's story. A player whose club had no fixture is no blank,
a starter who never played and was replaced from the bench counted for nobody, and a draft pick the
main game could not be matched to is left out, since its zero is not a score. Each line names the managers
where the list is short enough to be the story and counts them where it is not: "for Dan and Eve",
"for 9 of the 12 managers, everyone but Alice, Bob and Cam", "for 6 of the 12 managers", so a
template player in a big league is never a roll call. Two managers sharing a display name are counted
rather than named twice. "All" and "everyone but" are said only of the whole league: where the
standings were cut short or a manager's picks could not be fetched, a line counts only the managers
it can see ("for 9 managers"). Where the cap cuts through players level on
the last one's score, a closing line counts them ("2 more players also scored 9 points."); draft
blanks are ordered by rank or average rather than counted level, so they never close on one. The
same sentences print on the console under `Standout Players:` and, with `--summarise`, in a
`## Standout Players` section of the editorial's prompt, which carries one line saying what the
figures are and no rule of its own. The saved report never carries the section
([#482](https://github.com/rossgroomio/fpl-cli/issues/482)): it is material for the editorial to
weave into its prose, and the report is shared as it stands, so a second, ledger-shaped telling of
the same week would only repeat it. Read the section back from the JSON rows or the prompt, never
the report. Every line states this gameweek's facts only, so the system prompt needs no rule about
the section. The command builds it for the gameweek it recaps, never for a backfill's replays, and as
an addition nothing else reads: a squad row it cannot read skips the section with a note on stderr
rather than costing the recap, the report or the ledger capture. Nothing of it reaches the ledger.

*How a draft player came to his manager* ([#458](https://github.com/rossgroomio/fpl-cli/issues/458)):
every draft line says how the player came to the manager it names, as a clause on the
manager's name, the latest way he joined that squad as of the recapped gameweek: "Haaland
(Manchester City) scored 17 points for Alice, who drafted him in the first round.", "...for Bob,
who picked him up on waivers in GW4.", "...for Cam, who signed him as a free agent in GW9.",
"...for Dan, who acquired him from Alice in a GW7 trade." A blank reads the same way ("B.Fernandes
(Manchester United) managed 1 point for Dan, who drafted him in the first round."), a player left
on the bench names his owner where it says so ("but Bob, who signed him as a free agent in GW9,
left him on the bench"), and a pick the draft's auto-pick made says "who had him as an auto-pick".
A pick dropped and signed again by the same manager is the signing. Where the league has drafted
more than once the line names the draft ("in the first round of the GW20 redraft"), and a player
kept through a redraft is drafted again under it. The clause is a relative clause, not "her
first-round pick", because the data holds no one's gender; a trade names the manager he came from
unless another manager shares that name. The section's note line says what the clause is, and
the system prompt is unchanged. A `-g` replay shows the owner and acquisition of its own week.

The history is replayed, not read off the API: the draft's picks, then every accepted waiver and
free-agent move by its `added` time and every processed trade (`state` `p`; `v` is vetoed and
changes nothing) by its `response_time`, in one time order (`services/draft_provenance.py`).
The replay is trusted only if it agrees twice: its whole history must end exactly where FPL's own
ownership list (`element_status`) says everyone is now, and, stopped at the recapped gameweek, it
must put every fetched manager's players in the squad that gameweek recorded. Any disagreement, a
move that does not fit the squad it applies to, or a log that cannot be read (the trades included:
a trade sent away and sent back leaves no mark on either check) labels nobody: the lines read as
before and a
`draft_provenance_unavailable` warning names why. The picks come from the choices feed first and,
when it has none for the draft in force (it serves a league's latest draft only, so a scheduled
redraft empties it), from the snapshot `league-recap --draft` kept ([Draft pick
snapshots](#league-history)).

With neither, the picks are lost, but who drafted whom is not: each manager's squad at the draft's
gameweek is the set they drafted, corrected by undoing the moves made before that gameweek (a
player who arrived in an accepted transaction or a processed trade that gameweek was not drafted by
that manager, and the player who left in the same move was). The replay runs on from there and
faces the same two checks, and the lines say "who drafted him" with no round ("...in the GW20
redraft" after a redraft). Rounds return on their own once a snapshot of the draft exists. This
tier is stricter about its logs, because a start derived from them cannot reveal a move they are
missing: a trade made since the draft, before the gameweek, in a state nobody has seen move
players, a squad that could not be read, or an undone move that does not fit the squads all
refuse it. It costs one request per manager (their squad at the draft's gameweek), only when
the picks are lost and only once: a run whose replay passes keeps those squads in the cache dir
(`draft_rebuilds/`), and later runs, `fpl player` and `fpl stats` included, rebuild from that copy
under the same checks. A copy whose squads do not fit the moves undone is dropped, so the run after
reads the squads again; any other refusal leaves it. It is
reported as a `draft_rounds_unknown` notice on stderr and in
`metadata.warnings`, saying where a copy of the draft's choices response would restore the rounds,
not as `draft_provenance_unavailable`. With neither the picks nor squads that can be rebuilt from,
the warning says so and the lines read as before.

Verified on public leagues: the replay reproduced FPL's ownership in every league whose picks were
served, and each manager's squad for earlier gameweeks; the rebuild did the same on every league
tried whose feed returned no picks for a held draft. Costs one request more per draft recap (the
trades): the choices response the replay reads is the one the picks snapshot saves. Under `--format json` each standout row of a draft recap carries
`acquisition`: `null` where it could not be established, else `kind` (`draft`, `waiver`,
`free_agent`, `trade`), `gameweek` (the one the move first applied to), `draft_id`,
`draft_gameweek`, `redraft`, `round_unknown`, `round`, `pick`, `overall_pick`, `auto_pick` and
`drafter` (a pick only, otherwise null) and `from_manager` (a trade only), each manager as
`{manager_key, manager_name}`. `round_unknown` is true for a pick whose draft's picks are lost,
and `round`, `pick`, `overall_pick` and `auto_pick` are then null. Classic rows carry no
`acquisition`.

**Fines:** evaluates fines for every manager (not just you) when configured and records the
ruling against the gameweek. A backfilled gameweek is ruled too — the detailed replay rules every configured rule, the coarse tier rules the ones
derivable from cohort points (`last-place`, `below-threshold`) and records that it could not
rule `red-card`, which needs a squad the manager-history endpoint does not return. See
[Season Fines](#season-fines) for the table this builds up. On the console and in the saved
report, managers who took the same fine (same rule, same red-carded player, same penalty) share
one line led by the fine and naming every one of them, the penalty as configured. Shared lines
come first, biggest first and then in the order the rules are configured; a fine only one manager
took follows, reading `Manager: fine. penalty` as before. Two managers sharing a display name keep
a line each, since one line could not tell them apart. The ledger and `--format json` still hold
one record per manager per fine.

**Season fines:** the season-to-date *table* appears on console and in the saved report only
at the two milestone gameweeks — GW19 (the chip-availability boundary, which is also the
halfway point) and the finale. Every other week those surfaces stay a this-week view, and
[`fpl league-fines`](#season-fines) answers the season question on demand.

The editorial is deliberately not gated with them. A table and a sentence are different
things: with `--summarise`, the narrative gets the season totals every week and may drop one
into a paragraph where it sharpens what happened ("Bob's fourth last-place of the season"),
taking the numbers verbatim and repeating any qualification the tally carries. It is offered
as optional colour, never a required beat, so a gameweek the total adds nothing to simply
goes unmentioned. Each total carries its breakdown but not the gameweeks behind it (`Bob: 2
(2 last-place)`), since the editorial copied that list whole and by the run-in it would run to
a dozen gameweeks, and this gameweek's own fines are handed over with their place in the season
already worked out — "this gameweek's last-place fine is Bob's first of the season" — so the
narrative never has to count fines itself to say whether one is a repeat. That ordinal is
only stated where every earlier gameweek actually ruled that rule against that manager;
where the span holds a gap that could hide an earlier fine — one never captured, one at a
fidelity that could not rule it, or a season that began before the ledger did — the line
names the gap and forbids numbering the fine instead. `--format json` is
ungated too — `metadata.season_fines` is emitted every
week, so a scripted consumer never sees it appear and disappear on a calendar it cannot see.

**Naming the forfeit:** the Fines list prints each fine's configured `penalty` exactly,
and that list is unchanged, but the editorial may write a forfeit out word for word only
once, headline included. Everywhere else it names the same forfeit in other words, without
changing what it is (no more of it, a different drink or forfeit, or a part of it dropped). Give
the league its own vocabulary with `penalty_phrasings` (see [Fine Rule Types](#fine-rule-types))
and the editorial varies between those phrasings and uses no others; without it, the model
words the forfeit itself. An editorial that still writes a forfeit of two or more words out
word for word more than once is retried, then shipped with a `synthesis_penalty_repeated`
warning. A one-word forfeit is not counted, since it cannot be told apart from the same word
used for something else.

**LLM editorial** (`--summarise`): Newsletter-style narrative via synthesis provider. Names names, calls out decisions. Captain picks, chips and transfers each reach it as a full per-manager roster with an explicit count, never through the awards alone — the awards name only the best and worst, so a manager whose pick or move was neither would otherwise be invented or miscounted. The transfer roster carries every move with its points swing, the hit and the post-hit net, names the managers who made none, and lists a manager whose moves could not be fetched as having moved with the moves unknown rather than as standing still. A draft recap gets the same roster for its waivers and free-agent signings: every move as it was made (a rebuild chain is listed move by move, not as the endpoint pair the award prints), each tagged `[waiver]` or `[free agent]` with the labels the waiver awards use, the net per manager, the count of movers, who claimed a player and lost him to a rival — with the priority they spent on him — and who did neither, then every contested player with who won him and who was beaten to him — every race, the two-way ones the Most Contested award never headlines included, in the sentence that award prints (the one thing it may say of anyone's waiver position is that the winner stood higher in it) — so the editorial can say who else wanted a player and no longer sees only the Waiver Genius and Waiver Disaster and is no longer told to say nothing about moves because draft has no `## Transfers` section. The editorial is an add-on: if the synthesis provider has no usable API key the recap still renders, still saves its report and still captures the ledger, with the reason on stderr and a `synthesis_provider_unavailable` warning in JSON. `synthesis_summary` is `null` on such a run — the warning is what distinguishes it from a run that never asked for an editorial. An editorial the provider cut off at its token ceiling (`max_tokens`, or `length` from an OpenAI-compatible API) is regenerated once, told the last draft was cut off (the same single retry the checks below use, so a draft both cut off and wrong is told both), even when the checks find nothing wrong in it, since it may have stopped before the claims they read. A refusal or content filter is never regenerated, even alongside a wrong claim: re-sending a refused request to the same model usually draws another refusal. Whatever the reason, an editorial the provider stopped early on that is still the one kept is written out as it stands, with the reason on stderr, a `synthesis_stopped_early` warning in JSON, and a line in the warning callout above the editorial in the saved report (a refused one is named as refused there, not as cut off) — a truncated editorial is otherwise indistinguishable from a whole one, and the report is what gets read weeks later (issue #372). A draft editorial is also checked against the contested races it was given: wherever it says a manager was beaten to a contested player ("beaten to", "missed out on", "lost out on", "lost the race for", "pipped to", "was beat by … to") or beat a rival to him ("beat", "pipped", "edged out", "outbid"), that manager must hold that role on the player's line. The voice comes from the sentence — an auxiliary ("was", "got") or a "by" makes "pipped" and "beat" passive — and a list of managers set against a list of players is read position by position with "respectively" and as every pairing without it. The prompt forbids folding two races into one clause, because that is how a race's winner was once named as beaten in the next (issue #357). An editorial that fails the check is regenerated once, with the wrong claims and the lines they contradict appended to the prompt; the less damaged attempt is kept (a complete one over a cut-off one, then fewer wrong claims), and a retry that fails outright keeps the first attempt. If the kept editorial still fails, the wrong claims are named on stderr, in a `synthesis_contested_misattribution` warning in JSON, and in a warning callout above the editorial in the saved report. The check reads only clauses it can pin to roles — a race verb joined to the player by race phrasing ("to", "for", "on", "the race for") — so a negation, a possessive ("King's bonus"), a points swing ("lost 3 points on King") or a phrasing it does not recognise passes unchecked rather than raising a false alarm. Every editorial, classic or draft, is checked the same way for two more kinds of claim its data contradicts (issue #359). A roster line carries the manager's net overall beside each move's own swing, and a net figure ("-6 net", "net of -6") said of one named move — both its players in the clause — that is not that move's swing but another figure on the same line (the net overall, the swing before the hit, or another move's swing) is flagged, unless the clause makes it the manager's: a word that marks it ("overall", "across both", "his transfers"), the week or the hit beside the figure ("ended the week -6 net", "-6 net after the hit"), the manager named nearer before the figure than the move ("Gibbs-White for Wirtz went -4, and Alice was -6 net"), or the move's own swing stated in the same clause. And a tie word fused to a rank ("joint-lowest", "tied for last", "joint-seventh") is held to who holds that rank: a gameweek score is ranked on the GW Pts column, a league position on the Pos, Prev and Total columns (a head-to-head draft table places managers level on league points apart by points scored, and the Total column still shows them level), and a phrase that names neither (or both) passes if either reading does. A highest or lowest score said of the season ("the joint-highest score of the season") has a third reading, the season's record from the League History section, which holds where more than one score reached it, so a faithfully quoted shared record is never flagged (issue #494), unless the tie is said of a figure that is not the record ("Bob's 65 was the joint-highest score of the season" beside a 72 scored twice). To give that rule a line to point at, the prompt's GW Standings section now names the week's highest and lowest scores, says whether each was one manager's alone, states each one's gap to the nearest score and who holds it, and who avoided last place on the week by the bottom gap (issue #484, so "won by three clear" is quoted rather than worked out from a table in league order; it states no gap between two managers' season totals, and the rule says so, so "five behind the leader" is never the editorial's own subtraction from the Total column, issue #493), and lists every gameweek score and Pos position more than one manager holds, any shared Prev position, and any season totals the league's tie-break placed apart, and its table carries a GW Rank column beside GW Pts (issue #483) with a note naming Pos as the league position, since the table is in league order and the editorial once read a manager's last-row Pos as their gameweek finish. That rank is the one the saved report's GW Standings block numbers its rows with, so managers level on gameweek points share it in both rather than the report numbering them apart on league position; the roster lines call the manager's net "overall", the word the Transfer Disaster award already used. Because a placing can still be read off the wrong column, every editorial is also held to the placings it gives on the week (issue #486): an ordinal, "last", "bottom" or "top" that its own stretch of the sentence makes the week's (the week named after it as its scope, as in "third on the week", "last in the gameweek" or "the third-best score of the week", or the week's before it, as in "this week's third-best"; "this week" alone only dates a placing, and "GW5" may be another week) must be the manager's gameweek rank, the one the GW Rank column and the report state, and so must one the manager's own gameweek score is attached to ("finished eighth on 31", "scored 35 for 6th place") or that a finishing verb leads into in a clause that gives that score and names no other manager ("scored 31 and finished eighth", "managed just 31, good for eighth"), read as the week's whatever was meant, unless the clause also gives the manager's season total. "Last" and "bottom" hold for anyone on the week's lowest score and "top" for anyone on its highest, and a "-best" may also count the distinct scores above it ("the second-best score" after a shared top one). The manager is the one named nearest before the placing (only the one its own stretch names, where the sentence names two before it), or, for a pronoun with nobody named before it ("it only bought him a second-place finish in the week"), the one manager the nearest earlier clause of the paragraph named. A placing beside a word for the table, a season, holding a place or a span longer than the week ("in the league", "kept top spot", "her second-best score this campaign"), one a move lands on or counts from ("up to 4th", "from 9th to 5th", "second from the bottom"), one beside a move up or down the table unless the week is named straight after it ("rocketed up five places to snatch second on the week" is the week's), one hedged, negated or escaped ("would have been second", "could have been second", "avoided last place on the week"), one near a player, another subject or another person ("his captain", "her rival took third", "the manager who came last"), one that counts ("his third win", "second last") and one in a "respectively" list are all left alone. It shares the other checks' single retry, which is told the manager's score and placing on the week and, where the placing it gave is their league position, that too, and their reporting, with a `synthesis_wrong_gameweek_placing` warning in JSON. The escape that line states is held too (issue #488): the editorial has handed it to the manager who finished last, through a pronoun in the sentence after their fine ("He escaped the wooden spoon by one cruel point"), and set it against the foot of the table for a manager bottom of the league, so a sentence that says a manager escaped, avoided, dodged, survived, was spared or "missed" last place, the bottom, the wooden spoon, the worst week or the lowest score, or is "clear of" one of them, is read for whose escape it is: the manager named before it; for a pronoun with nobody named before it, the first manager the nearest earlier clause of the paragraph named, the subject a reader takes it for even where that clause named two ("Gus Peel took the fine, a point behind Hal Penn. He escaped the wooden spoon" is Gus Peel's escape), unless that clause opened on a list of managers; or, in the passive, the manager after "by" ("Last place was narrowly avoided by him"). It is the week's unless the words naming where from name the table ("the foot of the table", "last place in the league"), and a verb for holding a place or a copula before "clear of" lets it be the table's too ("stays clear of the bottom", "is a point clear of the wooden spoon"); the week's escape is wrong when that manager's score was the week's lowest, and the table's when they are bottom of the table (read only where every manager has a position), and one read both ways only when both are. An escape that says nothing happened is left alone: hedged or negated ("nearly escaped", "no escape from last place"), a base form or an infinitive nothing says was achieved ("cannot escape", "struggled to avoid", "Did he escape?", where "enough to avoid" and "a narrow escape" are escapes), or a gerund after a preposition ("a point from escaping", "missed out on avoiding it"). So is one whose escape is what was missed ("missed the bottom-of-the-week dodge"), one said of another week or a longer span ("last week", "in GW5", "a week after he escaped", "this season"), a survival of the worst week, which reads as enduring it, and one said of another person or subject. The retry is told whose score was the lowest and who avoided last place, in the GW Standings line's own words (`last_place_escape()`, which writes both), and the warning in JSON is `synthesis_escape_misattribution`. With the check in place, the prompt's rule on quoting a gap no longer makes every sentence that uses one name its managers. A draft editorial is also held to the kind of move a figure is said of (issue #390). A waiver roster line whose moves mix kinds gives each kind's net beside the net overall ("waivers -1, free agents +4, net +3 overall"), and a figure said of one kind ("his waiver moves netted +3", "+3 from his free agents") that is neither that kind's net nor one of its moves' swings but another figure on the line — the net overall, the other kind's net, or one of its moves' swings — is flagged. Only a kind word scopes a figure: "moves", "claims", "pickups", "the waiver wire", "waivers and free agents" and "one waiver and one free agent" cover every move, a figure whose own subject is one of them ("his moves netted +3 overall with a free agent doing the heavy lifting") keeps it whatever kind follows, and a manager whose moves are all one kind is never read. The moves are those of the one manager the clause names or, for a "his" in a clause that names nobody, of the one manager the nearest earlier clause in the paragraph named; a clause naming two managers, or following one that did, is left alone. "Netted" and "nets" count as net figures in both figure checks only with a signed figure ("netted +3"): unsigned, "Isak netted 2" is goals and "Gibbs-White netted 6 points" is the player's score. These checks share the contested check's single retry (told only about the kinds of claim that failed) and its reporting — stderr, a `synthesis_net_misattribution`, `synthesis_kind_net_misattribution` or `synthesis_unsupported_tie` warning in JSON, and a line in the report's callout — and each reads only clauses it can pin down: a clause naming another manager, two of one manager's moves, or a figure that is nobody's on the line passes, as does a tie about a captain, a bench or a player, a negated one, a word that is not a rank where it stands ("tied last week", "shared first blood"), or a numbered rank among gameweek scores. A draft editorial is also held to not reciting a contested race as a list (issue #459): a paragraph that names the race's player and three or more of its beaten managers (by full name, or a first or last name no other manager and no player shares; the winner never counts, two is not a roll call, and the same names spread one per paragraph are not one) fails the check. Paragraphs are separated by blank lines and a heading is a paragraph of its own; a paragraph that never names the player is not read, however many of the race's losers it names. Unlike every other check it fires on a true sentence, so it counts known names and never judges tone, and a beaten manager named in that paragraph for an unrelated reason counts all the same. Its retry, its stderr line and the report's callout say the draft broke its brief rather than that it contradicted the data, and the retry is told to name at most two of the managers beaten to any one player, and only where the loss is their story. It shares the single retry and the reporting of the checks above (a `synthesis_contested_roll_call` warning in JSON, with `synthesis_attempts` showing the retry). Which draft ships ranks a complete one over a cut-off one, then fewer claims the data contradicts, then fewer roll calls, so a false claim is never the price of clearing a roll call, and a retry that fixes one still ships if it rolls the call. Classic has no contested races, so the check finds nothing there. The checks are advisory: one that raises is skipped with a note on stderr and the editorial saved unchecked, and each warning's message gives one sentence per claim.

**Streaks:** notable open streaks print under `Streaks:` on console — leaders only, so console stays a highlights view — and in full as a `# League History` section in the saved report. Each is reported as an observed count over its true span (e.g. "3 in the last 11, with 8 not recorded") rather than a bare "in a row" once any gameweek in it did not count towards the run. "Not recorded" is kept for gameweeks the history genuinely lacks; a fully recorded gameweek the streak had nothing to judge in is named for what it was instead — "with no completed moves in 1" for a draft manager whose gameweek moved nobody, whether they made no claims or lost every one, "with no captain fixture in 1" for a captain whose club did not play — and leaves the run open without breaking it. A streak surfaces once its run reaches the condition's own minimum: 2 gameweeks for weeks on top, gameweek wins, last-place finishes, captain blanks and transfer hits; 3 for waiver hauls and backfires. Bottom-half gameweeks and green-arrow droughts never surface as streaks at all — both restate where the table already shows a manager is, so their run exists to drive the season count's firing rule rather than to be read on its own.

**Prior seasons:** on the league's opening gameweek — GW1, or the league's own
`start_event` for one created later — the recap fetches each manager's FPL history
(`entry/{id}/history/`, one request per manager, made on that gameweek only and only when
something will read it — `--save`, `--output` or `--summarise`; a league created after GW1
already fetches the same payload on that gameweek to rescope its totals, and the two share
it) and writes a `# Prior Seasons` section into the saved report: one line per manager with the
seasons played, the span with any seasons sat out, last season's points, rank and top-%
finish, and the best season on record. With `--summarise` the same lines reach the
editorial as a locked `## Prior Seasons` section with an explicit count, and the system
prompt pins any claim about a manager's earlier seasons to it. Two things it deliberately
will not say. Tenure is FPL-wide — "their 11th season of FPL", never "in this league" —
because classic entry and league IDs are reissued every season, so neither the API nor
the ledger can say when anyone joined this league. And "last season" is only said of the
season that actually just finished; a manager whose most recent season is older is
described as having sat last season out. Tenure counts seasons actually played (the
history's own list) rather than the entry's `years_active`, which disagrees with it. A
manager with an empty history is named as having no prior seasons on record — the API's
own answer, not a gap — and one whose fetch failed is named as unfetched, so neither
surface silently omits anyone. Draft has no per-manager history endpoint, so a draft
recap has no such section. Nothing is written to the ledger and the `--format json`
rows are unchanged: the record is the manager's, outside this league, and it does not
change between gameweeks.

**Replayed gameweeks keep who a player was:** a backfill resolves every pick against today's bootstrap, so a player transferred or renamed since would otherwise land on a past gameweek's row wearing his current club and current name. The recorded name and position are kept instead — and the club too, except where the gameweek's own fixtures derive one (below), which supersedes the record rather than yielding to it. A code the gameweek already resolved is never dropped just because the live lookup has stopped finding him. Everything the gameweek itself can answer — points, cards, the pick flags, whether his club had a fixture — is re-derived by the replay as normal, so a repair still improves what it can. The reading goes back to the *earliest* row recorded for that gameweek rather than the newest, so a gameweek an older version already restamped is repaired rather than having the restamp carried forward. A gameweek with nothing recorded yet — a first capture, or a coarse tier being upgraded, which stores no squad — has nothing to keep, and there the **club** is derived from the gameweek instead (below); the name and position are not derivable that way and stay as today's bootstrap gives them.

**A past gameweek's clubs, derived from the gameweek:** the two cases above with nothing to keep are the documented workflow — a mid-season first run captures the whole season at the coarse tier, and `--backfill-detail` then upgrades every gameweek — so without this a whole season of rows would carry today's club for every player transferred since. FPL's live data records, per player, which of that gameweek's fixtures his club played, and a fixture names its two clubs, so a player's club that week is what his own fixtures have in common: a double gameweek narrows to exactly one club and settles it outright, while a single fixture leaves two and cannot say which side he was on. Today's club being *neither* of those two is proof that he has moved, and only then — a handful of players a season, not fifteen per manager per gameweek — is his own season history worth a request to place him exactly. Today's club being *one* of the two is left unanswered rather than read as "he has not moved": a player who moved between those exact two clubs looks identical, and the wrong answer would equal today's club and so could never be flagged. The club already recorded for that gameweek is better evidence than the assumption, so it stands. The season history answers for every gameweek at once, so a whole-season backfill pays for each moved player once rather than once per gameweek. A club that blanked has no fixture to intersect and no answer; there the recorded club, then today's, still stands. A club derived this way — exactly, by one of those two routes — *supersedes* one already recorded, unlike the name and position above: a row written by an earlier first capture or coarse upgrade holds today's club as fact, and carrying that forward would preserve the mistake permanently. That holds even where the derived club matches today's, which is what repairs a player who left and came back. `league_history_club_rederived` says how many were replaced.

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

**Season records:** with `--summarise`, the editorial's League History section also states the league's highest and lowest gameweek score this season, with every manager and gameweek that reached each — "this gameweek" for the one being recapped, and "No score this gameweek equalled it" when the record stands from earlier — so "the week of the season" is a claim the editorial can quote rather than invent, and the system prompt allows a season superlative only where that line names it. Scores are counted the way the recap's standings count the week: gross, or net of transfer hits with `use_net_points`. Only a score the ledger holds counts; a gameweek with a manager whose capture failed, one holding no row for a manager in the league this gameweek, one never captured, and, with `use_net_points`, one holding a classic row that never recorded its hit is named beside the record ("not every score was recorded in GW2"), since a higher or lower score may be missing. A manager who has since left the league is judged only where they have rows. The line says how many times each record was scored, and the tie check reads a record scored more than once as shared. Nothing is stated on the league's first gameweek, where both records would just be that week's highest and lowest. The lines are not printed in the console or the saved report; `--format json` carries them as `metadata.notes_pack.season_record_entries`, each with `points` and `holders` (`{manager_key, manager_name, gameweek}`) beside the usual entry fields, `held_count` counting the gameweeks the qualifier names.

**Unavailable:** a manager whose position or points total can't be derived this run (e.g. a replayed draft gameweek with no earlier rows) is named under `Unavailable:` on console and in the report rather than silently dropped from Standings Movement.

**JSON:** `--format json` emits one row per manager — the same shape written to the
ledger, built from the rows this run assembled, so manager data is present even when the
store could not be written. `metadata` carries `coverage` (per gameweek: fidelity-tier
counts, unknown managers, whether the file was readable, and on draft how many managers'
rows still record nothing about lost waiver claims (`claims_unrecorded_count`) — every
gameweek with a file on disk appears there with a status, a damaged one as
`readable: false`, so an empty list means only that nothing has been captured yet), `season_phase`, `notes_pack`
(every entry, including those below their reporting minimum and every nonzero season
count whether or not it grew this gameweek, plus the season's highest and lowest gameweek
score under `season_record_entries`, see [Season records](#league-recap)), `season_fines` (the whole
season tally, emitted every week regardless of the milestone gate the printed surfaces use), `standout_players`
(`managers` and `whole_league`, the `top_scorers` and `blanks` lists, `top_scorers_omitted` /
`blanks_omitted` counting the players the cap left out level with the last one listed, and
`blanks_order` (`managers`, `draft_rank` or `points_per_game`); each player row carries the main
game's `id` (a draft recap's too) with `code` beside it, `name`, `team`, `team_name`, `position`,
`points`, `minutes`, `red_cards`, `counted_for` and `benched_by` as `{manager_key, manager_name}`
lists, `text` (the sentence the other surfaces print), and the draft-only `draft_rank` and
`points_per_game`, which are `null` in classic; the section is `null` when it could not be built),
`synthesis_summary` (with
`--summarise`, as it reaches the saved report — the model's heading already demoted beneath the
report's title or dropped, see [Report title](#league-recap)), `synthesis_attempts` (how many answers were
judged: 1, or 2 after a retry), `synthesis_shipped_attempt` (which of them shipped — a retry only
when it ranks better, so 2 judged can still ship the first) and `synthesis_usage` (`input_tokens`
and `output_tokens` summed over every answer judged, so a retry's cost is counted) — all three
`null` whenever no editorial came back, whether it was not asked for, the run was `--dry-run`, or
the call failed — `warnings`, `first_capture_store_path` — always present, carrying the
partition directory on its first capture and `null` on every run after that — and
`draft_picks_saved`, always present, listing the draft-picks snapshots this run wrote (see
[League history](#league-history); empty on classic and on any run that saved none). Warning
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

A draft recap also keeps each held draft's picks in the ledger, at
`<data dir>/league_history/<season>/draft-<league id>/drafts/draft-<draft id>.json`. The
draft API's `draft/<league id>/choices` endpoint serves only the league's latest draft: a
draft scheduled but not yet held returns no picks, and there is no route back to an
earlier one, so the moment a redraft is scheduled the previous draft's picks are gone from
the API. Every `--draft` run, a `-g` replay included, checks each draft the league details
(`league.drafts`) mark completed:

- Still served and not yet saved: the endpoint's response is written as served, with that
  draft's `league.drafts` entry added under `league_draft`. The path is printed on stderr,
  and under `--format json` listed in `metadata.draft_picks_saved`.
- Still served and already saved: nothing is written. The two are compared on the picks
  alone (`draft`, `index`, `round`, `pick`, `entry`, `element`, `was_auto`,
  `choice_time`), since the response's `element_status` is every player's owner *today*
  and changes each waiver round. Different picks leave the file untouched and raise
  `league_history_draft_picks_conflict`.
- No longer served: silent if a file holds at least one of its picks. If not —
  no file, or one that is not valid JSON, has no `choices` list, or holds none of that
  draft's picks — `league_history_draft_picks_lost`, once: that run records the loss in
  `lost-draft-<draft id>.json` beside it and later runs stay quiet. Delete that file to
  hear it again; a real copy put at the snapshot path is read in preference to it.

A failed read of the draft API, or a response with no `choices` list (an error or
maintenance body), is retried by the next run (`league_history_draft_picks_unsaved`) and
never read as a draft that is no longer served. A `league.drafts` list that is missing, or
holds an entry without a numeric `id` and a `draft_completed` (a completion timestamp, `null` or true-or-false), raises
`league_history_draft_picks_unchecked` instead of being skipped quietly, since a held
draft the snapshot cannot recognise is one whose picks would be lost with no sign.

The file is the endpoint's response with one key added, so the response saved by hand
(`curl -s https://draft.premierleague.com/api/draft/<league id>/choices > draft-<draft id>.json`
into that directory) is a valid snapshot too: it is read as the same picks and kept as it
is, without `league_draft`. That is the fallback for a league whose redraft is scheduled
before a recap has run. The draft recap's Standout Players read them back once the
feed has moved on to another draft (see [How a draft player came to his
manager](#league-recap)), and they are captured every run because they cannot be fetched
later. The recap hands the snapshot the choices response it has already read, so the
endpoint is requested once per run.

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
would be indistinguishable from a manager who genuinely held their place. Nothing fills it
in afterwards either: neither the carry that stops a replay erasing recorded standings nor
the repair sweep that heals an already-damaged ledger writes that field at a first
gameweek, so a partition that still holds lines from before this rule cannot become the
source that puts one back. And a draft row leaves `transfer_cost` empty rather than zero: draft charges nothing for a squad change,
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
sharing a name cannot merge into one. Each fine also carries `players` (schema version
5): the players its message names, each with the stable cross-season code that still
identifies him after FPL renames him. A red-card fine's message spells out whatever the
player was called when it was ruled, so a gameweek replayed months later would otherwise
name him by today's name on a row whose squad records the name he actually played under.
A list names exactly those players, `[]` means the rule names nobody (`last-place` and
`below-threshold` describe a score), and `null` means nothing is recorded either way: a
row written before schema version 5, whose names are restated from its own squad the
next time the gameweek is captured or replayed. Beside it, `fine_rules_evaluated` (schema
version 4) records which rule types were actually ruled, whether or not any triggered —
without it an empty `fines` list means three different things at once (nobody was fined,
no rules were configured, no rule was ever checked), and
[Season Fines](#season-fines) would score all three as innocence. A list names exactly
the rules ruled, `[]` means nothing was configured, and `null` means nothing is recorded
either way: an unknown capture row, or a row written before schema version 4.

A draft row also carries `lost_claims` (schema version 6): the waiver claims its manager
submitted that gameweek and a rival won, each with both players, the kind of move and the
priority it was made at. They sit apart from `transactions`, which stays the list of moves
that happened — nothing moved here, so there are no points on either side and no net —
because a manager whose whole gameweek was one claim they lost has no completed move,
and a ledger holding moves alone wrote them into the permanent record as having sat the
week out. Only claims a rival won are recorded, which the feed's denial codes do not
identify on their own: a claim denied because the manager's own earlier accepted claim
had already dropped the nominated player is the cascade behind a success, and so is a
claim denied because he had just won the incoming player himself — both are read against
his whole gameweek and neither appears. A player he claimed several times against
different drops is one claim, at the best priority he spent on him (see
[Conditional chains and outcomes](fpl-rules.md#waivers)). A replay that finds fewer
claims than the gameweek recorded keeps the recorded list and says so
(`league_history_claims_carried`): a claim whose player can no longer be placed is
dropped, and a gameweek the feed has stopped serving yields none, and neither is allowed
to write an outbid manager back out of the record. A list names exactly the claims lost,
`[]` means the feed was read and held none for this manager, and `null` means nothing
is recorded either way: a classic row, whose format has no waiver wire; an unknown
capture row; or a row written before schema version 6 — which is the one state
`--backfill-detail` re-records, below.

Rows are append-only. Re-running a gameweek that has not changed writes nothing; a
re-run whose numbers differ (bonus points settled, a failed fetch repaired, a coarse
gameweek filled in) appends a superseding row and leaves the old one in place. A file
that cannot be parsed is never reset or overwritten: the run says which file and what to
do about it, still prints the recap from live data, and exits 0.

Each row carries the schema version it was written under — the version of the install
that wrote the line, whatever version the row it superseded was read at. A row from an
older version is brought up to the current shape as it is read, and the line on disk is
left as it is; a row from a newer version — an install ahead of this one, sharing a
synced data directory — is skipped with a warning and preserved untouched rather than
read wrongly or discarded.

Two fidelity tiers, both recorded on the row:

| Tier | Source | Carries |
|---|---|---|
| Coarse | Classic manager-history endpoint, one request per manager for the whole season | Points, cumulative total, transfer count and cost, bench points, team value, bank, world rank (season and gameweek), and the fines derivable from cohort points alone (`last-place`, `below-threshold`) |
| Detailed | A live recap run, or `--backfill-detail` replaying a past gameweek | Everything above plus captain, vice, full squad, transfer or waiver detail, and the waiver claims a draft manager lost to a rival |

Classic gaps fill at the coarse tier automatically. `--backfill-detail` upgrades them,
at the cost of one request per manager per gameweek, which is why it is opt-in. Draft
has no manager-history endpoint at all, so a draft gameweek that was never captured can
only be rebuilt with `--backfill-detail`, and only while the season is live. The same
flag re-records a draft gameweek captured before lost waiver claims were recorded
(schema version 6): its rows are complete in every other respect, so nothing else
touches them, and the coverage report names those gameweeks until it runs. That is
time-sensitive in the same way — the claims come from the league's transaction feed,
which nothing guarantees will serve a whole season indefinitely.

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
are stable, so scripts should key on those. Fourteen non-capture codes share the list, since
they share the channel: `synthesis_provider_unavailable`, `synthesis_stopped_early`,
`synthesis_contested_misattribution`, `synthesis_net_misattribution`,
`synthesis_kind_net_misattribution`, `synthesis_unsupported_tie`, `synthesis_wrong_gameweek_placing`,
`synthesis_escape_misattribution`, `synthesis_contested_roll_call`, `synthesis_penalty_repeated`,
`league_standings_moved_on`, `league_tables_updating`, `draft_provenance_unavailable` and `draft_rounds_unknown`.

| Code | Raised when |
|---|---|
| `league_history_league_id_missing` | No league id is configured for this format, so the gameweek was not recorded at all |
| `league_history_store_unreadable` | The gameweek's file could not be read or written; it is left untouched and the recap still renders from live data. One warning per affected gameweek, and the message names the file and the `mv` that retires it |
| `league_history_coverage` | One line per coverage gap: gameweeks missing, held at the coarse tier, holding unknown managers, or (draft) captured before lost waiver claims were recorded. An unreadable gameweek is not a gap — it is reported as `league_history_store_unreadable`, and `--backfill-detail` skips it rather than writing to a file it cannot parse |
| `league_history_unmatched_players` | A draft squad player could not be matched to a main-game player, so their recorded points are zero rather than a real score |
| `league_history_transfer_detail_short` | Fewer transfers were captured than the manager's recorded count, so the stored list is incomplete rather than empty |
| `league_history_standings_truncated` | The standings response covered only part of the league, so the gameweek is recorded for that subset only |
| `league_history_backfill_manager_unreachable` | One manager's history could not be fetched; their gameweeks stay unknown and are re-attempted next run |
| `league_history_backfill_replay_failed` | One gameweek could not be replayed in detail; the others are unaffected |
| `league_history_backfill_write_failed` | A backfilled gameweek could not be written; the rest of the backfill continues |
| `league_history_identity_carried` | A finished gameweek kept the name, club or position it already had recorded for one or more players rather than the ones today's bootstrap gives them, or restored a player reference this capture had lost. Raised by a re-capture of a finished gameweek as well as by a replay; any one of the four on its own raises it |
| `league_history_club_rederived` | A finished gameweek replaced the club it already had recorded for one or more players with the one that gameweek's own fixtures place them at. The recorded club was stamped from a bootstrap that had already moved on — a first capture or a coarse-tier upgrade has no earlier row to carry a club from — so unlike name and position it is superseded rather than kept |
| `league_history_standings_carried` | A finished gameweek kept a league position or cumulative total it already had recorded rather than the nothing this run could re-derive. Draft raises it on any replay: with no per-manager history endpoint and standings that describe a later gameweek, a replayed draft gameweek derives neither figure, and writing that out would erase the ones the live capture recorded |
| `league_history_claims_carried` | A finished draft gameweek kept the lost waiver claims it already had recorded for one or more managers rather than the fewer this run found. A replay drops a claim whose player it can no longer place, and a gameweek the league's transaction feed has stopped serving yields none at all; the recorded list is the more complete one either way, and a longer replayed list still lands as a correction |
| `league_history_tied_last_place_repaired` | A gameweek recorded its `last-place` fine against one manager of a tie, leaving the others recorded as owing nothing. The managers level on the same score now carry the same ruling — the one that gameweek wrote, penalty text and all, re-keyed to them. Nothing is re-fetched and nothing is re-ruled, so this needs neither `--backfill-detail` nor a network call, and it stops once the gameweek is repaired. It is skipped where the row data cannot settle who was level: the gameweek does not record whether it was ruled on gross or net points, so where the two measures name different managers as tied, the gameweek is left exactly as recorded |
| `league_history_tied_last_place_ambiguous` | A gameweek looks to have left a manager out of its `last-place` fine, but it does not record whether it was ruled on gross or net points and the two measures disagree about whether that manager was level. Nothing is written for them — fining a manager who was level only on the measure that did *not* rule would invent a fine rather than restore one. Raised on every run, because the state is permanent and only you can settle it: add the fine by hand if you know how that gameweek was ruled. It can appear alongside `league_history_tied_last_place_repaired` for the same gameweek, where some of the tie was settled and some was not |
| `league_history_standings_repaired` | A gameweek whose recorded positions an *earlier* run had already erased was restored from the ledger itself — the last superseded line that recorded them, or, on draft, a cumulative total re-summed from earlier gameweeks. The *last* one, not the first: a gameweek captured several times while bonus and late results were still landing recorded a stale position early and the settled one last, and only the latest recorded value is the one the damage destroyed. Nothing is re-fetched, so this needs neither `--backfill-detail` nor a network call, and it stops once the damage is repaired. The count is the managers whose league position or cumulative total actually changed — a row rewritten only to restate a fine's player names is not one of them |
| `league_history_draft_picks_unsaved` | Draft only. A held draft's picks are still served but were not saved this run: the draft API could not be read, or the file could not be written. Raised only while some held draft has no saved copy; the next run tries again |
| `league_history_draft_picks_conflict` | Draft only. A held draft's saved picks differ from the ones the draft API serves now, or the file cannot be read as a snapshot. The file is left exactly as it is, since a held draft's picks never change and only you can say which side is wrong; the message names the `mv` that retires the file if it is the wrong one |
| `league_history_draft_picks_lost` | Draft only. The league details say a draft was held, the draft API no longer serves its picks, and no usable copy exists (no file, or one that holds none of that draft's picks, which the message describes), so who picked whom in that draft is not recorded. Raised once per draft: the run that raises it records the loss in `lost-draft-<draft id>.json` beside the snapshot path, and later runs stay quiet while that file is there. The message names the path a copy of the choices response would go to |
| `league_history_draft_picks_unchecked` | Draft only. The league details carry no `league.drafts` list, or an entry in it without a numeric `id` and a `draft_completed` (a completion timestamp, `null` or true-or-false), so those drafts' picks were neither checked nor saved. Most likely the draft API changed shape; the drafts it can read are still handled |
| `synthesis_provider_unavailable` | `--summarise` was asked for but the synthesis provider had no usable key; everything else in the recap, the capture included, ran normally |
| `synthesis_stopped_early` | The provider reported that the editorial stopped for a reason other than finishing (a token ceiling, a refusal) on the attempt that shipped — a token ceiling only after one retry, a refusal never retried — so `synthesis_summary` may be cut off mid-sentence. The saved report says so in the warning callout above the editorial, naming the stop reason. For a refusal (`refusal`, `content_filter`) the message and the callout say the provider refused the editorial rather than that it may be cut off, since re-running usually draws the same refusal, and name the refusal's category when the provider gave one (Anthropic only: its `stop_details.category`, e.g. `cyber`, `general_harms`; an OpenAI-compatible `content_filter` stop carries none). Everything else in the recap is unaffected |
| `synthesis_contested_misattribution` | Draft only. The editorial still puts a manager in the wrong role of a contested waiver race after one retry — beaten to a player the race's line does not name them as beaten to, or winning one someone else won. The message quotes each wrong claim beside the contested line it contradicts. `synthesis_summary` is the editorial as written; the saved report carries the same lines in a warning callout above it |
| `synthesis_net_misattribution` | Either format. The editorial still pins a figure from a manager's roster line on the wrong move after one retry — the net overall (or the swing before the hit, a kind's net on a draft line that mixes kinds, or another move's swing) said of one named move whose own swing differs. The message quotes each figure, says whose it is, and gives the move's own swing. Saved as written, with the same lines in the report's callout |
| `synthesis_kind_net_misattribution` | Draft only. The editorial still says a figure of one kind of a manager's moves — their waivers, or their free agents — that is not that kind's own after one retry: the net overall of a line that mixes kinds, the other kind's net, or the swing on a move of the other kind. The message quotes each figure with the kind it was said of, says whose it is, and gives that kind's own net. Saved as written, with the same lines in the report's callout |
| `synthesis_unsupported_tie` | Either format. The editorial still calls a gameweek score or league position "joint", "tied", "level" or "shared" after one retry where no two managers hold it (said of the season's highest or lowest score, where the League History season record was scored only once either, or where the score the tie is said of is not the record). The message quotes the tie and says who holds the rank alone (or that nobody does). Saved as written, with the same lines in the report's callout |
| `synthesis_wrong_gameweek_placing` | Either format. The editorial still gives a manager a placing on the week after one retry that their gameweek score does not earn: a placing it says is the week's ("third on the week", "came last on the week", "the second-best score of the week"), or one the manager's own gameweek score is attached to ("finished eighth on 31", the league position read as the week's finish that issue #483 reported, or "scored 31 and finished eighth"). The message quotes the placing and gives the manager's score and gameweek placing, who held the week's lowest or highest score where it said "last", "bottom" or "top", and, where the placing it gave is the manager's league position, says so. Saved as written, with the same lines in the report's callout |
| `synthesis_escape_misattribution` | Either format. The editorial still says, after one retry, that a manager escaped, avoided or dodged a last place they finished in: last place on the week (or the bottom, the wooden spoon, the worst week) for a manager on the week's lowest score, or the foot or bottom of the table for a manager bottom of it. The message quotes the escape, gives the manager's score and who was level on it, or their place at the bottom of the table, and says who avoided last place on the week and by how much, as the GW Standings section states it. Saved as written, with the same lines in the report's callout |
| `synthesis_contested_roll_call` | Draft only. The shipped editorial still has a paragraph that names a contested race's player and three or more of its beaten managers. The message says how many of the race's beaten managers the paragraph names and which player they were beaten to, or, where more than one paragraph lists the race at the same count, how many paragraphs do. Unlike the codes above, the sentence is true: this is a draft that broke its brief, not one the data contradicts, so the saved report's callout says so in its own words. Two beaten managers in a paragraph, three spread across paragraphs, or a paragraph that never names the player never raise it. A retry ships over the first draft only when it has fewer contradicted claims, or as many and fewer roll calls, so the warning means the draft that shipped is one that lists them. Saved as written, with the same line in the report's callout |
| `synthesis_penalty_repeated` | Either format. The shipped editorial still writes a fine's forfeit out word for word more than once after one retry, headline included, where the brief allows it once (see [Naming the forfeit](#league-recap)). The message names the forfeit and quotes each time it was written. Like `synthesis_contested_roll_call`, the sentences are true: this is a draft that broke its brief, so the saved report's callout says so in its own words. Only forfeits of two or more words are counted, and a mention in other words, from `penalty_phrasings` or not, is never one |
| `league_standings_moved_on` | The recapped gameweek is the most recently finished one, but a later gameweek has started, so the league table no longer describes it. The gameweek is recapped and recorded from each manager's own gameweek history instead — see [When to run it](#league-recap) |
| `league_tables_updating` | FPL's processing feed for the recapped game covers the recapped gameweek and says bonus is not yet confirmed for every match day, or the league tables are still updating, so the positions and totals the recap read may be stale. The gameweek is recorded anyway; re-run before the next deadline and the later capture supersedes this one — see [When to run it](#league-recap) |
| `draft_provenance_unavailable` | Draft only. The Standout Players lines could not say how each player came to his manager, so they read without it: the history of the league's draft, waivers and trades (or its list of drafts) could not be read, did not end where FPL's own ownership list says everyone is, did not match the squads the gameweek recorded, or the draft's picks are held by neither the choices feed nor a saved snapshot (see [How a draft player came to his manager](#league-recap)). The message names which. Nothing else in the recap changes |
| `draft_rounds_unknown` | Draft only. A held draft's picks are held by neither the choices feed nor a saved snapshot (a redraft was scheduled before the first snapshot was taken), so the Standout Players lines say who drafted each player but not in which round: each manager's drafted set was rebuilt from their squad at the draft's gameweek and the moves made before it. A notice, not a failure: the lines are labelled all the same. The message says where a copy of the draft's choices response would restore the rounds, and the warning stops once one exists or the redraft is held (see [How a draft player came to his manager](#league-recap)) |

None of these change the exit code — `league-recap` exits 0 whenever the recap itself
rendered, and a skipped, truncated, self-contradicting or brief-breaking editorial (`synthesis_provider_unavailable`,
`synthesis_stopped_early`, `synthesis_contested_misattribution`, `synthesis_net_misattribution`,
`synthesis_kind_net_misattribution`, `synthesis_unsupported_tie`, `synthesis_wrong_gameweek_placing`,
`synthesis_escape_misattribution`, `synthesis_contested_roll_call`, `synthesis_penalty_repeated`) is no exception.

Four things do exit 1, all emitting the shared `{"command", "error"}` envelope on stdout
under `--format json` (see [JSON Output](#json-output)) and the same message as red prose
on stderr in table mode: an unreachable FPL API, a gameweek that could not be resolved at
all, a reconciliation failure, and a `fines:` block that cannot be parsed (a bad rule, a
mistyped `threshold:`, or an unrecognised key). The distinction matters when scripting a
retry — an outage is worth retrying, the other three are not, with one exception: a
reconciliation failure raised while FPL is still processing the gameweek keeps its own
diagnosis and adds that the mismatch may be timing instead, since the table it checked
against may not have caught up. Re-run once FPL finishes; a failure after that points at
the mapping.

The second used to answer differently depending on how you asked: exit 1 with a generic
`"Could not resolve a gameweek to recap."` under `--format json`, exit **0** in table mode
with the specific reason on stdout. It now exits 1 either way and carries the reason the
resolver actually has — `Gameweek 7 is not yet finished`, `Gameweek 99 not found`,
`Season hasn't started` — into whichever channel you are reading. `fpl review` shares the
resolver and changed with it, from exit 0 and prose on stdout to exit 1 and prose on
stderr. Declining a gameweek still being played is unchanged and deliberate: there are no
final scores to recap until it finishes.

The fines one refuses rather than degrades on purpose. An unreadable `fines:` block is not
the same as no fines configured, and recapping anyway would write "every configured rule
was checked, none triggered" into the append-only ledger for a gameweek whose rules were
never read — a false acquittal the season cannot be walked back to correct. Fix the rule
and re-run; nothing has been recorded in the meantime.

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
more), in which case it re-rules the whole cohort together, so a cohort-relative rule like
`last-place` is never ruled half under one cohort and half under another.

**A tie for last is shared.** Every manager level on the lowest score that gameweek is
fined, not whichever of them the cohort's arrival order reached first — the same reading
the league table already takes, where managers level on points share the position. Because
the ledger is append-only, gameweeks recorded before this was true cannot be edited: every
`league-recap` run instead sweeps the gameweeks it targets and appends the recorded ruling
against the managers it left out, reported as `league_history_tied_last_place_repaired`. A
manager the sweep cannot place — level under one measure of a gameweek that does not record
which measure ruled it — is named by `league_history_tied_last_place_ambiguous` rather than
fined on a guess.

**A block it cannot read stops the command.** An unknown `type:`, a rule missing one, a
`below-threshold` without its `threshold:`, a non-string `penalty:`, a `threshold:` that
isn't a number (`"40"` in quotes is the easy YAML slip), or a key `fines:` doesn't
recognise — only `classic`, `draft`, `escalation_note` and `penalty_phrasings` are valid — is reported by name,
on stderr in table mode and in the `error` envelope under `--format json`, rather than
skipped. The same message comes back from any command that reads the block, `fpl status`
included, so a hand-edit slip is diagnosed wherever you next run.

The key check matters more than it looks: `clasic:` would otherwise leave the rule list
empty, which reads downstream as *no fines configured* rather than as a typo, and
`league-recap` would record that non-ruling as a completed one.

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

Rolling a setup into a new season silently invalidates IDs and per-team files: a dead draft league returns nothing, entry and league IDs reissued over the summer resolve to a stranger's team or league, a per-team file rebuilt in August can still describe last season's twenty clubs, and the scoring scale can still be anchored to a cohort two summers gone. None of these error — they produce plausible output. `fpl doctor` checks all of it in one pass:

**IDs in `settings.yaml`** — each configured ID is resolved against the live API and the team/league name reported back, so a wrong-but-valid ID is visible:

- `classic_entry_id` resolves, reporting the team and manager name, **and belongs to `classic_league_id`** (via the entry's own `leagues.classic`) — catching an ID reissued over the summer that now points at someone else's team. The reissued-ID verdict only fires when the league itself checked out, so a stale league ID cannot condemn a correct entry
- `classic_league_id` resolves, reporting the league name back. No season assertion: classic league IDs come from a sequence that restarts each July, so `created` always lands in the current season and last season's ID resolves to a *different* league rather than going dead. The stamp proves nothing here — the entry's membership check above is what proves the pairing is still yours
- `draft_league_id` resolves; flagged when its draft was held in a previous season (draft leagues are recreated each season). The fix hint points at `fpl init` rather than a manual edit, because init derives this ID from your `draft_entry_id` and never prompts for it unless that derivation fails
- `draft_entry_id` resolves **and belongs to `draft_league_id`** (via the entry's `league_set`), catching a recycled ID that points at someone else's team — the recycled-ID verdict only fires when the league itself checked out, so a stale league ID cannot condemn a correct entry

**Data files:**

- `team_ratings.yaml` — season stamp and team set vs the live league, judged against the same resolved season the report names (problems here are stale, not broken: the ratings service already ignores or rebuilds bad files)
- `team_managers.yaml` — merged shipped + user copy covers exactly the current twenty clubs
- `previews/` — optional season preview intel: a file for a club not in the current league is flagged (it loads and inflates the coverage gate), and files the loader skipped (previous season, malformed) are surfaced with `fpl intel` as the follow-up
- `team_finances.json` — `scraped_at` falls within the current season
- `player_prior.yaml` — season label matches (auto-invalidated otherwise)
- `returnee_snapshot.json` — the returnee radar's week-over-week snapshot: season label matches (a previous season's is discarded and rebuilt on the next `fpl returnees` run), and reports the gameweek it currently holds

**Scoring calibration:**

- `quality_ceilings` — the season the [calibrated quality ceilings](custom-analysis.md#quality--value-scores) were measured against, recorded in the generated block as `CALIBRATION_SEASON`. Unlike everything above it, this is season data frozen in code rather than a file that rolls or rebuilds: a July rollover that passes without a recalibration leaves the anchors describing a cohort that has since turned over, with every code-side input unchanged and the fingerprint drift guard green. Anchors from the last completed season are the intended steady state all year and report OK; a completed season newer than the recorded one is **stale**, with the upgrade (a newer `fplkit` release carries a freshly calibrated block) and the maintainer re-run both named

**Environment** — which directory each of config/data/cache resolved to and whether an `FPL_CLI_*` override is in effect, plus whether `settings.yaml` exists.

**Which season it is** — the season doctor reports in `metadata.season`, and judges "a previous season's" against, comes from GW1's `deadline_time` where the API answers and the July cutover only where it does not — the same rule `fpl status` uses, so the two commands cannot name different seasons for the same moment. A season that overruns 1 July (2019-20, COVID-delayed) therefore keeps its own label in both, and a `team_finances.json` scraped during that overrun reads as current rather than as a previous season's.

Each finding is classified as **broken** (wrong answers now — fix it today), **stale** (self-corrects, needs one routine refresh, or waits on an upstream fix the tool already works around), **skipped** (not configured/present), or **unchecked** (API unreachable). Exits non-zero when anything is broken, so it can gate scripts.

**`--providers`** checks the external data sources instead of the local setup. None of them version anything, and the tool degrades gracefully everywhere, so upstream drift otherwise surfaces as plausible but wrong output — a renamed stat field zeroes every player's xG without an error. Each probe asserts shape and volume, not just reachability, and where a column check is not the contract it runs the parser itself:

- **FPL API** — bootstrap has 20 teams / a sane player count / 38 gameweeks, and every stat field the tool reads is present in the raw data (a missing one would silently read as 0). One `element-summary` is probed as well, for the player with the most minutes: its history rows must carry every field the per-gameweek signals read (a renamed one would read as 0 for every gameweek of every player, with form trajectory and the consistency signals going neutral), and must exist at all once a gameweek has finished
- **Draft API** — bootstrap resolves with 20 teams and a sane player count, and its club list agrees with the main game's by `code`. Every draft path translates a draft club id through that code (see [Player identifiers](#player-identifiers)), so a draft club with no main-game counterpart is broken: that club's players are read with no club. A main-game club missing from the draft list is named but stays OK, since no draft player can be at a club the draft game does not list, and so are clubs the two games number differently, which are translated; the numberings have agreed every season so far, so either is worth seeing. The row reports unchecked when either bootstrap could not be read or the main one carried no clubs
- **vaastav dataset** — for each season it serves (the two oldest of the four-season window), `players_raw.csv` exists upstream, covers the columns the parser reads, and clears a row-count floor
- **Core-Insights dataset** — same for `players.csv` and `playerstats.csv` in each season it serves (last season and the one in progress; it is the sole source for both), plus the current season's per-gameweek files at the latest finished gameweek. `players.csv` must also parse into the player lookup every other file joins on, and the per-gameweek files are run through the parsers the scoring commands use rather than a header check: the columns can all be present over values nothing survives, so the probe asserts the parse yields records, and that the match join keeps most of the rows it reads, and names the signals a shortfall costs. A file missing, parsing to nothing or keeping under half its rows only for the newest gameweek is a publishing lag or a backfill in progress (stale); the same problem two gameweeks running is a layout or format change (broken). Opponent Elo is judged separately, because a blank one keeps its player-match records, which count unadjusted in the opponent-adjusted xG signals: the probe counts matches without Elo across the same 12-gameweek window scoring reads, notes a minority on the ok row, and reports stale once most of the window lacks it, saying whether only the newest gameweek does (a backfill) or earlier ones too (an upstream gap)
- **Understat** — an HTTP status from Understat is the provider answering, not the network failing, and while it answers that way every scoring command falls back to FPL xGI for every player without a word: a refusal (401, 403) is broken, so is a gone endpoint (404, 410: `getLeagueData` is undocumented and has moved before, and only a code change brings the enrichment back), any other status (429, 5xx) is stale, and only a network failure is unchecked. Then: league data is non-empty, and every current club resolves to rows in Understat's own data (an unresolved club silently loses xG enrichment); early in the season an unresolved name is reported as stale, since Understat only lists a club once it has ingested a match for it. The club test is the enrichment's own gate (`understat_club_rows`), called on the same league payload the scoring commands scan, so the probe and the runtime cannot reach different verdicts about the same club — a second copy of the gate written beside the probe is exactly how they diverged in #229. Resolving a club says nothing about whether its players' names line up, so a second row reports how many players FPL says have played actually join by name, naming the ones that did not — three players lost their xG enrichment to an HTML-escaped apostrophe while this probe called all 20 clubs healthy (#263). A few percent always miss and stay green; a wholesale break reads as stale early in the season and broken once the pool has settled. Players at a club that failed to resolve are excluded, since that is the row above's finding and counting them here would restate one club gap as a squad's worth of name misses
- **football-data.org** — configured, standings has 20 rows, and every served TLA maps onto a live FPL short name (a failed join here doesn't produce a missing rating — it silently re-rates the club as promoted)

The same classification applies, and transient unreachability is always **unchecked**, never broken (a provider that answers with an HTTP status has been reached, which is why Understat's refusal reads as broken). A scheduled CI job runs `fpl doctor --providers --format json` weekly and fails only on broken, so provider drift with no commit behind it still surfaces within days.

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
  penalty_phrasings: ["pint of shame", "penance pint"]   # Optional - how the recap editorial may name the forfeit
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
    model: claude-sonnet-5-5
    timeout: 60
    query_defaults:
      max_tokens: 8000
      output_config:
        effort: high            # Anthropic only; null unpins it
```

`output_config` is passed through to the Anthropic Messages API; the shipped
`effort: high` is `claude-sonnet-5-5`'s own default, pinned so that switching models cannot
change it silently (`claude-opus-5-5`, for one, defaults to `medium`). Change it in
`settings.yaml` to sweep effort, or set `effort: null` to unpin it. A model that predates the
parameter (Sonnet 4.5, Haiku 4.5, Opus/Sonnet 4.0 and 4.1, Claude 3) is not sent `effort`,
since it would refuse every call. A block that is not a mapping (`output_config: high`) fails
the call with an error naming it rather than being ignored. An OpenAI-compatible provider
ignores the block. A role whose config carries no `max_tokens`
gets 8000 on Anthropic, since thinking and the answer share that ceiling.

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

`last-place` is the one rule ruled against the whole league rather than against your score
alone, so it is the one a tie can fall on. Managers level on the lowest score of the
gameweek are all fined — the tie is shared, the way the league table shares a position,
rather than settled by whichever of them the standings happened to list first.

`penalty_phrasings` is the league's own ways of saying its forfeit, which the `league-recap`
editorial varies between rather than repeating the `penalty` word for word (see Naming the
forfeit under [league-recap](#league-recap)). Set it under `fines:` to cover every rule, or
on one rule to give that rule its own list, which then replaces the block's for it. Set it
at the block level only when every rule carries the same forfeit. It must be a list of
strings; blanks and repeats are dropped. The Fines list and the ledger always carry the
configured `penalty`, whatever is set here.

```yaml
fines:
  penalty_phrasings: ["pint of shame", "penance pint", "one for the group chat"]
  classic:
    - type: last-place
      penalty: "Pint on video"
```

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
