---
name: build-test-plan
description: >
  Build a runnable test plan covering every user-facing fpl-cli change
  shipped between two releases, written for a Claude Code session in the
  fpl-workspace vault (live FPL API, real leagues, LLM keys). Use whenever
  the user says "build a test plan", "test plan for the release", "what
  needs testing since v2.2", "verify the last release", or asks how to
  check that shipped changes actually work against live data. Also the
  natural follow-up after cutting a release — offer it once a release is
  published.
model: opus
compatibility:
  claude-code: full (parallel research sub-agents; GitHub MCP on the web, gh locally)
  codex: partial (sequential research — no sub-agent spawning)
  cursor: partial (sequential research)
  copilot: partial (sequential research)
---

<!-- Runs in the fpl-cli repo; writes into the fpl-workspace vault. -->

# Build Test Plan

Turn a range of fpl-cli releases into a runbook another Claude Code
session can execute against live data in the fpl-workspace vault.

This skill runs **in the fpl-cli repo**, where the git history, PR bodies,
diffs and docs live. The plan it writes runs **in fpl-workspace**, which is
where the FPL credentials, LLM keys, real leagues and generated data are.
That split is the whole reason the plan has to be written down rather than
improvised: the session that knows what changed is not the session that can
exercise it.

Two things stay true throughout:

- **The plan targets released `fplkit`.** The vault installs the CLI from
  PyPI, so a test written against an unreleased commit on `main` cannot be
  run there. Unreleased work is noted, not tested.
- **fpl-cli is public; the vault is private.** The plan lives in the vault,
  but write it so it *could* be public: no manager or league names, no
  entry/league IDs. Refer to `classic-<league id>`, "your classic league".
  Real names belong only in the Results log the runner fills in.

## Step 1 — Fix the range

```bash
git fetch origin main --tags
git tag --sort=-creatordate | head -10
```

The default range is **the end of the last test plan → the newest release
tag**. Find where the last one stopped:

```bash
ls /home/user/fpl-workspace/docs/test-plans/
```

If the vault isn't cloned, attach `rossgroomio/fpl-workspace` and clone it
— the plan lands there and the environment facts come from there.

Then classify what sits beyond the newest tag:

```bash
git log <newest tag>..origin/main --oneline --no-decorate
```

If those commits are `docs:`/`chore:`/`ci:` only, say so in the plan's
opening paragraph — it tells the runner that testing the released version
covers the whole range, which is worth stating because it is not obvious.
If unreleased *code* sits on main, the honest options are to cut a release
first or to scope the plan to the released range and list the unreleased
work as untested. Put that to the user rather than silently picking.

Confirm the range with the user before researching. Getting it wrong wastes
the most expensive step.

## Step 2 — Inventory the changes

```bash
git log <from>..<to> --oneline --no-decorate
sed -n '/## \[<to version>\]/,/## \[<from version>\]/p' CHANGELOG.md
```

Squash-merged subjects carry their PR number as `(#N)` — that is the handle
for the research step.

Keep `feat:`, `fix:`, `refactor:`, `perf:`. Drop `chore:`/`docs:`/`ci:`/
`test:`/`style:` — but skim their subjects first. A user-visible change
mislabelled `chore:` is invisible to the changelog *and* to this plan, and
that is exactly the change nobody thinks to test.

## Step 3 — Carry forward the last plan's unfinished business

Read the previous plan's Results log. It is the highest-value input to the
new plan and the easiest to skip.

Three kinds of thing carry forward:

- **BLOCKED tests** — deferred because the season state didn't allow them
  ("only GW1 complete, defer the replay test"). Check whether the condition
  has since cleared. If it has, the test is now runnable and belongs in the
  new plan verbatim.
- **FAILs** — check whether the range under test fixes them. Match on PR
  subject and number: a FAIL that a shipped `fix:` claims to address needs
  a test that reproduces the *original* failure and confirms it is gone.
  A FAIL nothing has addressed carries forward unchanged, so it doesn't
  quietly become accepted behaviour.
- **Post-plan findings** — the observations recorded below the table. These
  are usually the sharpest bugs in the whole document, because they came
  from a human looking at real output. Same treatment as FAILs.

This loop is real: most of v2.3.0 exists because the v2.0→v2.2 run found
those bugs. A plan that doesn't close its predecessor's loop lets fixes ship
unverified.

## Step 4 — Research each change

This is where plan quality is won or lost. A test built from a commit
subject alone reads "verify `fpl doctor --providers` works" and cannot
fail. A test built from the diff reads "expect all six probes, each
asserting shape **and** volume: 20 teams, 38 gameweeks, every stat field
the tool reads present" — and that one catches a regression.

**Group before you fan out.** Cluster the commits by the surface they touch
(league-recap and its ledger; `doctor`; scoring and data quality; JSON
envelopes). Themes make better research units than individual PRs, because
several PRs usually move one surface together and the tests want to be
written against the surface's final state, not each intermediate step.

Then spawn one research sub-agent per theme, in a single batch so they run
concurrently. Brief each one:

> Research the fpl-cli changes in <theme> shipped between <from> and <to>,
> so a tester with a live FPL account can verify them. Commits: <list with
> PR numbers>.
>
> For each change, read: the PR body and any linked issue (via GitHub MCP
> `pull_request_read` / `issue_read`, or `gh pr view <N>`), the diff
> (`git show <sha>`), the relevant section of `docs/command-reference.md`,
> and the tests the PR added under `tests/`.
>
> Report back, per change:
> - The exact CLI invocation that exercises it, flags included.
> - Falsifiable expectations — specific counts, field names, value ranges,
>   exit codes, which stream output lands on. Name the actual values
>   (how many probes, which warning codes, which JSON keys), not "the
>   documented ones".
> - The repro from the issue, for a `fix:` — the condition that used to
>   break is what the test recreates.
> - Degradation and edge paths the change introduces or touches: missing
>   data, corrupt file, unreachable provider, invalid input, exit codes.
> - Whether the current season state can exercise it at all, and if not,
>   the condition that would unblock it.
>
> Ground every specific in a file you actually read. Do not describe
> behaviour you inferred from a commit subject.

Sub-agent findings are a draft, not the plan. Reconcile them yourself:
drop duplicates where two themes touch the same command, and check that
the specifics they quote really appear in the docs or diff before those
numbers become assertions the runner will trust.

## What makes a test worth writing

The reference plan (`docs/test-plans/v2.0-to-v2.2.md` in the vault) is the
quality bar. What separates its useful tests from filler:

**It can fail.** Every expectation is something that would read differently
if the fix regressed. "Renders without crashing" is worth one line for a
crash fix and nothing otherwise; "row count equals the real league size"
caught a real pagination bug.

**It tests the bug, not the fix.** For a `fix:`, recreate the condition
from the issue and assert the old symptom is absent. Testing that the new
code path runs tells you nothing about whether the bug is gone.

**It uses the observable surface.** The runner has a released binary, not
the source. Commands, files on disk, exit codes, stdout vs stderr — never
internal functions.

**It builds a fixture before it gives up.** When live data can't reach a
code path, a scratch fixture often can: point `FPL_CLI_DATA_DIR` at a
throwaway copy of the store and doctor a row to the shape the bug needed,
or feed the matching function inputs that disagree. This reaches repair
and migration paths that a healthy live store never exercises, and it
costs the runner a `cp -r` they throw away afterwards. Reach for it before
reaching for BLOCKED — and keep it off the real store, which for the
ledger is irreplaceable.

**It mines the environment for fixtures it already has.** The vault's own
data is full of them. Players in captured ledger rows who have since
changed club are a ready-made test that a re-run doesn't restamp recorded
history — verify what each row *should* say from that gameweek's live
data, tabulate it, and any drift is an unambiguous FAIL. Look for this
before inventing setup.

**It says when it genuinely can't run.** Some things no fixture reaches:
a blank gameweek that hasn't happened, a week-over-week comparison with no
prior week. Write the test anyway, mark it BLOCKED with the condition that
unblocks it, and the next plan picks it up via Step 3. A test that
silently can't run is worse than one marked BLOCKED, because it looks like
coverage.

**It covers degradation.** Missing config, corrupt store, provider down,
bad gameweek number. Make corruption tests reversible and say how to
restore (`git checkout -- <path>`) — the runner is working on real data
they want to keep.

**It names the channel.** This tool's JSON contract has moved between
stdout and stderr more than once. Where a test asserts on output, say which
stream, and have the runner capture stderr separately
(`2>/tmp/<id>-err.txt`).

**It earns its place.** Plan length tracks the range. Two releases and
twenty user-facing changes justify something like the reference plan's
~35 tests across four parts; a patch-only range with four fixes wants six
or seven. Padding a small range with smoke tests of untouched commands
buys no coverage and teaches the runner to skim.

## Step 5 — Order the parts

Group tests into parts by surface, then order the parts by **what the user
gets from running them**, not by release order:

1. Anything that produces artifacts worth having this week (a recap, a
   ledger capture) goes first, because a plan often gets run across two
   sessions and the first session should produce the thing with a deadline.
2. Health checks next (`fpl doctor`) — they surface environment problems
   that would otherwise show up as confusing failures later.
3. Everything else, grouped by surface.

Say the ordering rationale in the preamble. A runner who understands why
Part 1 is first will keep it first when the session runs short.

Number tests with a per-part letter prefix (`P1`–`P4` preflight, `R1`–`R10`
recap, `D1`–`D4` doctor…) so the Results log stays readable and a follow-up
session can reference a single test unambiguously.

## Step 6 — Write the plan

Read `references/plan-template.md` for the exact document shape, and
`references/workspace-env.md` for the environment facts that go into the
preflight and the cost/safety notes. Write the plan to:

```
/home/user/fpl-workspace/docs/test-plans/v<from>-to-<to>.md
```

Name it for the range's ends. Trim a trailing `.0` only when the two ends
differ in major.minor — `v2.2.0`→`v2.4.0` becomes `v2.2-to-v2.4.md`,
matching `v2.0-to-v2.2.md`. When both ends sit in the same minor, keep
full patch versions on both, or the name reads as a range it isn't
(`v2.3.0`→`v2.3.2` is `v2.3.0-to-v2.3.2.md`, never `v2.3-to-v2.3.2.md`).

Pre-seed the Results log with one empty row per test ID. It is the runner's
worksheet and the next plan's Step 3 input, so every ID needs a row waiting
for it.

Then add a **coverage map**: every in-range PR number against the test IDs
that exercise it. Write it by walking the Step 2 inventory, not by
summarising what you already wrote — the point is to catch the change you
researched and then lost track of while grouping. A PR with no test ID
next to it is either a gap to fill or a deliberate omission to justify in
one line, and either way the runner can see it. This is cheap to produce
and the only mechanism that makes coverage auditable from the artifact
itself.

Vault convention: no blank lines before or after headings.

## Step 7 — Land it

Commit the plan in the vault on its own branch:

```bash
cd /home/user/fpl-workspace
git checkout -b claude/test-plan-v<to>
git add docs/test-plans/<file>
git commit -m "docs: add v<from> to v<to> test plan"
```

Ask before pushing — the vault is the user's own repo and they may want to
read the plan first.

Then summarise here: the range, how many tests in how many parts, which
carried forward from the last plan, and anything you had to mark BLOCKED
on season state so the user knows what the run won't cover.
