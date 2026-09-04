# Plan template

The document shape to produce. Sections in this order; the vault's
convention is no blank line before or after a heading.

## Skeleton

```markdown
# fpl-cli v<from> → v<to> test plan
[Opening paragraph: what this covers, and the release-boundary caveat —
whether unreleased commits on main are docs-only (so testing the released
fplkit covers the whole range) or whether code sits untested beyond the
tag.]
[Second paragraph: run order and why. Which part is first and what it
produces. Which parts can slip to a later session.]
## How to run this plan
- [cwd, and that report paths are cwd-relative]
- [never hardcode season or gameweek — read them from the preflight]
- [PASS / FAIL / BLOCKED per test into the Results log; a FAIL doesn't
  stop the run unless it makes later tests meaningless]
- [stderr capture convention, and which stream the error envelope uses]
- [cost awareness: which flags spend LLM calls, scraper runs or FPL
  requests]
- [anonymity: fpl-cli is public, this vault is private]
## Preflight
**P1 — version.** [...]
**P2 — environment.** [...]
**P3 — baseline status.** [...]
**P4 — git baseline.** [...]
## Part 1 — <surface> (run first)
Changes under test: [one sentence naming the PRs by number and what they
did]. Reference: [the docs section the expectations come from].
**X1 — <short name>.** `<exact command>`. Expect:
- [falsifiable expectation]
- [falsifiable expectation]
## Part 2 — <surface>
[...]
## Carried forward from v<previous range>
[Only if Step 3 found anything. Each item: the original test ID, what it
found, and whether this range claims to fix it.]
## Wrap-up
1. [commit generated state worth keeping]
2. [fill the Results log, summarise counts, separate fpl-cli bugs from
   environment issues]
3. [do not fix fpl-cli from the vault — its source isn't there]
## Results log
| ID | Result | Notes |
| --- | --- | --- |
| P1 |  |  |
| P2 |  |  |
[one empty row per test ID]
```

## Writing a test

Each test is a bolded ID and short name, the exact command, then a bulleted
list of expectations. One paragraph of prose is fine when the setup needs
explaining; anything longer means the test is really two tests.

**Weak** — restates the changelog, cannot fail:

```markdown
**D3 — provider probes.** Run `fpl doctor --providers`. Expect the provider
drift probes to work.
```

**Strong** — every bullet is a thing that would read differently if the fix
regressed, and the numbers came from the diff and the docs:

```markdown
**D3 — provider probes.** Run `fpl doctor --providers; echo $?` and
`fpl doctor --providers --format json`. Expect all six probes, each
asserting shape **and** volume, not just reachability:
- FPL API: 20 teams, sane player count, 38 gameweeks, and every stat field
  the tool reads present in the raw data.
- Understat: league data non-empty and every current club's name resolves;
  this early in the season an unresolved club must classify as **stale**,
  not broken (Understat lists a club only after ingesting a match for it).
- football-data.org: configured, 20 standings rows, every served TLA maps
  onto a live FPL short name (the join whose silent failure re-rates a club
  as promoted).
Transient unreachability must classify as **unchecked**, never broken; exit
non-zero only on broken.
```

Note what the strong version does: names the count (six), the volumes (20,
38), the classification vocabulary the code actually uses (broken / stale /
unchecked), and the *consequence* of the bug being tested ("the join whose
silent failure re-rates a club as promoted"). The last one matters most —
it tells a runner staring at ambiguous output which way to call it.

## Marking a test BLOCKED up front

When the season state can't exercise something, write the test and gate it,
naming the condition. This is how it reaches the next plan:

```markdown
**R4 — point-in-time replay.** Once at least two gameweeks are completed,
run `fpl league-recap -g <earlier GW>`. Headline numbers and league
positions must be as at that gameweek, not today's [...]. If only GW1 is
complete when this runs, defer R4 and mark BLOCKED with the date.
```

## Reversible destructive tests

Corruption and deletion tests run against data the user wants to keep. Give
the restore command in the same bullet as the damage:

```markdown
**R8 — unreadable store degrades gracefully.** Reversible corruption test:
truncate one captured file mid-line (`truncate -s 40 <path>`), re-run
`fpl league-recap 2>/tmp/r8-err.txt`. Expect a `league_history_store_
unreadable` warning naming the file and remedy, the recap still rendering
from live data, exit 0, and the corrupt file left untouched. Restore with
`git checkout -- data/league_history/` and confirm a clean re-run.
```

## The Results log

One row per test ID, pre-seeded empty, in plan order. The runner fills
Result with PASS / FAIL / BLOCKED (`PASS*` for a pass with a caveat worth
reading) and Notes with one line of evidence — the actual numbers seen, not
"looked fine".

Leave room below the table for free-text post-plan findings. The reference
plan's five post-plan findings — things a human noticed in real output that
no test asked about — produced several of the next release's fixes. Say so
in the wrap-up so the runner knows to record them rather than mention them
in passing and lose them.
