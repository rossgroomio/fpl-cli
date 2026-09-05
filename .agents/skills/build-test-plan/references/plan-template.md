# Plan template

The document shape to produce. Sections in this order; the vault's
convention is no blank line before or after a heading.

## Skeleton

```markdown
# fpl-cli v<from> → v<to> test plan
**Status:** not yet run.
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
- [write each row to the file as the test finishes, not in a batch at the
  end — a long run gets its context compacted and anything still only in
  the session's head is gone]
- [on resuming in a later session, read the Results log first and skip
  rows already filled; it is the run's only durable state]
- [stderr capture convention, and which stream the error envelope uses]
- [cost awareness: which flags spend LLM calls, scraper runs or FPL
  requests]
- [anonymity: fpl-cli is public, this vault is private]
- **[reports go to a scratch directory, never the vault's report tree.**
  `preview`, `review` and `league-recap` all take `-o/--output`, so every
  `--save` in this plan must carry `-o <scratch>/reports`. Files under
  `01_Reports/<season>/` are point-in-time snapshots of a gameweek as it
  was analysed at the time — regenerating one overwrites the record with a
  later model's output, and a preview for a gameweek whose deadline has
  not passed is simply wrong to have on disk. Neither is a test artifact.
  The same goes for rewritable state under `data/` that is not the ledger:
  a test that migrates or rewrites a snapshot file (`returnee_snapshot.json`,
  `team_ratings*.yaml`, `team_finances.json`) runs against a scratch
  `FPL_CLI_DATA_DIR`, so the real one is untouched.]
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
1. [replace the Status line under the title with the run's date, version
   and PASS/FAIL/BLOCKED counts]
2. [commit the filled plan, and **only** the generated state that is both
   irreplaceable and genuinely new: new rows under `data/league_history/`
   for a gameweek that had none. Do **not** commit regenerated reports
   under `01_Reports/`, a preview for a future gameweek, or a rewritten
   `data/` snapshot — revert those with `git checkout` before committing.
   If a test needed a report, it was written to the scratch dir and the
   Results-log row cites it from there. The test that a piece of state
   passes: would this have been created anyway, by real use, today?]
3. [fill the Results log, summarise counts, separate fpl-cli bugs from
   environment issues, and record anything noticed outside the test
   matrix as a post-plan finding]
4. [close the carried workspace issues whose items passed; comment the new
   blocking condition on the ones still deferred]
5. [do not fix fpl-cli from the vault — its source isn't there. Report
   findings to the user; anything filed upstream takes the house issue
   style — Summary / Repro / Cause / Impact / Suggested fix / Test gap —
   and the anonymity rule from the top of the plan]
## Coverage
| PR | Change | Tests |
| --- | --- | --- |
| #NNN | [subject] | X1, X4 |
| #NNN | [subject] | — not tested: [one-line reason] |
## Results log
| ID | Result | Notes |
| --- | --- | --- |
| P1 |  |  |
| P2 |  |  |
[one empty row per test ID]
```

## Closing the document out

The plan is not just instructions — it is the run's record, and the next
plan's Step 3 reads it rather than the session transcript that produced it.
So the wrap-up has to say how to leave it, or a finished run leaves behind
a half-filled table nobody can date.

Give the plan a `**Status:** not yet run.` line under the title, and have
the wrap-up replace it on completion:

```markdown
**Status:** run <date> against fplkit <version> — 38 PASS, 4 FAIL,
3 BLOCKED. [one clause on anything that changes how to read the results,
e.g. "Part 4 deferred to a later session".]
```

That line is what a future reader — and the next plan — sees first. Without
it, telling a plan that was run clean from one that was never started means
parsing the whole table.

The wrap-up should also have the runner:

- Record post-plan findings in the document, below the table. Things
  noticed in real output that no test asked about are consistently the
  sharpest bugs a run produces, and they are lost if they stay in chat.
- Close the carried workspace issues whose items now pass, and comment on
  the ones still deferred with the new blocking condition — otherwise the
  same work queues twice and the next plan carries it forward again.
- Raise **one** issue in the vault for work this run deferred, when the
  document alone will not surface it again. The plan is the record and the
  next plan's carry-forward reads it, so most BLOCKED rows need nothing
  more. An issue earns its place when the unblocking condition is
  time-distant or event-gated (nobody opens a merged plan document nine
  months later), or when the item belongs to a range no plan covers and so
  has nowhere else to live. One issue holding many items, grown by
  comment, is the shape that works — a dozen issues for a dozen BLOCKED
  rows is noise. Give it the `testing` label, state the condition that
  unblocks each item, and record the data state at the time of testing:
  a later session cannot reconstruct why something was unreachable.
- Commit the filled plan alongside any genuinely new ledger rows, so the
  record and the evidence land together. Evidence that lives in the
  scratch dir stays there — quote the numbers into the Results-log row
  instead, because the row has to stand on its own once the scratch dir is
  gone. Never commit a report the run regenerated: the Results log is the
  artifact, not a rewritten snapshot.

## The coverage map

Build it from the Step 2 inventory, one row per in-range PR, before you
call the plan done. Writing it from memory of the tests you just drafted
defeats it — the whole point is to surface the change you researched
carefully and then lost while grouping tests by surface.

An em-dash in the Tests column is a legitimate answer for something
genuinely not worth a test (a pure refactor with no behaviour change, a
fix whose only surface is covered by another test), but it needs the
one-line reason next to it. An unexplained blank is a gap.

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
