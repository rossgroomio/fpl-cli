# Changelog

All notable changes to this project will be documented in this file.
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


