# Changelog

All notable changes to this project will be documented in this file.
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


