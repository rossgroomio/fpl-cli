---
name: ab-brief
description: >
  Write the brief a fresh fpl-workspace (vault) session runs to A/B a
  league-recap prompt or editorial-check change, once its fpl-cli PR has
  been through code review, and act on the handback comment that session
  posts on the PR. Use when the user says "A/B brief", "prompt for a vault
  session", "give me a prompt for a fresh vault session to A/B this", or
  "write the brief for the A/B"; as the step the create-pr skill names
  after review for a PR touching fpl_cli/prompts/ or a data section the
  model is shown; and when a comment carrying the ab-handback marker lands
  on the PR.
model: opus
compatibility:
  claude-code: full (GitHub MCP on the web, gh locally; subscribe_pr_activity for the wake-up)
  codex: partial (no PR subscription, so the user says when the handback is posted)
  cursor: partial (no PR subscription)
  copilot: partial (no PR subscription)
---

<!-- Runs in the fpl-cli repo; its brief runs in a fresh fpl-workspace session. -->

# A/B Brief

Hand a league-recap A/B to a dedicated vault session, and close the loop
when its results come back.

A change to a prompt, a data section the model is shown, or an editorial
check is verified by the vault's harness (`.claude/scripts/recap_ab/` in
fpl-workspace) before it merges: CLAUDE.md's prompt-change rule. The work
splits across two sessions, and this skill is the seam between them:

- **The fpl-cli session owns the code.** It writes the change, takes it
  through code review, runs this skill, and makes every fix the round
  turns up.
- **The vault session owns the test.** It has the FPL credentials, LLM
  keys, real leagues and the drafts corpus, and it never changes fpl-cli.

The forward leg is copy and paste: this skill prints a brief, the user
pastes it into a fresh vault session. The return leg is a PR comment: the
vault session posts one handback comment on the fpl-cli PR, which wakes
this session because Step 6 subscribed it to the PR first.

**This session never runs the A/B or the replay, not even "just to
check".** Twice in October an fpl-cli session did the vault's work itself,
once three whole rounds and once a corpus replay the user had to stop.
That overlap is what the split exists to end: two sessions testing one PR
produce two sets of numbers and no owner for either.

Everything here is written for a public repo. The skill, the PR, its
commits and the handback carry no manager names, entry IDs, manager_keys
or league IDs; examples use Manager A, Manager B. The brief itself goes to
a private session, but write it to the same standard so a pasted fragment
can never leak.

## Step 1: The gate

Refuse to write a brief, and say exactly what is missing, unless all three
hold. A brief written early pins a SHA the review is about to move, and
the vault session then spends a round on code nobody will ship.

1. **The branch has an open PR.** `gh pr view --json number,state,headRefOid`
   locally, or the GitHub MCP `list_pull_requests` with
   `head: rossgroomio:<branch>` on the web.
2. **A code review has been posted, and every thread on it is resolved or
   replied to.** Read the threads (`pull_request_read` with
   `get_review_comments`, or `gh api graphql`). An unresolved thread whose
   last comment is a reply counts as answered. A thread deliberately left
   open as the A/B merge gate is fine: name it, because Step 5 resolves it
   when the gate clears.
3. **The head you will pin includes the review fixes.** Each fix reply
   names a commit; check every one is in the head
   (`git merge-base --is-ancestor <fix sha> <head sha>`). A reply saying
   "pushing next" with no such commit on the branch means the fix is not
   there yet.

Report the CI state on that head (`get_check_runs`, or `gh pr checks`) but
do not block on it. Briefs in October went out with CI still running and
it never mattered: the round takes longer than CI, and a red CI is a code
problem this session fixes on its own clock.

## Step 2: Classify the change

From the diff against the merge-base (Step 3), say which test the change
needs:

- **Fresh model runs** when what the model is shown, or the model itself,
  changes: either prompt (`fpl_cli/prompts/league_recap.py`'s system or
  user prompt), a data section the model is shown (what a
  `format_recap_*` function emits, or the recap data keys it reads, often
  set in `fpl_cli/cli/_league_recap_data.py` or `league_recap.py`), the
  synthesis model or its settings (`llm.synthesis` in
  `fpl_cli/config/defaults.yaml`, `fpl_cli/api/providers/`), or the retry
  loop (`fpl_cli/cli/_synthesis_retry.py`).
- **A replay of the saved drafts corpus** when the editorial checks change
  (`fpl_cli/prompts/league_recap_checks.py`). Never model runs for a check
  change alone: the corpus already holds the drafts, and a replay judges
  them in seconds where fresh runs cost an hour and add noise.
- **Both** when it does both.

Name every check whose `retry_instruction` changed or is new. A replay
shows which drafts would now retry, never how the model reads the new
wording; only fresh runs show that, so a new or reworded retry tips a
check-only change towards fresh runs too.

This is an expectation, not the verdict. The vault's `triage.sh` dry-runs
both builds and is authoritative; the brief states what you expect and
tells the vault session to stop if triage says otherwise. A disagreement
is itself a finding: either the diff does something its author did not
see, or the classification above has a gap.

## Step 3: Gather

Collect everything the vault session cannot cheaply rediscover. It starts
with no knowledge of the PR, and every fact it has to dig out of fpl-cli
is time spent and a chance to guess.

**Both arms as full 40-character SHAs.** Control is the PR's merge-base
with main, treatment is the PR head:

```bash
git fetch origin main <branch>
git merge-base origin/main origin/<branch>    # control
git rev-parse origin/<branch>                 # treatment
git log --oneline <control>..origin/main      # has main moved on?
git diff --stat <control> origin/main -- fpl_cli/
```

Copy the SHAs out of command output, never type one. One October brief
went out with placeholder SHAs, and one session typed a SHA from memory
and broke a uvx run. If main has moved on, say whether those commits are
docs, tests or CI only (an empty `fpl_cli/` diffstat). If they touch
`fpl_cli/`, stop and put it to the user: the PR should probably merge main
first, so the round tests what will ship.

**Only what you have read in full.** Every expectation in the brief about
a file, probe, earlier change or count comes from reading the whole thing,
never a preview, a diffstat or an inference. The first real brief told the
vault session that a probe wrote the forfeit once, from the probe's first
600 characters (it wrote it twice), and that an earlier PR had changed the
prompt, from a diffstat that included console code (it had not). Each cost
the round a stop. Where you could not check, write it as a question the
dry run or the corpus answers, not as an expectation.

**What the change is meant to do, per sentence shape, and what counts as
a regression.** Quote real sentence shapes in full from the reports the
issue cites or the PR's tests, with every manager name replaced by
Manager A, Manager B. Never trim a sentence into a test-shaped fragment:
one October fix brief did, and the PR's test passed while the real
sentence was still missed. For each shape, say what treatment should write
instead (or that it should be unchanged) and what would be a regression,
and name the file each shape was quoted from so the vault session can find
it. If an expectation you want sits uneasily with the PR's own rule (puns
wanted in headlines, while the prompt's "no others" covers the headline
too), put that to the user before writing the brief: left in, it becomes a
definitional call the vault session makes mid-round, and the call moves
the count. The brief that listed expected changes per shape ran fastest and cleanest
of all the October rounds, because grading became a comparison against a
written expectation instead of a judgement made from scratch.

**The baseline.** The previous round's numbers for the same measure, from
the linked issue or earlier PR bodies, numbers only. If no round has
measured it, say so and say where the first measurement will come from
(often control's own runs, or a replay of older control drafts), and do
not predict its size: the first brief expected "a clear share" of repeats
in control and the round found 1 in 10, which left the vault session
deciding whether a wrong guess was a stop. "Better" is then stated against
whatever control measures, not against a guess.

**The gameweek.** The most recently finished one per
`fpl status --format json`, unless the PR's evidence came from a specific
gameweek (an issue quoting one week's recap), in which case name that
gameweek and say why. The evidence week is where the failure is known to
show, so it is the strongest test.

**Run counts.** For fresh runs where the gate is a frequency, 40 draft and
4 to 5 classic per arm. A round of 8 draft runs per arm in October was too
small to call first time, and the re-run cost more than doing 40 once.
Draft is the gate by default because it has the most checks and the most
variance; where the change's evidence lives in classic (a classic-only
section, a failure the issue saw only in classic), raise classic and say
why. Replay rounds need no run counts.

**The round name** for `RECAP_AB_ROUND`, as `pr<N>-gw<GW>` (`pr507-gw8`).

**For a replay: which corpus rounds, and what recheck data.** List the
rounds on the vault's main (`.claude/scripts/recap_ab/corpus/`, via the
GitHub MCP `get_file_contents` on `rossgroomio/fpl-workspace` or a clone).
Read the README's "Rechecking saved drafts": a check that reads a recap
data key older snapshots lack judges those drafts on less than its build
computes, so the replay needs `RECAP_AB_RECHECK_DATA` carrying that key for
each round's gameweek and format, merged with any round's own
`recheck-data.json`. Say what the key should hold and where its value
comes from. Name the rounds whose snapshots already carry the key (a round
recorded on this PR's own build does), since the override would replace
their values. Read each probe you expect a result from in full.

The replay's control will not reproduce a round recorded on an older build
wherever a check changed in between. For each round, run
`git log --oneline <round's recorded sha>..<control> --
fpl_cli/prompts/league_recap_checks.py` and name the expected
non-reproductions with their PR, giving every SHA involved in full: the
first round's vault session met one such miss, guessed the SHA of the
commit that explained it, and broke a replay before looking it up.

**Whether a second control exists:** an earlier corpus round, at the same
gameweek, holding control model runs (dry runs and probes alone cannot be
pooled), whose control prompts are byte-identical to this control's. One
October round pooled with its predecessor this way and halved the control
runs it needed. Read what changed between the two builds in the code that
writes the prompt (`fpl_cli/prompts/league_recap.py` and the recap data it
reads), not a diffstat: `fpl_cli/cli/` also holds the console and saved
report, which the model never sees. If you cannot tell, say the dry runs
decide and leave it there. If no such round exists, say so, so the vault
session does not go looking.

## Step 4: Write the brief

Fill in `references/brief-template.md` in its order; the template carries
the wording that must survive verbatim in spirit, and the reasons stay
here so the brief stays short enough to read before the round starts.

- **The opening line** points the vault session at the harness README
  first. The harness changed after most October rounds; a session working
  from memory of an earlier round runs an older procedure.
- **The role boundary** is near the top and absolute. In two October
  rounds the vault session pushed fixes to the fpl-cli PR branch, and one
  opened an fpl-cli PR. Both times the user asked it to, which is why the
  boundary holds even against the user's own request mid-round: a fix made
  in the vault session is invisible to the fpl-cli session that owns the
  PR, which then pushes over it or reviews code it never saw.
- **A fresh `origin/main` checkout of the vault.** A leftover branch from
  an earlier round once caused git and stop-hook noise that read as a
  broken test.
- **Stop conditions at every step that has an expectation** (triage, dry
  run, anything in the brief that does not match). "If anything in this
  brief doesn't match what you find, stop and ask me" caught a missing
  corpus in one round. A brief is written from one repo about another; a
  stop costs a message, a wrong round costs an hour. Stops are for set-up
  facts (triage, dry-run diffs, corpus contents, SHAs), never for results:
  a number that differs from what the brief hoped for is the finding, and
  the brief should not predict one in the first place.
- **Landing** is spelled out because every October session needed a
  follow-up message to land its round. Never write a blanket "don't commit
  anything": one October brief did, and that round's drafts were lost
  before the next round needed them. The rule is that anything naming
  managers stays under `data/debug/`, and the corpus is the one tracked
  exception.
- **Out-of-scope findings and harness gaps are listed, not fixed.** One
  round spent about an hour building unscoped harness and kept it only
  because the user asked. A small harness fix the round cannot run
  without may ride the round's vault PR; anything bigger is a finding.
- **The handback** (below) is required in full, because in October the
  whole vault transcript, progress chatter included, was pasted back and
  the fpl-cli session had to dig the results out.

Before printing, read the brief once as the vault session will: every SHA
is 40 characters and came from command output, no `<placeholder>` or
"TBD" is left, every number has a source, every expectation comes from
something you read in full, and no manager name or ID appears anywhere in
it.

## The handback

One comment on the fpl-cli PR, posted once, after the vault PR is open if
the round has one. Its first line is the marker, an HTML comment GitHub
does not render:

```
<!-- ab-handback pr=<N> head=<40-char sha the round tested> round=<round> -->
```

Both sessions post as the same GitHub identity, and a session watching a
PR is told to skip echoes of what it posted itself. The marker is how this
session tells the handback apart from its own comments: it never writes
one, so a comment carrying it is an A/B result to act on. It also carries
the head the round actually tested, which Step 5 checks.

Then a heading, `## A/B handback for <round>, from the fpl-workspace
session`, and two blocks:

- **Block A, "PR body".** First-person prose in the create-pr skill's
  style and length budget (up to three short paragraphs, plus caveats in
  italics), numbers only, ready to go into the PR body as written.
- **Block B, "Findings".** A verdict line (`ship`, `fix then re-run
  fresh`, `fix then replay`, or `don't ship`), the vault PR link if one was
  opened, then one entry per finding: kind (regression, check false
  positive, missed catch, out of scope, harness gap); check code if any;
  the sentence shape in full with managers as Manager A, Manager B; what
  should have happened; and the test shape to add.

The comment lands in a public repo, and GitHub keeps edit history and
sends notification emails, so a name in it cannot be taken back. The vault
session writes it to a file under `data/debug/`, runs the vault's
`.claude/scripts/recap_ab/handback_check.py` over it, and posts that
exact file only on exit 0. The script reads every manager the vault's
ledger has recorded and fails (exit 1) on any spelling of a full name, any
part of one no other manager shares, an entry ID, manager_key or league
ID, and on a footballer who shares a manager's name, which has to be
reworded by club and position; there is no allow-list. Exit 2 means it
could not check, which is not a pass. If the script is missing or exits 2,
the vault session does not post; it prints the comment in chat and says
why, and the user carries it across. It never edits the comment once posted: a
correction is a second short comment, through the same check.

## Step 5: Closing the loop

When the handback lands (found by a check-in, a wake, or the user saying
it has been posted; read it from the PR's comments):

1. **Check the marker.** Its `pr` is this PR and its `head` is the head
   the brief pinned, or a later head the brief allowed ("if the PR has
   moved on, use its current head and say so"). For a later head, read
   what changed between the two: if it changes what the model sees or the
   checks, the round tested something other than what was briefed, so say
   so to the user before acting on the numbers. A comment with no marker
   is not a handback, whoever wrote it.
2. **Put Block A into the PR body**, replacing whatever caveat said the
   A/B had not run, keeping the body inside the create-pr style.
3. **Treat each Block B finding like a review comment.** Fix and push
   (`chore(review): ...` commits, per CLAUDE.md), or say on the PR why
   not, in one comment covering every finding you are not fixing. Never
   reply to the handback comment itself as if it were a thread: it is not
   one, and the vault session is not listening.
4. **Re-run Step 2 on the new head.** If the fixes change what the model
   sees, write a fresh-run brief for the new head; if they touch only the
   checks, a replay brief; either way the next round's handback comes back
   the same way, and this session stays subscribed. A verdict of
   `fix then re-run fresh` or `fix then replay` is the vault session's
   expectation, and Step 2 on the actual fixes decides. Scope the round by
   the diff, not by the finding: a system-prompt line reaches every arm,
   so a fix aimed at the phrasings arm alone still re-runs the plain
   treatment arm when it touches the system prompt (#507's first fix did).
   An earlier round's arm is reused only where its build is unchanged and
   the new dry runs show the same data sections, and a follow-up round
   whose predecessor's vault PR is still open continues on that PR's
   branch under the same round name with new labels, so both rounds are
   graded together and land together.
5. **If no fix is needed**, say the A/B gate is cleared, and resolve the
   thread left open as the gate (reply first, saying which round cleared
   it). Merging stays the user's call.
6. **Out-of-scope findings** become issues only once the user agrees: list
   them for the user with a one-line proposal each.

A `don't ship` verdict goes to the user before anything else: it may mean
the approach is wrong, which is their decision, not a fix to push.

## Step 6: Deliver

1. **Subscribe this session to the PR's activity, and arm a check-in,
   before printing.** The subscription catches the reviews and CI on any
   fixes; it has not been seen to deliver the handback. In the first real
   round the comment landed and the subscribed session did not wake: both
   sessions post as one account, and either a comment from that account is
   filtered out as the session's own, or a plain PR comment is not an event
   the subscription delivers. One miss cannot tell which, so the skill does
   not rely on the wake. Arm a `send_later` check-in about an hour after
   delivery, and on firing read the PR's comments for the marker; if none
   is there, re-arm two hours out, and stop after four empty check-ins,
   telling the user once that the next message from them will bring you
   back. Say both are set. Where either tool is missing (a local session,
   another agent), tell the user to come back here and say when the
   handback is posted.
2. **Print the brief in one fenced block** for copy and paste, with a
   fence longer than any fence inside it (`~~~~` around a brief that holds
   ```` ``` ```` blocks), so the copy comes out whole.
3. **Above the block, not inside it, say how to start the vault session:**
   a fresh session on `rossgroomio/fpl-workspace`, Opus, at xhigh effort
   for a round with fresh runs and high for a replay alone. October's
   fresh rounds ran at xhigh; a replay at high took under four minutes.

## Common mistakes

- Running triage, a dry run or a replay here "to check the brief": that
  is the overlap this skill exists to end
- A placeholder or hand-typed SHA, or a short one
- A sentence shape trimmed to the fragment a test matches
- "Don't commit anything" in place of the landing rules
- Printing the brief before subscribing to the PR and arming the check-in
- Relying on the subscription alone to deliver the handback
- An expectation built from a preview, a diffstat or a guess at a count
- Treating the handback as an echo of this session's own comment, or
  editing the PR body with the vault session's transcript instead of
  Block A
- A manager name in the skill, the brief, the PR or a commit
