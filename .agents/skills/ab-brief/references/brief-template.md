# Brief template

The brief is written as the user, to the vault session ("tell me", "ask
me"), because the user pastes it. Fill every `<...>`; none may survive into
the printed brief. Drop a numbered step that does not apply (no dry run for
a replay alone, no replay steps for prompts alone) rather than leaving it
with "n/a". Keep the order: the vault session reads top to bottom and
starts acting partway down.

```
A/B <what is being tested, in a few words> for rossgroomio/fpl-cli PR #<N> (issue #<M>). Read .claude/scripts/recap_ab/README.md first and follow it rather than working from memory of earlier rounds.

This session tests; it does not change fpl-cli. Attach rossgroomio/fpl-cli with push access only so you can post the handback comment. Post exactly that one comment on PR #<N> and nothing else there: never push, never edit the PR body, never reply to or resolve review threads, never file issues. Fixes, false positives and anything outside this PR go into the handback as findings, even if I ask you mid-round to fix them.

Start from a fresh origin/main checkout of the vault: `git fetch origin main && git checkout -B <this session's branch> origin/main`. Nothing from an earlier round's branch should be in the tree.

The change: <short paragraph: what the PR changes in the prompt, data sections or checks, and why>.

What it should do, shape by shape (managers as Manager A, Manager B; sentences quoted in full from <report or test the shapes came from>):
1. "<sentence shape in full>" — <unchanged | treatment should write ... instead>. Regression: <what would count>.
2. ...
<Any regression that is not tied to one shape, e.g. other check codes rising, the register changing.>

Arms (full SHAs; uvx serves exactly these):
- ctl: <40-char merge-base SHA> (the PR's merge-base with main<; main has since moved on by <commits>, which are docs/tests/CI only>)
- var: <40-char head SHA> (PR #<N> head)
If the PR has moved on since this brief, use its current head as var and say so in the handback.
<Second control: corpus round <round>'s ctl prompts should be byte-identical to this ctl's; if the dry runs confirm it, pool them and say so. | No earlier round shares this control's prompts; don't pool.>

Steps:
1. Triage: `RECAP_AB_GW=<GW> .claude/scripts/recap_ab/triage.sh <ctl> <var>`. I expect: <FRESH A/B | REPLAY | both>, <retry feedback differs for <codes> | retry feedback identical>, modules changed <list>. If it says otherwise, stop and tell me why before running anything.
2. Dry runs (fresh runs only): `export RECAP_AB_ROUND=<round>`, then `dry_run.sh ctl <ctl>` and `dry_run.sh var <var>` one at a time. The prompts should differ only here: <exactly which lines of recap_system-{classic,draft} and recap_prompt-{classic,draft}, and how many>. If anything else differs, stop.
3. Runs: GW<GW>, <D> draft and <C> classic per arm<, and why classic is raised>, round `<round>`, each batch on the scratch ledger as the README says (never `--save`). <Any extra label, its spec, config and counts.>
4. Grade: `grade.py` per the README, both formats, draft as the gate<, or the gate this change needs and why>. For this change's failure shape: <how to count it, per run and per arm, and how to judge each candidate>. Report every `synthesis_*` check code per arm (first drafts and shipped), how often a retry fired and cleared, and the feel measures (words, words per sentence, figures per 100 words, bold, five-word runs, roll calls).
5. Replay (check changes only): `recheck.py manifest` over rounds <rounds>, <RECAP_AB_RECHECK_DATA: what it holds, built from where, merged with which round's recheck-data.json>, then `recheck.sh` for ctl and var and `recheck.py diff`. <Probes to include.> Expect: <which codes should change on which drafts, and that every other code reproduces>. Judge every changed draft yourself against that arm's recap data: was the sentence true or false?

Baseline: <previous round's numbers for the same measure, with their source, or "no round has measured this; ctl's own runs are the first measurement">. Better looks like: <the numbers that would mean ship>.

If anything in this brief doesn't match what you find, stop and ask me.

Landing: <fresh runs: commit corpus/<round>/ (and <extra files>), push, and open a vault PR with the arms, run counts and headline numbers in its body; the round is not done until that PR merges. | replay with no new probes or recheck data: no vault PR, and say so in the handback.> Anything naming managers stays under data/debug/; the corpus is the one tracked exception.

Out-of-scope findings and harness gaps: list them in the handback, don't fix or file them. A small harness fix the round cannot run without may ride the round's vault PR; anything bigger is a finding for me.

Numbers only in anything that leaves data/debug/. Never echo a secret (a key, a password, a token) into tool output.

Handback: when the round is done (after the vault PR is open, if there is one), write one comment to data/debug/ab-handback-<round>.md in exactly this shape:

<!-- ab-handback pr=<N> head=<the 40-char var SHA you actually ran> round=<round> -->
## A/B handback for <round>, from the fpl-workspace session

### PR body
<First-person prose in fpl-cli's create-pr style, numbers only, ready to paste into the PR body: what was tested, the arms, run counts, the headline numbers against the baseline, and any caveat in italics. No names.>

### Findings
Verdict: <ship | fix then re-run fresh | fix then replay | don't ship>
Vault PR: <link, or "none (replay with no new corpus files)">
1. Kind: <regression | check false positive | missed catch | out of scope | harness gap>. Check: <code or none>. Shape: "<the sentence in full, managers as Manager A, Manager B>". Should have: <what should have happened>. Test: <the test shape to add>.
(or "No findings.")

Then run `python3 .claude/scripts/recap_ab/handback_check.py data/debug/ab-handback-<round>.md` (follow its own usage line if it differs). It fails on any manager's full name, unique surname or unique first name, entry ID or league ID; fix the text and re-run until it exits 0, and post only then. If the script is missing from your checkout, don't post: print the comment here and tell me the check is missing. Post it once on rossgroomio/fpl-cli PR #<N>, never edit it afterwards (a correction is a second short comment through the same check), and give me the comment's link.
```
