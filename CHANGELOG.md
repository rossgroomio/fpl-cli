# Changelog

All notable changes to this project will be documented in this file.
## [2.5.0] - 2026-09-07

### Bug Fixes

- stop the fpl-mate output style calling ep_next prior-informed (#261)
- restore xG enrichment for players with an apostrophe in their name (#268)
- keep coverage readable when one ledger file is damaged (#271)
- stop the review scrubber deleting sentences naming full names (#270)
- recap the last completed gameweek once the next one has kicked off (#274)
- stop fpl xg table output crashing when custom analysis is off (#275)
- cap httpx, pydantic and click below their next majors (#278)
- stop a truncated LLM summary being saved without a warning (#276)
- stop a draft replay erasing the league positions it recorded (#280)
- validate FPL_CLI_DATA_DIR and FPL_CLI_CACHE_DIR eagerly like FPL_CLI_CONFIG_DIR (#281)
- raise the click floor to 8.2 so the test suite can capture stderr (#283)
- collapse a league-wide captain tie into one recap line (#282)
- fill the blank Source column in a review's Standout Performers (#284)
- exit 1 with the real reason when review or recap cannot run (#285)
- point doctor's draft ID fix hints at `fpl init` (#288)
- keep the current table's points out of a replayed gameweek's recap (#293)
- report a failure the same way whatever the output format (#292)
- keep league positions when one manager's replay data is partial (#295)
- stop a replay renaming the player a stored red-card fine names (#290)
- ground the review's "Next Week" advice in next gameweek's fixtures (#291)
- rebuild the ratings prior when a football-data key appears (#289)
- record the club a player played for in a replayed gameweek (#296)
- keep `review --summarise` running when an LLM key is missing (#297)
- derive season labels from GW1's deadline instead of the clock (#299)
- correct award move direction and free-agent mislabelling (#302)

### Features

- flag stale quality-score calibration in `fpl doctor` (#298)
- hand the league-recap editorial every manager's transfers (#300)
- give the draft recap editorial every waiver and free-agent move (#303)
- give the season-opener recap each manager's prior FPL seasons (#304)

## [2.4.1] - 2026-09-05

### Bug Fixes

- polish output inconsistencies found in the v2.4 test-plan run (#239)
- keep agent progress off stdout so JSON output stays parseable (#240)
- stop league-recap calling a manager's first fine their second (#241)
- write team_finances.json in stable diff order with real UTF-8 (#244)
- repair squad-builder and update-gw-prep's normalisation call (#245)
- restore xG data for a signing yet to play for their new club (#242)
- scale the `fpl xg` minutes floor to the gameweeks played (#243)
- show returnee week-over-week changes on every run of a gameweek (#246)
- stop review bench comparisons mixing combined and per-player (#249)
- stop fpl returnees flagging a healthy Understat team map (#248)
- say which league history file is unreadable, and how to repair it (#250)
- stop GW review dropping the gameweek's top scorer (#252)
- report every table-mode failure on stderr instead of stdout (#251)
- raise when include_match_data lacks include_players (#254)
- stop early-season advice offering ep_next as a second opinion (#253)
- centre GW review's Nearby Rivals window on the user (#257)
- report a malformed fines rule instead of crashing with a traceback (#258)
- report missing or wrong classic IDs instead of masking them (#259)
- rate a returning promoted club from its Championship record (#256)

### Refactoring

- read every command's settings off the Click context (#247)

## [2.4.0] - 2026-09-04

### Bug Fixes

- retry rate-limited return-intel searches instead of dropping them (#195)
- stop HTML entities breaking markdown in generated reports (#198)
- refuse to guess which player a shared surname means (#197)
- tell gw-prep adopters which Python its helper scripts need (#196)
- rank easy fixture runs on the same FDR model as the ATK/DEF columns (#199)
- return real stats in early-season gw-prep and squad-builder runs (#194)
- score Triple Captain doubles on their opponents, not their own club (#203)
- surface returning keepers and defenders on the injury watchlist (#205)
- rank draft waiver keepers on saves and defensive quality (#210)
- stop printing the season-phase note in league-recap reports (#211)
- score draft waiver targets on their own history, not a stranger's (#212)
- warn when gameweek prep and squad builds run on stale team ratings (#214)
- stop league-recap editorial inventing league-history claims (#216)
- stop league-recap streak lines misreading run counts as rankings (#217)
- mark blanks and doubles by the club a player had that gameweek (#213)
- score `fpl fixtures` on the same FDR as `fpl preview` (#215)

### Features

- source last season's player history from Core-Insights (#204)
- blend last season's pedigree into early-season quality scores (#208)
- blend last season's pedigree into ownership-family scores (#218)

## [2.3.2] - 2026-08-26

### Bug Fixes

- keep a recorded club when a current gameweek's league-recap re-runs (#179)

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
