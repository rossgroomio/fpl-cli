# Changelog

All notable changes to this project will be documented in this file.
## [2.3.1] - 2026-08-26

### Bug Fixes

- keep draft players matched to the main game when FPL renames them (#171)
- judge a replayed gameweek's blanks by the clubs players had then (#173)
- keep the club and name a replayed gameweek already recorded (#175)

## [2.3.0] - 2026-08-26

### Bug Fixes

- catch a classic entry ID reissued to another manager over the summer (#137)
- restore xG enrichment for players who moved club mid-season (#151)
- put the JSON error envelope on stdout alongside the success one (#153)
- stop LLM narratives putting players at clubs they left (#152)
- catch Core-Insights gameweek files that parse to zero records (#154)
- scale GK ceilings to sample size and warn on early-season quality (#156)
- rate teams from GW1 results instead of waiting for GW2 (#155)
- keep `--format json` stdout parseable on stats and price-history (#157)
- stop league-history rows claiming facts the gameweek never had (#158)
- report every `--format json` failure as an envelope on stdout (#159)
- credit every tied manager on gameweek win/loss streaks (#166)

### Features

- capture the per-gameweek world rank in the league-history ledger (#161)
- track fines across the season instead of only the current gameweek (#165)
- report how many times each streak has happened this season (#167)

## [2.2.0] - 2026-08-25

### Bug Fixes

- stop early-season fixture difficulty falling back to a flat 4.0 or one gameweek (#109)
- repair stale team ratings and stop over-weighting last season (#114)
- stop NOT/NFO mismatch and SHE tla collisions corrupting team priors (#113)
- stop early-season scoring signals crashing on incomplete history (#120)
- stop scores printing negative in columns documented as 0-100 (#121)
- stop select_starting_xi labelling an empty XI as 4-4-2 (#124)
- stop promoted teams being rated among the Premier League's best (#125)
- stop unavailable players outranking ones who are actually playing (#126)
- calibrate MID/FWD/GK quality ceilings so elite players read 85+ (#127)

### Features

- allow choosing the browser for TLS-inspecting proxies (#108)
- warn on data-provider drift and add fpl doctor --providers (#123)
- surface injured players worth claiming before they return (#133)

## [2.1.0] - 2026-08-23

### Bug Fixes

- survive a keyring backend that panics below Python
- validate pre-season teams and guard pFDR against unrated seasons
- consistent non-zero exit codes on command failure, friendly pre-season squad 404 (#47)
- drop stale committed team_ratings.yaml seed
- rank promoted teams against the PL, not the Championship
- config dirs must be absolute, and gated commands name their gate (#46)
- layer team_managers.yaml over the shipped copy per club
- make team ratings and manager config season-aware (#56)
- stop gw-prep squad validation failing when a heading names the formation
- keep the new-signing flag alive until real minutes exist, not until the window shuts
- stop gw-prep validators mislocating sections on drifted headings
- stop review inventing fines, positions and rolled transfers at gameweek 1
- keep the status classic section alive pre-season and stop last-place fines misreading a partial league table
- report exact classic league size and rank in status instead of page-one figures
- keep league recap accurate for large leagues and gameweek 1
- cap the managers named in a tied league recap bench-haul award
- pin PuLP below 4.0 so squad allocation keeps working (#80)
- stop a new season's reports overwriting the previous season's (#90)

### Features

- add FPL_CLI_DATA_DIR override so the data dir survives ephemeral environments
- user config dir copy of fixture_predictions.yaml overrides shipped copy
- add `fpl intel` for season preview intel with per-gameweek decay
- add preview-ingest skill for turning preview prose into intel files
- wire season preview intel into squad-builder
- wire season preview intel into gw-prep and update-gw-prep
- point-in-time headline numbers and league positions for replays (#75)
- record every gameweek to a durable league history store (#76)
- surface streaks and season phase in every output (#77)
- add `fpl doctor` to surface stale config and dead IDs (#98)

### Performance

- stop fpl player and fpl stats fetching data they discard (#96)

## [2.0.0] - 2026-05-25

### Bug Fixes

- allow ep_next/ep_this to be None at end of season
- handle ep_next/ep_this=None in stats sort and table display

### Features

- emit JSON null for missing ep_next/ep_this projections

### Refactoring

- dedupe ep_next/ep_this JSON shim and sort sentinel

## [1.4.2] - 2026-05-24

### Bug Fixes

- feat(scout): bind player clubs to current-season reference (#40)
- feat(sell-prices refresh): Add retry logic to /api/me/ polling with hydration race handling (#39)

### Scout

- enforce player_reference as a strict allowlist (#41)

## [1.4.1] - 2026-05-17

### Refactoring

- refactor FPL scraper to fetch API directly instead of intercepting (#37)

## [1.4.0] - 2026-05-17

### Bug Fixes

- handle bench boost chip in classic team display (#27)
- chip-aware contributed in league-recap and status (#28)
- narrative bugs in gameweek review (identity, terminology, captain, grounding, fabrication) (#30)
- rename Standings to GW Standings; anchor chip count
- prevent fabricated momentum stats and truncated pFDR rows
- correct captain hindsight swing formula
- tighten manager attribution rule in research prompt
- prevent hallucinated players and fabricated backstories in research tables
- repair broken tests + harden research validator
- correctly attribute worst captain to blank-VC managers
- scope draft waiver validator to its subsection
- clamp window to season end + ship fixture predictions in package
- bucket draft txns by league_entry id, not entry_id
- surface full captain roster to synthesis prompt (#32)
- scrub fabricated names from research narrative and tables (#33)
- hit-aware transfer/waiver award net + clearer detail (#34)

### Features

- localise user-facing times to UK (GMT/BST) (#29)
- Phase D1 draft waiver validator (#31)
- contract chain rebuilds in waiver award details

### Scraper

- honour FPL_BROWSER_IGNORE_CERTS for TLS-inspecting proxies (#35)

## [1.3.0] - 2026-04-17

### Bug Fixes

- correct stats key names and draft trailing heading (#19)
- inject explicit chip roster into synthesis prompt
- six LLM/formatting fixes for gw review output (#26)
- use custom team-ratings FDR in GW fixtures table when custom_analysis enabled
- drop understat position from enrichment spread

### Features

- embed squad-builder output on wildcard/freehit weeks (#23)
- surface active chips in recap narrative
- detect bench boost chip and adjust analysis emphasis
- chip-aware analysis for WC, FH, BB, and TC (#24)
- WhatsApp-friendly standings text block (#25)

### Refactoring

- consolidate ownership ceiling dispatch into single helper
- thread Position literal through scoring pipeline (#21)
- rename horizon=1 output field to single_gw_score (#22)
- tighten project SKILL.md prompt contract and phase ordering

## [1.2.1] - 2026-04-10

### Bug Fixes

- attenuate multi-GW raw_quality by position + halve gk_cs_rate (#16)
- unify quality_score display ceilings across commands (#17)

## [1.2.0] - 2026-04-09

### Bug Fixes

- gate xGI sustainability behind custom_analysis flag
- attenuate GK signals for low-minutes keepers

### Features

- add player reliability metric (#10)
- add rolling-window xGI sustainability signal (#11)
- add GK-specific quality scoring path (#12)
- fixture-adjusted npxG to remove double-counting (#13)
- add consistency index signals (Phase 1) (#14)
- wire consistency signals into scoring (Phase 2) (#15)

## [1.1.0] - 2026-04-07

### Bug Fixes

- wrap migration in try/except, update architecture.md paths
- CLI hygiene batch (P10-P13) (#4)
- Understat name matching for abbreviated/punctuated web_names
- PR #4 code review follow-ups (#5)
- review fixes - gather resilience, CSV field protection, test gaps
- correct README data source count and Core-Insights repo URL

### Features

- add cached dataset fetcher with ETag conditional requests (#6)
- add rolling pts/£m metric and rename value fields (#7)
- expose ep_next/ep_this (FPL predicted points) (#8)
- implement CoreInsightsClient for 2025-26 season data
- add HistoricalDataProvider composition layer, update call sites

### Refactoring

- extract shared types, rename season helpers, separate cache dirs

## [1.0.0] - 2026-04-03

### Features

- CI pipeline and PyPI publish for v1.0.0 (#1)
