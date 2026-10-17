# LLM prompt audit, October 2026 (issue #389)

Run with the `claude-api` skill's `prompt-audit` procedure on 2026-10-17 against `main` at 748b8ba, then extended the same day with the saved reports in the fpl-workspace vault. Report and proposed diff only: nothing below has been applied. Every quoted example uses the placeholder names the prompts themselves use (Alice, Bob, Manager A); no league or manager names from real recaps appear here, and every fragment quoted from a saved editorial has been chosen so that it names nobody.

## Assumptions (Step 0)

**Scope.** The six prompts the CLI sends to a model, with the code that assembles and sends them, in the order the issue lists them:

| Surface | Prompt text | Assembled and sent by |
|---|---|---|
| `fpl league-recap --summarise` | `RECAP_SYNTHESIS_SYSTEM_PROMPT`, `get_recap_synthesis_prompt()`, the `format_recap_*` helpers (`fpl_cli/prompts/league_recap.py`) | `fpl_cli/cli/league_recap.py`, `fpl_cli/cli/_synthesis_retry.py` |
| the recap retry | `get_recap_editorial_retry_prompt()`, `EDITORIAL_CHECKS[*].retry_instruction` (`fpl_cli/prompts/league_recap_checks.py`) | same |
| `fpl review` research | `REVIEW_RESEARCH_SYSTEM_PROMPT`, `get_review_research_prompt()` (`fpl_cli/prompts/review.py`) | `fpl_cli/cli/_review_summarisation.py` |
| `fpl review` synthesis | `_build_system_prompt()` and its fragments, `get_review_synthesis_prompt()`, `get_synthesis_retry_prompt()` | same |
| `fpl preview --scout` | `SCOUT_SYSTEM_PROMPT`, `build_scout_user_prompt()` (`fpl_cli/prompts/scout.py`) | `fpl_cli/agents/data/scout.py` |
| `fpl returnees --enrich` | `RETURNEE_ENRICHMENT_SYSTEM_PROMPT` and user template (`fpl_cli/prompts/returnees.py`) | `fpl_cli/cli/returnees.py` |

Request-building code in scope: `fpl_cli/api/providers/anthropic.py`, `openai_compat.py`, `perplexity.py`, `_models.py`, `_http.py`, `__init__.py`, and the `llm:` block of `fpl_cli/config/defaults.yaml`. The League History sentences the recap prompt carries are built in `fpl_cli/services/league_history_notes.py`; that module is prompt text in all but name and is in scope for the one finding that lands on it. Second, lower-priority pass: `.agents/skills/gw-prep/SKILL.md`, `.agents/skills/squad-builder/SKILL.md`, `.claude/output-styles/fpl-mate.md`. Not read, by the procedure's own rule: `.claude/settings.json`. No `.mcp.json` or `CLAUDE.local.md` exists.

**Target models.**

- Synthesis role (recap editorial, review personal analysis): `claude-sonnet-5-5`, the shipped default since #362 on 2026-10-09 and the model the user's own `settings.yaml` has pinned since the same day. The provider is a hand-rolled Messages API client over httpx, not the Anthropic SDK; that is a deliberate design (one registry covering Anthropic, Perplexity and OpenAI-compatible endpoints) and is not treated as a finding.
- Research role (review research, scout, returnee enrichment): Perplexity `sonar-pro`. A non-Anthropic model, so the audit's model-behaviour rows do not transfer. Those three prompts are audited for internal consistency, stale facts and structure only, and every finding on them is low confidence by construction.
- The three configuration files are read by Claude Code sub-agents that pin `opus` or `sonnet` aliases, so they are audited against the current Claude generation.

**Evidence.** The first pass worked from git history (the system prompt is a constant, so every version is recoverable exactly) and a synthetic user prompt built from the test fixtures. The second pass read the fpl-workspace vault: 28 saved league recaps (16 from the 2025-26 run-in, 12 from GW1 to GW6 of 2026-27, classic and draft), 22 saved reviews as a control, the history of the vault's `settings.yaml`, the recap skill that runs the commands, and the WhatsApp converter the recaps are pasted through. The vault keeps no `data/debug/` dumps (they are git-ignored), so the prompts actually sent still come from fpl-cli's history, mapped to each report by its generation date and the release current on that date.

## Summary

The recap system prompt has grown fourteenfold since April without anyone pruning it, and the saved reports show the editorial changing shape in step with it. In April the prompt was about 280 tokens: three lines of voice, seven short rules. Today it is about 3,900 tokens: the same three lines of voice, then 25 rules carrying 14 `NEVER`s, 5 `Do NOT`s and 27 lowercase nevers (about 80 negations in all). Holding the model constant across GW1 to GW5 of this season, which all ran on `claude-sonnet-5`, the prompt went from 12 rules to 22 and the editorial lost its bold labels (16 per editorial down to about 3), lost its section breaks entirely, and nearly tripled the share of ledger phrasing in its sentences. By GW6 the draft editorial reproduces seventeen five-word runs of the League History section verbatim and reads like a settlement statement: "has now gone five gameweeks without a green arrow in a row, with one gameweek not judged either way" is the data line with a name in front of it. That is the prompt working as written. Seven of its rules demand the sections' "own wording" or figures "verbatim", and a model that follows instructions literally does exactly that.

The model change is not the cause of what the issue describes, though it adds a signature of its own. The vault's `settings.yaml` has pinned the synthesis model itself since May: `claude-sonnet-4-6` for the 2025-26 recaps, `claude-sonnet-5` from 21 August, `claude-sonnet-5-5` from 9 October. The issue was filed at 15:27 on 13 October; the first GW6 recap, the only one ever generated on Sonnet 5.5, is stamped 15:47 the same day. So "the last few recaps" are GW3, GW4 and GW5, all on the same model as GW1 and GW2, and the thing that changed between them is the prompt. The 5.5 signature shows up in GW6 and in the GW6 review, whose prompt did not change: figures per hundred words roughly double and sentences shorten. One gameweek is one data point, so that is a hypothesis for the A/B below, not a result.

The rules also contain one outright contradiction. Rule 3 says cross-gameweek facts are allowed from exactly two sections, while rules 8, 11, 12 and 13 permit season fine totals and season counts that are in neither. The safe reading of a contradiction is the restrictive one, so the season colour the author wanted is the thing most likely to be dropped.

The retry prompt is not where the behaviour is coming from. It is the original user prompt again, under the same system prompt, plus one to four sentences of correction, and it fires only when a check finds a wrong claim or the provider stops at the token ceiling. What is missing is any record of how often it fires: the recap discards the attempt count, and under `--debug` it saves the first prompt rather than the one that produced the saved text, which `review` was fixed to do in #399. Nothing in the JSON payload, the report or the vault says whether an editorial is a first or second draft.

One thing the prompt never says, and should. The editorial is pasted into the league's WhatsApp group through a converter that keeps bold and italics and strips headings and rules, which is why the April format (bold labels, short lines) worked and the current wall of prose does not. One correction from the first version of this report: the GW3 classic editorial's longest paragraph, a riff about a manager being thrown out of the group chat, was added by hand by the author before the file was committed, not written by the model. It is excluded from the measurements below, and the rule against inventing events that the first version proposed on its evidence is withdrawn: no model-written invention was observed, and a prohibition against a failure that has not happened is the pattern this audit exists to remove. What the edit does show is what the author adds when the model does not: a joke.

Caching would not pay. The request is one call a week; the only repeat is the retry, and the saving per retry is about a penny against a 25 percent surcharge on the first write. No change recommended there.

Counts by group: Group 1 (dated prompt text) 12 findings, Group 2 (configuration files) 2, Group 3 (tool descriptions) not applicable (no tools are sent), Group 4 (request config and architecture) 8. Of the 22, 4 are high confidence, 12 medium, 6 low. The proposed diff carries the 16 high and medium items as separate hunks, plus one optional consolidated rewrite of the recap rules block that needs the A/B below before adoption.

## Evidence from the saved reports

**Which model and which prompt wrote each recap.** The vault's `settings.yaml` overrides fpl-cli's default, so its history, not `defaults.yaml`, dates the model: `claude-sonnet-4-6` from the config migration on 14 May (and presumably before), `claude-sonnet-5` from 21 August, `claude-sonnet-5-5` from 9 October (vault commit 731c81a, the same day as fpl-cli #362). The prompt version is the fpl-cli release current on the generation date.

| Reports | Generated | Model | Release | Rules in the prompt |
|---|---|---|---|---|
| GW31 to GW38, 2025-26, both formats | 29 Mar to 25 May | Sonnet 4.6 | pre-release to v2.0.0 | 7 to 9 |
| GW1, 2026-27 | 25 Aug | Sonnet 5 | v2.2.0 | 12 |
| GW2 | 1 Sep | Sonnet 5 | v2.3.2 | 15 |
| GW3 | 7 Sep | Sonnet 5 | v2.6.1 | 20 |
| GW4 | 15 Sep | Sonnet 5 | v2.7.1 | 22 |
| GW5 | 21 Sep | Sonnet 5 | v2.7.2 | 22 |
| GW6 | 13 Oct | Sonnet 5.5 | v2.7.4 | 25 |

**Register metrics, editorial text only.** Means per group. "Ledger phrases" counts, per hundred words, the vocabulary of the data sections and the rules (`in a row`, `not judged`, `GWn-GWm` spans, `net`, `overall`, `priority n`, `beaten`, `of the season`, `made no moves`, `submitted no claims`, `green arrow`). "Separators" is the count of `---` section breaks inside the editorial.

| Group | n | Words | Words per sentence | Figures per 100 words | Bold spans | Separators | Ledger phrases per 100 words |
|---|---|---|---|---|---|---|---|
| Sonnet 4.6, 7 to 9 rules (2025-26) | 16 | 295 | 9.0 | 4.8 | 8.2 | 3.3 | 0.5 |
| Sonnet 5, GW1 to GW2, 12 to 15 rules | 4 | 335 | 15.9 | 6.2 | 15.8 | 0 | 0.7 |
| Sonnet 5, GW3 to GW5, 20 to 22 rules (GW3 classic excluded, see Hand corrections) | 5 | 330 | 18.2 | 5.2 | 3.2 | 0 | 2.0 |
| Sonnet 5.5, GW6, 25 rules | 2 | 372 | 13.9 | 11.9 | 0 | 0 | 3.0 |

The control. The review's personal-analysis prompt did not change between the three model eras (its last edits were #334 on 8 September and the retry prompt in #399), so its section of the saved reviews isolates the model:

| Group | n | Words | Words per sentence | Figures per 100 words | Separators |
|---|---|---|---|---|---|
| Sonnet 4.6 (GW21 to GW38, 2025-26) | 17 | 414 | 15.5 | 6.2 | 1.3 |
| Sonnet 5 (GW1 to GW5) | 5 | 618 | 22.4 | 7.7 | 0 |
| Sonnet 5.5 (GW6) | 1 | 496 | 14.2 | 12.7 | 0 |

Reading the two tables together: the move from 4.6 to 5 in August lengthened sentences and removed section breaks in both surfaces, so that part of the change is the model. The loss of bold and the rise of ledger phrasing between GW2 and GW5 happened on one model while the prompt grew by ten rules, so that part is the prompt. The GW6 jump in figures per hundred words appears in both surfaces, so it is at least partly the model, with one data point behind it.

The same recap measures split by format. Separators exclude the one that closes the editorial.

| Group | Format | n | Words | Words per sentence | Figures per 100 words | Bold spans | Separators | Ledger phrases per 100 words |
|---|---|---|---|---|---|---|---|---|
| Sonnet 4.6, 7 to 9 rules (2025-26) | classic | 8 | 294 | 8.7 | 4.9 | 9.5 | 2.9 | 0.5 |
| | draft | 8 | 296 | 9.3 | 4.6 | 7.0 | 3.8 | 0.6 |
| Sonnet 5, GW1 to GW2, 12 to 15 rules | classic | 2 | 336 | 16.9 | 6.1 | 18.0 | 0 | 0.5 |
| | draft | 2 | 334 | 14.8 | 6.2 | 13.5 | 0 | 0.9 |
| Sonnet 5, GW3 to GW5, 20 to 22 rules | classic (GW3 excluded) | 2 | 336 | 20.4 | 5.7 | 3.5 | 0 | 1.2 |
| | draft | 3 | 327 | 16.7 | 5.0 | 3.0 | 0 | 2.5 |
| Sonnet 5.5, GW6, 25 rules | classic | 1 | 359 | 14.4 | 10.6 | 0 | 0 | 2.8 |
| | draft | 1 | 385 | 13.3 | 13.2 | 0 | 0 | 3.1 |

The two formats move together on length, sentence length and bold, and apart on register: ledger phrasing per hundred words is higher in draft in every era and double classic's over GW3 to GW5. Draft is also the format the issue's author was reading when #389 was filed. The pooled means above understate that, which is why the A/B in "Verification" is graded per format.

**Verbatim copying.** For each 2026-27 recap, the five-word runs that appear both in the editorial and in a League History line of the same report: GW1 none (no history yet), GW2 classic 7, GW3 draft 8, GW4 draft 8, GW5 draft 6, GW6 classic 5, GW6 draft 17. The 2025-26 recaps have no League History section and copy nothing. Split by format the copying is a draft phenomenon: over GW3 to GW5 the three draft editorials copied 8, 8 and 6 runs and the two classic ones none, and at GW6 17 against 5. Section length explains only part of it. At GW4 and GW5 the draft League History section ran to 71 and 88 words against classic's 14 and 29, but at GW6 the two were level (63 and 66 words) and the draft editorial still copied three times as much. Draft has no captains, chips or transfers to write about, so its history lines are the largest block of quotable prose in the prompt; that is the likeliest reason, and it is a hypothesis. Vocabulary that exists only in the prompt's rules and the roster lines starts appearing in prose at GW5 ("net overall", "first of the season", "pipped") and multiplies at GW6 ("made no moves and submitted no claims", "beaten to him", "not judged"). The GW6 draft editorial contains the sentence "has 3 waiver hauls in the last 4 (GW3-GW6), with no completed moves in 1" with only a name changed from the League History line that fed it. Rule 3 asks for the section's "own wording for counts, spans, and holds" and rule 7 for an observed count to be "repeated that way"; the model is complying.

**Length.** The "300-400 words max" cap is honoured in every era: 264 to 322 words on Sonnet 4.6, 314 to 356 on Sonnet 5 (the GW3 classic's 482 includes a hand-written paragraph), 359 and 385 on 5.5. The shortest editorials were the ones written under the smallest prompt, on the oldest model, so the cap's own effect cannot be separated out here.

**What the three eras read like.** The spring editorials open on a bold one-line hook, run four or five short bold-labelled sections separated by rules ("The Summit", "The Chip Graveyard", "The Bench Crime of the Week", "The Fine"), use fragments for effect ("Five. Points. Tripled."), address managers directly, and close with a sign-off line. GW1 to GW3 of this season keep the voice but as continuous prose: bold names, long sentences, no sections. GW4 and GW5 keep a headline and a hook and then account for the week's rosters, with phrases such as "nine managers moved players, ten sat still" and "net +6 across three moves". GW6 is accountancy with a headline: nearly every sentence carries two figures and a span in brackets, streak lines are reproduced whole, and the one joke in each editorial is in its last paragraph.

**Hand corrections.** Two kinds are visible. GW2's two recaps were corrected after their commit and say so in a note at the top (fpl-cli #187, #188, #189). GW5's two recaps were corrected before their commit: both are stamped 16:33 and 16:34 on 21 September, issues #357 and #359 were filed at 18:20 and 18:30 quoting errors from those editorials, and the files committed at 20:01 do not contain them. GW3's classic recap was also edited before its commit, in the other direction: the author added its longest paragraph, a riff about a manager being removed from the group chat (confirmed by the author after the first version of this report misread it as a model invention). So pre-commit edits run both ways, corrections and additions, how many other editorials were touched cannot be seen, and GW3 classic is left out of every measurement above.

**Where the editorial goes.** The vault's `convert-md-to-whatsapp` skill and its 14 April brainstorm document record that each recap is pasted into the league's WhatsApp group. The converter keeps `**bold**` and `*italic*` (as WhatsApp's own markers), turns headings into bold, deletes `---` rules and the generated-by footer, and passes pipe tables through with a warning because WhatsApp cannot render them. The spring editorials' bold labels and short paragraphs survive that conversion as a readable chat message; a 380-word block of fifteen-word sentences does not. The prompt never mentions the medium.

## The issue's four questions

**Has the recap system prompt picked up rules that contradict each other or move the tone?** Yes to both, and the second is now observed rather than inferred. The contradiction is rule 3 against rules 8, 11, 12 and 13 (finding 1). The tone shift tracks the prompt on a fixed model: between GW2 and GW5, all on Sonnet 5, the rules went from 15 to 22, bold labels went from 16 per editorial to about 3, and ledger phrasing nearly tripled (the metrics above). Two mechanisms are at work. The register of a prompt becomes the register of its output, and this one is 90 percent prohibition (finding 3). And seven rules ask for the data sections' own wording or verbatim figures, so the editorial quotes the ledger's bureaucratic sentences instead of writing its own (finding 20). The quoting is concentrated in the draft recaps (the per-format table and the copying counts above). The `<context>` and `<tone>` blocks are almost word for word the April text; the voice instruction has not changed, it has been buried. Rules 20 and 21, and 12 and 13, are patch pairs where the second was added on top of the first after another incident, which is the "patch accretion" pattern the procedure names.

**How much of the editorial now comes from the retry, and are retries firing more than we think?** Little, and unknowable, respectively. The retry sends the full original user prompt again with feedback appended and the instruction "Write the recap again from scratch", under an unchanged system prompt (`league_recap_checks.py:1245-1275`). It fires on a failed check or a `max_tokens` stop and never on a refusal (`_synthesis_retry.py:116-141`). So a retried editorial is a fresh draft with up to four extra sentences of instruction, not a different prompt regime. But `league_recap.py:920` throws the attempt count away, `league_recap.py:1065-1067` saves the first prompt under `--debug`, and neither the JSON metadata nor the report callout records that a retry happened. Only a stderr line at the time says so, and the vault does not keep stderr. Of the saved recaps only GW6 was generated after #368 made retries possible, and nothing in its files says which draft it is. Since #396 a `max_tokens` stop also earns a retry, and with adaptive thinking sharing the 8,000-token ceiling at the default `high` effort, the longer the rules block the longer the thinking, so retries plausibly are more frequent than before. There is no data either way. Finding 14 proposes recording it.

**Do the prompts suit `claude-sonnet-5-5`?** Mostly, and the model is not what changed the recaps the issue describes: GW3 to GW5 were written on Sonnet 5, the same model as GW1 and GW2. Three caveats from the migration notes the skill ships still apply. The notes say Sonnet 5 prompts should perform well unchanged, that the model holds a system-prompt role more reliably and follows instructions more literally (which is what finding 20 shows), and that its effort levels are recalibrated with the default left at `high`, so an effort should be set explicitly and swept rather than inherited; the CLI sets none (finding 10). It also declines in more categories, including a `general_harms` category that benign work can trigger; a prompt built around roasting named people is the kind of text that could, though no refusal has been reported and #399's refusal handling was written from the documentation, not from an observed refusal. The one GW6 data point suggests 5.5 packs more figures into shorter sentences than Sonnet 5 did, in the review as much as the recap. The failure class the rules target (wrong tie, wrong role, wrong net) reproduced on 5.5 in #390, so the model change neither caused nor cured it.

**The usual items.** The system/user split is right: stable rules in `system`, per-run data in the user turn, with the data sections carrying short embedded instructions that repeat a system rule (previous leader, player clubs, fine placements). Those repeats agree with the rules they echo, and the procedure's keep list leaves agreeing redundancy alone, so they are noted (finding 19) but not proposed for removal. Examples are few and inline ("Bob's fourth last-place of the season"), which is the right amount. The output-format instruction is paired with code that enforces it (`normalise_recap_editorial`), which is the ideal shape. What the prompt lacks is context only the author has: the medium (finding 21) and the one principle behind all 25 rules, which hunk R states in its opening sentence. Caching: see finding 16.

## Inventory (Step 1)

Model-call sites in `fpl_cli/`, found by grepping for `.query(`:

| Call site | Role | Provider by default | Shape |
|---|---|---|---|
| `fpl_cli/cli/_synthesis_retry.py:101` (shared by recap and review synthesis) | synthesis | Anthropic `claude-sonnet-5-5` | system + one user turn, retried at most once |
| `fpl_cli/cli/_review_summarisation.py:1082` | research | Perplexity `sonar-pro` | system + one user turn |
| `fpl_cli/agents/data/scout.py:190` | research | Perplexity `sonar-pro` | system + one user turn |
| `fpl_cli/cli/returnees.py:629` | research | Perplexity `sonar-pro` | system + one user turn per shortlisted player |

No tools, no prefill, no stop sequences, no sampling parameters, no beta headers. The Anthropic request (`anthropic.py:66-78`) is `model`, `max_tokens`, `messages`, `system`, with header `anthropic-version: 2023-06-01`. Nothing else in `query_defaults` reaches the payload: only `max_tokens` is read. Every call is single-shot, so the preserved-thinking and history-editing rules do not apply.

Measured sizes (characters divided by 3.9, so roughly tokens):

| Prompt | Size | Bullets | Caps emphasis | Negations |
|---|---|---|---|---|
| recap synthesis system | ~3,900 | 34 | 19 | 81 |
| recap user prompt, synthetic 10-manager classic league, no history, no player clubs | ~600 | | | |
| review synthesis system (fines on, fixtures on) | ~3,300 | 46 | 15 | 37 |
| review research system | ~1,700 | 28 | 5 | 17 |
| scout system | ~700 | 17 | 4 | 5 |
| scout user template, before the player reference is filled | ~1,100 | 11 | 7 | 12 |
| returnee enrichment system | ~500 | 7 | 2 | 5 |

A real recap user prompt is larger than the synthetic one: the `## Player Clubs` section alone is one line per distinct player across every squad, and the history, fines and prior-seasons sections add more. A 10-manager league with a full season of history is plausibly 2,000 to 3,000 tokens, so the system prompt is the larger half of what the model reads.

## Provenance (Step 2)

The recap system prompt, version by version. Every rule traces to one incident, which is the signature of accretion. Sizes are approximate tokens.

| Date | Commit | Rules | Size | What changed |
|---|---|---|---|---|
| 2026-04-02 | 3055441 | 7 | 280 | Initial: voice, no advice, this gameweek only, fines, bench haul, bench-outscored, names |
| 2026-04-12 to 05-09 | 245a73e, 069a1c1, 0793e6b, 33915e7 (#32) | 9 | 550 | Chips rule, then "use the Chips Played count"; captains locked to the Captains section |
| 2026-08-22 | afe4bc6 | 10 | 650 | Transfers only from awards or a note |
| 2026-08-23 | 59dddde (#77) | 11 | 910 | League History as the only source of history; season-phase framing added to tone |
| 2026-08-25 | edca571 (#152) | 12 | | Player clubs only from the data |
| 2026-08-26 | a9ea953 (#165), 8b552d1 (#167) | 15 | | Season Fines as optional colour, no adding up; season-count lines |
| 2026-09-04 | f01987c (#216), a7f2242 (#241) | 18 | 1,610 | One manager per entry, no shared "club"; previous leader must match the statement; never "second" fine |
| 2026-09-06 to 09-08 | 2531e26 (#300), f91a1ff (#303), af85102 (#304), d4f1c66 (#334), 47fbc49 (#341) | 22 | 2,550 to 3,000 | Transfers roster, waivers roster, prior seasons, waiver-is-a-competition, contested lines |
| 2026-09-16 | 60cafde (#351) | 22 | 3,190 | `<output_format>` block: headline not title |
| 2026-10-09 | 272cb2d (#368), b5862de (#374) | 25 | 3,790 | One race per clause (#357); tie words (#359); two kinds of figure (#359) |
| 2026-10-09 | 2e20def (#362) | | | Synthesis default `claude-sonnet-5` to `claude-sonnet-5-5`, unverified live; the vault pinned the same model the same day |
| 2026-10-14 | f187311 (#392) | 25 | 3,890 | Per-kind nets added to the two-kinds-of-figure rule (#390) |

Two observations from the history. First, from September the prompt and the data sections were extended together under the "enumerate and lock" approach (give the model the sentence, not the sum), and that approach is sound: the rules that work are the ones with a data line to point at. Second, from #368 onward each new failure also got a mechanical check and a retry, so the newest rules (6, 21, 22) are the ones with a code path behind them, which is exactly where the prose can be shortest.

The review synthesis prompt last changed in #334 (2026-09-08) and #399 (2026-10-14, retry prompt only); the research, scout and returnee prompts have not changed since the repository's September flattening. That is what makes `review` a usable control for the model eras above.

## Findings (Step 5)

Ordered by confidence, then by surface. "Line" references are to the files at 748b8ba.

### High

**1. Rule 3 contradicts rules 8, 11, 12 and 13.** `fpl_cli/prompts/league_recap.py:57` against `:62`, `:65-67`. Evidence: rule 3 says "Stick to what happened this gameweek, with two exceptions" (League History and Prior Seasons) and "A streak, trend, or season-arc fact not listed there is forbidden to mention, however obvious it might seem". Rule 11 says of the Season Fines section "Use it when a season total sharpens what already happened this gameweek", rule 13 tells the model to repeat the Fines section's "their first of the season" placements, and rule 8 allows League History season-count lines "to ground a season retrospective". A season fine total is a season-arc fact in neither of rule 3's two sections. Pattern: Group 2, instruction text that contradicts itself (the procedure's conflict row, applied within one file). Why it matters: Sonnet 5.5 follows instructions more literally than the models these rules were written against, and the safe reading of a contradiction is the restrictive one, so the season colour the author wanted is the thing most likely to be dropped. Confidence high: the text contradicts the text. Action: rewrite rule 3 to name every section that may supply a cross-gameweek fact (hunk 1).

**2. Retry telemetry drift between recap and review.** `fpl_cli/cli/league_recap.py:920` (`best, _ = await synthesise_with_retry(...)`), `:1065-1067` (`--debug` writes `user_prompt`, the first attempt's prompt), against `fpl_cli/cli/_review_summarisation.py:1284` (writes `shipped.prompt`, the prompt that produced the saved text, per #397 and #399). Pattern: Group 4, no accounting. Why it matters: the issue's second question cannot be answered from any artefact the recap produces, and the vault confirms it: nothing in a saved recap says which draft it is. Confidence high: the two call sites visibly differ and the review-side change was made deliberately in #399. Action: save the shipped attempt's prompt, keep the attempt count, and expose both (hunk 12).

**3. Prohibition register.** `league_recap.py:55-79`. Evidence: 14 `NEVER`, 5 `Do NOT`, 27 lowercase `never`, 2 `ONE`/`AND` emphases, against three lines of positive voice guidance at `:40-43`. Pattern: Group 1a, pressure language; "when several instructions are each marked critical, the markers stop carrying information, and the prompt's register becomes the output's register". Observed: on one model, the editorials written under 12 to 15 rules carried about 16 bold spans and a hook, the ones under 20 to 22 rules carried about 3 and read as an account of the week (Evidence section). Confidence high, upgraded from medium on the saved reports. Action: lowercase the emphasis and keep each rule's reason (hunk 2); the structural fix is hunk R.

**20. "Own wording" rules import the ledger register.** `league_recap.py:57` ("stated using that section's own wording for counts, spans, and holds"), `:61` ("must be repeated that way, never simplified"), `:62` ("Take the count verbatim, repeat its 'not judged' qualifier alongside it"), `:63` ("repeated verbatim"), `:66`, `:70`, `:71`, `:72` ("use those counts verbatim"); the sentences being quoted are built in `fpl_cli/services/league_history_notes.py:388` and `:523-535` (the "in a row", "this season", "the latest this gameweek" and "not judged either way" shapes). Evidence: the GW6 draft editorial reproduces seventeen five-word runs of its League History section and the sentence "has 3 waiver hauls in the last 4 (GW3-GW6), with no completed moves in 1"; the GW6 classic reproduces "five gameweeks without a green arrow in a row, with one gameweek not judged either way"; the 2025-26 recaps copy nothing because the section did not exist; split by format, GW3 to GW5 draft copied 8, 8 and 6 runs against none in classic, and ledger phrasing per hundred words runs higher in draft in every era (2.5 against 1.2 over GW3 to GW5). Pattern: Group 1c, over-specification ("describe the goal, not the method"): the goal is that counts, spans and qualifiers keep their meaning (#188, #216), and the method the rules chose is to repeat the sentence. Why it matters: the League History lines are written for a report table, in a register no columnist would use, and the rules make the editorial quote them. Confidence high: the copying is in the saved reports and the rules ask for it. Action: keep the figures, spans and qualifiers exact and free the phrasing (hunk 15), and give the notes service sentences a reader could lift (same hunk, second part).

### Medium

**4. Numeric length cap.** `league_recap.py:45`, "Brief - 300-400 words max". Pattern: Group 1b and 1f, hard word caps "tuned against an older model's verbosity"; the procedure asks for qualitative length guidance. The saved reports show the cap honoured in every era (264 to 385 words), so it is doing no visible harm, and this is the hunk most easily declined. It is also not what protects against the cut-off editorials in #372 and #396: 400 words is about 550 output tokens against an 8,000 ceiling, and the cut-offs come from thinking consuming the ceiling, not prose. Confidence medium. Action: rewrite as audience framing (hunk 3). This is a product call as much as a model one; the hunk can be declined if a hard length is wanted.

**5. Patch pair: contested lines (rules 20 and 21).** `league_recap.py:74-75`. Evidence: rule 20 (#341) describes the Contested players lines; rule 21 (#368) was added after #357 to say each line is one race and to ban three phrasings of the same fold. Pattern: Group 1d, patch accretion; the fold is also now caught mechanically by `check_contested_attributions`, so the prose is belt and braces. Confidence medium. Action: merge into one rule that states the principle once, keeps the one observed phrasing as the example, and keeps the reason (hunk 4).

**6. Patch pair: fines counting (rules 12 and 13).** `league_recap.py:66-67`. Evidence: rule 12 (#165) says take Season Fines numbers verbatim and never add up; rule 13 (#241) spends 180 tokens on the arithmetic of "second", then the Fines section was given explicit placement sentences (#233) that settle the question in the data. Pattern: Group 1d, patch accretion, and the "give the model the sentence, not the sum" fix has since landed in the user prompt. Confidence medium. Action: merge to one rule that defers to the placement sentences (hunk 5).

**7. Review synthesis: the same instruction three times.** `fpl_cli/prompts/review.py:332` (hard constraint), `:439` (Classic Selection) and `:444` (Draft Selection) each carry the aggregate-versus-per-player comparison rule in full; `:378`, `:437` and `:442` each carry "only players who actually contributed, not bench". Pattern: Group 1c, repetition as reinforcement; "duplicated rules make the model spend effort reconciling wordings; bulk also directly inflates adaptive-thinking spend". Confidence medium. Action: keep each once, in the hard constraints, and leave the Selection placeholders to say what the section is for (hunk 6).

**8. Review synthesis: sentence caps and "EXACTLY".** `review.py:401` ("Structure your response EXACTLY as follows"), `:404`, `:437`, `:442` ("2-3 sentences"), `:459`, `:462` ("1-2 sentences"). Pattern: Group 1f, numeric output ceilings, and 1a emphasis. The section headings are what `required_synthesis_sections()` reads, so the headings must stay; the sentence counts are not read by anything, and the saved reviews ignore them (the analysis section averaged 618 words on Sonnet 5 against a prompt that adds up to about ten sentences). Confidence medium. Action: keep the headings, replace the counts with qualitative guidance, lower "EXACTLY" to "in this order" (hunk 7).

**9. Review synthesis: GW1 stated three times.** `review.py:481` (`_EDGE_CASES_OPENING_GAMEWEEK`), `:487-496` (`_SEASON_CONTEXT_OPENING_GAMEWEEK` in the user prompt) and `_review_summarisation.py:630-634` (the transfers string). Pattern: Group 1c, repetition. The user-prompt `<season_context>` block is the fullest and sits beside the data; the system-prompt edge case adds only "Judge the squad as a pre-season build". Confidence medium. Action: drop the system-prompt duplicate, keep the user-prompt block and the data string (hunk 8).

**10. No explicit effort on the synthesis role.** `anthropic.py:64-72` builds the payload from `model`, `max_tokens`, `messages`, `system` only; `defaults.yaml:46-51` carries `max_tokens: 8000`. Pattern: Group 4, thinking config sized for the wrong model. Why: the migration notes for Sonnet 5.5 say the effort levels are recalibrated and the default stays `high`, so a prompt inherits more or less thinking than it had on Sonnet 5 with no way to tell, and the recommended first move is to set `output_config.effort` explicitly and sweep it. Effort is also the only lever on the #372 and #316 truncation shape (thinking shares the ceiling) that does not fight the 60-second non-streaming timeout, which #316 found 16,000 tokens exceeds. If a user points the synthesis role at `claude-opus-5-5` through `FPL_SYNTHESIS_MODEL`, the implicit default silently becomes `medium`. Confidence medium. Action: pass `output_config` through from `query_defaults` and pin `effort: high` explicitly in `defaults.yaml` with a note to sweep (hunk 9). Lowering the level is not proposed without a measurement.

**11. Provider fallback `max_tokens` of 1,024.** `anthropic.py:64`, `int(merged.get("max_tokens", 1024))`. Pattern: Group 4, `max_tokens` sized for a thinking-off route. Why: on a model with adaptive thinking, 1,024 is used up by thinking and the response arrives with no text block, which is the exact failure #316 documented at 4,096. It is reachable: `defaults.yaml` gives only the synthesis role a `max_tokens`, so `FPL_RESEARCH_PROVIDER=anthropic` (a documented override) runs the review research and scout prompts at 1,024. Confidence medium. Action: raise the fallback to the shipped synthesis value (hunk 10).

**12. Refusal category not read.** `anthropic.py:117-127` reads `stop_reason` and nothing else; `_models.py:LLMResponse` has no field for it. Pattern: Group 4, API fossil by omission. Why: Sonnet 5.5 returns `stop_details.category` on every refusal (`cyber`, `bio`, `frontier_llm`, `reasoning_extraction`, `general_harms`), and that word is the difference between "the roast tripped `general_harms`" and "the request matched a cyber classifier by accident". Today the warning, the JSON `synthesis_stopped_early` entry and the report callout can only say "refused". Confidence medium. Action: carry the category through `LLMResponse` into `log_abnormal_stop` and the callouts (hunk 11).

**13. Output style narrates its own history.** `.claude/output-styles/fpl-mate.md:57` ("As of v2.3.0, a command failure under `--format json` is..."), `:133` ("The ceilings were recalibrated in v2.2.0. Don't anchor on absolute bands you remember from an older run"), `:137` ("GK ceilings ramp in with the calendar through GW6 (v2.3.0). Before that fix an elite, ever-present keeper's score was capped..."). Pattern: Group 2, history narratives (past tense, version pins). Why: an output style loads every session; the rule's authority is the behaviour it prescribes, and "before that fix" describes a CLI nobody runs. Confidence medium. Action: rewrite each as the present-tense rule (hunk 14).

**14. No record of attempts or token usage.** `league_recap.py:920`, `_review_summarisation.py:1236-1242`, both providers' `usage` fields are read into `LLMResponse` and then dropped by every caller except `ScoutAgent`. Pattern: Group 4, no token accounting. Why: the procedure's own rule is that without per-surface visibility every other issue here is invisible, and the issue's "more often than we think" is unanswerable today. Confidence medium. Action: with hunk 12, add `synthesis_attempts` and the two token counts to the JSON metadata and the `--debug` bundle (hunk 13).

**21. The prompt never says where the editorial is read.** `league_recap.py:35-37` (`<context>`). Evidence: the vault pastes every recap into the league's WhatsApp group through a converter that keeps bold and italics, turns headings into bold, and deletes rules and tables; the spring editorials' bold labels and short paragraphs survive that as a readable chat message, the current 380-word block of fifteen-word sentences does not, and the April brainstorm document records the standings table as the one part that did not convert. Pattern: the keep list's item 11 (re-baselining adds text) and item 1 (the audience and medium are context only the author has). Why it matters: told the medium, a model formats for it without being told how; untold, it defaults to the long-form prose the rest of the prompt's register suggests. Confidence medium: the mechanism is documented, the effect on this prompt is untested. Action: add one sentence of context (hunk 16).

### Low (report only, no diff)

**15. Research, scout and returnee prompts (Perplexity).** `review.py:58`, `:71` (`ALWAYS:` and `NEVER:` lists), `:288-300` (an `IMPORTANT:` block with three `MUST ONLY`s in the user prompt), `scout.py:93` ("CRITICAL: The following players are INJURED, SUSPENDED..."), `:134-139` (two `CRITICAL:` quality requirements repeating rules the system prompt already states), `scout.py:33-39` (named accounts with credentials such as "consistent top 50k finisher", which nothing verifies and which age with every season). Pattern: Group 1a and 1c signals, Group 2 volatile specifics. These prompts run on `sonar-pro`, where the audit's behaviour rows do not apply, so this is idiom-dating only. The research prompt's table row floors and ceilings ("3-5 players drawn EXCLUSIVELY from the Dream Team list") are format-pinning for a format-sensitive output and are kept under the procedure's keep list; `ensure_top_performer_first()` and `validate_research_teams()` enforce the rest in code. Flag.

**16. Prompt caching.** Not a defect, recorded because the issue asked. Sonnet 5.5's minimum cacheable prefix is 512 tokens, so the ~3,900-token recap system prompt qualifies. But a recap is one request per gameweek, and the only repeat inside the five-minute window is the retry. A cache write costs 1.25 times the input rate; a retry that reads the system prompt and the whole original user prompt from cache saves roughly 0.9 times those tokens at $2 per million, about a penny, and only on runs that retry. Net expected saving is negative or negligible. No change.

**17. Em dashes.** `league_recap.py:69-70` carry three em dashes from the April text while every later rule uses spaced hyphens. Prompt format bleeds into output format, and the house style (`fpl-mate.md:17`) is hyphens; the saved editorials carry five or six em dashes each on Sonnet 4.6 and Sonnet 5 and none on 5.5. Idiom only. Flag; fold into hunk 2 if that hunk is taken.

**18. Dead parameter.** `league_recap.py:90`, `:166-167`: `get_recap_synthesis_prompt(research_summary=...)` appends a `## GW Context (from research)` section, but no caller passes it (`grep research_summary fpl_cli/cli/league_recap.py` finds nothing). Harmless to the model; a maintenance trap. Flag; remove in a tidy-up commit after grepping the tests.

**19. Instructions embedded in data sections.** `league_recap.py:316-337` (previous-leader statement carries "never attribute a prior top spot to anyone else"), `:777-779` (Player Clubs preamble), `:911-914` and `:925-928` (Prior Seasons preamble), `:1036-1039` (fine placements preamble), `:116-118` (BGW and DGW notes). Each repeats a system rule beside the data it governs, by design ("pinned here as well as in the system prompt because the section is what the model quotes from"). They agree with the rules they echo, and the procedure leaves agreeing redundancy alone. Flag only; if hunk R is taken, re-read these against the shorter rules and keep them.

**22. Model-era shifts, for the record.** From the review control: Sonnet 5 wrote sentences about 45 percent longer than Sonnet 4.6 and dropped the section breaks and most em dashes, with no prompt change; Sonnet 5.5, on one gameweek, doubled figures per hundred words and shortened sentences again. Nothing in prose patches a model's cadence, and the procedure's guidance is to re-baseline rather than add instructions; the effort sweep in hunk 9 is the one lever worth pulling, and the WhatsApp context in hunk 16 is the one piece of missing information. Flag.

### Checked and clean

- `anthropic-version: 2023-06-01` is the current header value; no sampling parameters, prefill, stop sequences, forced tool use or beta headers anywhere (Group 4 API fossils: none).
- Server-side refusal fallback was considered and rejected: it retries only `cyber` and `frontier_llm` declines, which are not the categories an FPL recap could plausibly trip, and #399's "never retry a refusal" stands.
- Every CLI command and flag the three configuration files name exists in `fpl_cli/cli/` (173 flag mentions checked by script against the click option literals; the only non-local hits are `--format`, which lives in the shared `output_format_option` decorator). Every script and reference path the skills name exists. The model and effort pins are aliases, not versions, and `tests/test_skill_effort_levels.py` holds them to `.agents/README.md`.
- The three copies of the "`ep_next` tracks `form` before GW6" note (`gw-prep/SKILL.md:240`, `squad-builder/SKILL.md:148`, `fpl-mate.md:136`) agree with each other; left alone under the keep list.
- The four model-call sites are each genuine judgement or research work; none is a deterministic step wearing an LLM (Group 4 executor check: none to remove).
- `<output_format>` in the recap prompt is paired with `normalise_recap_editorial()`, and the review's `<output_format>` headings are read back by `required_synthesis_sections()`: both are instruction-plus-enforcement, the shape the procedure recommends. The saved GW5 and GW6 recaps all open on a `## ` headline beneath the report's own title, so #351 did what it set out to do.

## Proposed diff (Step 6)

One finding per hunk. Each names the tests whose string assertions it touches; the replacement assertion is given where the string moves. Nothing here is applied.

### Hunk 1 (finding 1): rule 3 names every section that may supply a cross-gameweek fact

`fpl_cli/prompts/league_recap.py:57`

```diff
-- Stick to what happened this gameweek, with two exceptions: a historical claim (a streak, trend, or season-arc fact spanning more than this gameweek) is permitted only when it appears in the "## League History" section, stated using that section's own wording for counts, spans, and holds; and a manager's FPL seasons before this one are permitted only as the "## Prior Seasons" rule below allows. A streak, trend, or season-arc fact not listed there is forbidden to mention, however obvious it might seem. Do NOT infer history from the Awards or GW Standings sections - they are compressed and can misrepresent what actually happened over time
+- Stick to what happened this gameweek. The only facts spanning more than this gameweek you may state are the ones a section hands you, with its counts, spans and qualifiers kept exactly as it states them: streaks and season counts from the "## League History" section, a manager's FPL seasons before this one as the "## Prior Seasons" rule below allows, season fine totals from the "## Season Fines" section, and where this gameweek's fine sits in a manager's season from the "## Fines" section. A streak, trend, total or season-arc fact none of them states is not yours to mention, however obvious it looks. The Awards and GW Standings sections describe one gameweek and are never a source of history
```

Tests: `tests/test_league_recap.py:4297` asserts `"Stick to what happened this gameweek, with two exceptions"`; change to `"Stick to what happened this gameweek. The only facts spanning more than this gameweek"`. The `'"## League History" section'` and `'"## Prior Seasons" rule below'` assertions on the next two lines still hold.

### Hunk 2 (finding 3): lowercase the emphasis, keep the reasons

`fpl_cli/prompts/league_recap.py:55-79`. Mechanical: replace each `NEVER` with `never` and each `Do NOT` with `do not` in the `<rules>` block, replace `ONE named manager` with `one named manager` and `made no moves AND submitted no claims` with `made no moves and submitted no claims`, and replace the three em dashes at `:69-70` with ` - `. Where a rule had no reason, add the one its issue recorded; the two that lack one are:

```diff
-- NEVER give advice or recommendations. This is a recap, not a preview
-- NEVER speculate about future gameweeks
+- This is a recap, not a preview: no advice, no recommendations, and nothing about future gameweeks
```

```diff
-- NEVER alter player or manager names. Use the exact spelling provided in the data.
+- Spell every player and manager exactly as the data does: the league knows its own names, and a misspelling reads as a mistake about the person
```

Tests touching exact caps strings: `tests/test_league_recap.py:4000` (`"Do NOT infer waiver activity from the Awards section"`), `:4059` (`"Do NOT infer transfer activity from the Awards section"`), `:4068` (`"NEVER state a club for a player other than the club given for them"`), `:6957` (`"NEVER fold two lines into one clause"`). Each becomes its lowercase form. `tests/test_league_recap.py:3615` (`"verbatim" in ...lower()`) is unaffected.

### Hunk 3 (finding 4): qualitative length

`fpl_cli/prompts/league_recap.py:45`

```diff
-- Brief - 300-400 words max. Punchy paragraphs, not walls of text
+- Short: a column, not a report. Readers skim it on their phones between matches, so say the few things worth saying this week and stop. Punchy paragraphs, not walls of text
```

The retry's "Keep it to the length asked for and finish it." (`league_recap_checks.py:1273`) still reads correctly against this wording; `tests/test_league_recap.py:6939` is unaffected.

### Hunk 4 (finding 5): one contested-lines rule

`fpl_cli/prompts/league_recap.py:74-75`, replacing both rules:

```diff
-- The "Contested players" lines at the end of that section are the only source for who else wanted a player. Each names one player, how many managers claimed him, who won him and who was beaten to him, with the priority each beaten manager gave the claim - their own ranking of the claims they submitted that week, not the league's waiver order. Use the count verbatim; never call a player contested, "in demand" or "wanted by half the league" unless a line lists him, and never say a manager wanted, chased or missed out on a player unless the line names them. Where the beaten managers on a line carry no (free agent) or (other move) tag, it was a waiver race and the winner won by standing higher in the league's waiver order - you may say that, and nothing else about anyone's waiver position, which the data does not state. A line whose beaten managers are tagged (free agent) was first-come-first-served, so say nothing about waiver order there at all. Where a line says the winner could not be identified, name nobody as having won him.
-- Each "Contested players" line is one race, and its roles are fixed: the manager it says won him won him, and only the managers it says were beaten to him were beaten to him. Write every race as its own clause, naming its winner and beaten managers in exactly those roles. NEVER fold two lines into one clause - no "respectively", no "X and Y were beaten to A and B", no list of managers set against a list of players - because a manager who won one race and lost the next is then easily put in the wrong role. One beaten manager may be named once against several players ("beaten to both A and B") only when every one of those lines names them as beaten.
+- The "Contested players" lines at the end of that section are the only source for who else wanted a player: each names one player, how many managers claimed him, who won him and who was beaten to him, with the priority each beaten manager gave the claim (their own ranking of the claims they submitted that week, not the league's waiver order). A player is contested, "in demand" or "wanted by half the league" only when a line lists him, and a manager wanted, chased or missed out on him only when the line names them. Each "Contested players" line is one race with fixed roles, so write every race as its own clause naming its winner and its beaten managers, and never fold two lines into one clause ("X and Y were beaten to A and B respectively"): a manager who won one race and lost the next is then put in the wrong role. A manager was "beaten to both A and B" only when both lines name them as beaten. Where the beaten managers carry no (free agent) or (other move) tag the race was won on the league's waiver order, which is all you may say about anyone's waiver position; a race tagged (free agent) was first come first served, so say nothing about order; and where the line says the winner could not be identified, name no winner
```

Tests: `tests/test_league_recap.py:6956-6958` assert `"Each \"Contested players\" line is one race"` (holds), `"NEVER fold two lines into one clause"` (becomes `"never fold two lines into one clause"`), `'no "respectively"'` (becomes `'"X and Y were beaten to A and B respectively"'`). The retry instruction for `RECAP_WARNING_CONTESTED_MISATTRIBUTION` (`league_recap_checks.py:1192-1196`) is unchanged and `:6931`'s `'"respectively"'` assertion on it still holds.

### Hunk 5 (finding 6): one fines-counting rule

`fpl_cli/prompts/league_recap.py:66-67`, replacing both rules:

```diff
-- When you do use it, take its numbers verbatim and only from that section. NEVER add up fines yourself from the "## Fines" section, which covers this gameweek alone, and never present a total the Season Fines section qualifies as incomplete as though it were final - repeat its qualification alongside it or leave the number out
-- NEVER call a fine or a last-place finish a manager's "second" (or third, or any later count), and never reach for "again", "another", "twice", "back-to-back", "in a row" or "still" about one, unless a section says so about that manager in those words. The "## Fines" section places each of this gameweek's fines in its manager's season for you - if it says the fine is their first, it is their first, whatever the rest of the data seems to suggest. A manager whose Season Fines total is 1 has been fined once, this gameweek, and never before. Two managers each on 1 are two separate first offences, not a repeat for either; and the league-wide "N fine(s) recorded in total" is the league's number, never one manager's
+- Season fine totals come from the "## Season Fines" section exactly, never from adding up the "## Fines" section, which covers this gameweek alone, and a total that section qualifies as incomplete carries its qualification or stays out. Where this gameweek's fine sits in a manager's season - their first, their third, their "another" or "again" - is stated for you under the "## Fines" section; use that placement and no other, because a bare total of 1 is one fine, this gameweek, and the league-wide total is nobody's running count
```

Tests: none assert on rule 12 or 13 text directly (`"NEVER add up"` and `"second"` are not asserted); `tests/test_league_recap.py` fine-placement tests read the user prompt, which this hunk does not change.

### Hunk 6 (finding 7): say each review rule once

`fpl_cli/prompts/review.py:437-444`

```diff
-[2-3 sentences: How did your Classic team perform? Only reference players who actually scored points for you (starters + auto-subs, NOT bench players). Disappointments, captain choice assessment. Reference the community narrative where your players featured.]
+[How your Classic team performed: the contributors who decided it, the disappointments, and the captain call against the hindsight line. Reference the community narrative where your players featured.]

-**Selection:** [Note any selection mistakes - did benched players outscore starters? Was the captain the right call? If 2+ players from the same team collectively hauled or blanked, note the exposure outcome. If a bench player from the Bench vs Starters data outscored more than one starter, say so as either an aggregate ("combined for N, more than his M") or a per-player claim ("each scored fewer than his M individually") - never both in the same clause - and check the arithmetic before writing it. If selections were good, acknowledge briefly.]
+**Selection:** [Selection mistakes from the Bench vs Starters data, the captain call, and any same-club exposure that hauled or blanked together. If selections were good, acknowledge briefly.]

-[2-3 sentences: How did your Draft team perform? Only reference players who actually scored points for you (starters + auto-subs, NOT bench players). Poor performers.]
+[How your Draft team performed: the contributors who decided it and the poor performers.]

-**Selection:** [Note any selection mistakes - did benched players outscore starters? If 2+ players from the same team collectively hauled or blanked, note the exposure outcome. If a bench player from the Bench vs Starters data outscored more than one starter, say so as either an aggregate ("combined for N, more than his M") or a per-player claim ("each scored fewer than his M individually") - never both in the same clause - and check the arithmetic before writing it. If selections were good, acknowledge briefly.]
+**Selection:** [Selection mistakes from the Bench vs Starters data and any same-club exposure that hauled or blanked together. If selections were good, acknowledge briefly.]
```

The contributors-only rule stays at `:378` and the aggregate-versus-per-player rule at `:332`, both in the hard constraints. Tests: `tests/test_review.py:45-63` assert on words that remain (`"bench"`, `"BENCH"`, `"selection"`, `"Bench vs Starters"`, `"pre-computed"`, `"formation-valid"`); run `tests/test_review.py` and `tests/test_cli_review.py` to confirm no assertion keys on the placeholder text.

### Hunk 7 (finding 8): headings stay, counts go

`fpl_cli/prompts/review.py:401`, `:404`, `:459`, `:462`

```diff
-Structure your response EXACTLY as follows:
+Use these sections, in this order:

-[2-3 sentences: High-level verdict across both formats. Were you a winner, loser, or somewhere in between this week? Use the GW Position annotation (e.g. "4th= worst") for any league framing - do not re-derive positions from raw data.]
+[A short verdict across both formats: winner, loser or somewhere in between this week. Use the GW Position annotation (e.g. "4th= worst") for any league framing - do not re-derive positions from raw data.]

-[1-2 sentences: What does this GW suggest for upcoming decisions? Every start, bench, transfer or drop call ...
+[Briefly: what does this GW suggest for upcoming decisions? Every start, bench, transfer or drop call ...

-[1-2 sentences: What patterns from this GW are worth watching? No fixture data ...
+[Briefly: what patterns from this GW are worth watching? No fixture data ...
```

`required_synthesis_sections()` reads only the `## ` headings inside `<output_format>`, which are untouched. Tests: `tests/test_review.py` completeness tests key on headings, not placeholders.

### Hunk 8 (finding 9): GW1 said once

`fpl_cli/prompts/review.py:479-485`

```diff
 _EDGE_CASES_OPENING_GAMEWEEK = f"""\
 <edge_cases>
-- Classic has no transfers in the opening gameweek and no transfer was rolled or held. Judge the squad as a pre-season build, not as a week of restraint
 - {_EDGE_CASE_WAIVERS}
 - {_EDGE_CASE_MISSING_DATA}
 - If the league standings are reported as unavailable, say the table is not out rather than inventing or inferring a position
 </edge_cases>"""
```

The `<season_context>` block (`:487-496`) and the transfers string (`_review_summarisation.py:630-634`) keep the rule beside the data. Tests: grep `tests/test_review.py` and `tests/test_cli_review.py` for `"pre-season build"` and `"week of restraint"` before applying; move any such assertion onto `_SEASON_CONTEXT_OPENING_GAMEWEEK`.

### Hunk 9 (finding 10): explicit, configurable effort

`fpl_cli/api/providers/anthropic.py:64-72`

```diff
         merged = {**self.query_defaults, **kwargs}
         max_tokens = int(merged.get("max_tokens", 1024))
+        output_config = merged.get("output_config")

         messages = [{"role": "user", "content": prompt}]
         payload: dict[str, Any] = {
             "model": self.model,
             "max_tokens": max_tokens,
             "messages": messages,
         }
         if system_prompt:
             payload["system"] = system_prompt
+        if output_config:
+            # `output_config.effort` is the only thinking control on the current
+            # models; thinking shares `max_tokens` with the prose (#316), so a
+            # lower level is the lever on a cut-off editorial that a bigger
+            # ceiling (and the 60s timeout) is not (#389).
+            payload["output_config"] = dict(output_config)
```

`fpl_cli/config/defaults.yaml:50-51`

```diff
     query_defaults:
       max_tokens: 8000  # thinking + output share this ceiling (#316); 4096 left no room for prose once the model reasoned first
+      output_config:
+        effort: high  # Sonnet 5.5's own default, pinned so a model swap cannot move it silently (Opus 5.5 defaults to medium). Sweep medium and low against saved recaps before lowering (#389)
```

Docs: the `llm:` example in `docs/command-reference.md:1622-1629` and the `AnthropicProvider` row in `docs/architecture.md:424` show the new key. Tests: add a case beside `tests/test_providers.py:355-367` asserting `call_payload["output_config"] == {"effort": "high"}` when `query_defaults` carries it, and that the key is absent when it does not; `tests/test_providers.py:1069-1081` (the `max_tokens >= 8000` pin) is unaffected. `OpenAICompatProvider` ignores the key, which is correct: `output_config` is not part of the chat-completions shape.

### Hunk 10 (finding 11): fallback ceiling sized for a thinking model

`fpl_cli/api/providers/anthropic.py:64`

```diff
-        max_tokens = int(merged.get("max_tokens", 1024))
+        # Thinking shares this ceiling (#316): at 1024 a current model returns a
+        # lone thinking block and no text. Matches the shipped synthesis default.
+        max_tokens = int(merged.get("max_tokens", 8000))
```

Tests: none pin 1024 (`grep -n 1024 tests/test_providers.py` finds nothing); `tests/test_providers.py:355-367` passes an explicit value.

### Hunk 11 (finding 12): carry the refusal category

`fpl_cli/api/providers/_models.py`, in `LLMResponse`:

```diff
     citations: list[str] = field(default_factory=list)
     stop_reason: str | None = None
+    # The provider's reason for a refusal, when it gives one: Anthropic's
+    # `stop_details.category` ("general_harms", "cyber", ...). None otherwise.
+    stop_category: str | None = None
```

`fpl_cli/api/providers/anthropic.py:117-127`:

```diff
+        stop_details = data.get("stop_details") or {}
         response = LLMResponse(
             content=content,
             model=data.get("model", self.model),
             usage=usage,
             stop_reason=data.get("stop_reason") or None,
+            stop_category=stop_details.get("category") or None,
         )
```

`log_abnormal_stop` (`_models.py:120-137`) appends ` (category: %s)` when `response.stop_category` is set, and the two refusal messages in `fpl_cli/cli/league_recap.py:1048-1051` and `:607-611` and the report callout in `fpl_cli/templates/gw_league_recap.md.j2` name it the same way. Tests: extend `tests/test_providers.py:379-386`'s refusal case with a `stop_details` payload; `tests/test_cli_league_recap.py` refusal tests gain the category in the expected message.

### Hunk 12 (finding 2): the recap records which attempt shipped

`fpl_cli/cli/league_recap.py:875-935`, `_editorial_with_data_checks` returns the attempt and the count:

```diff
-) -> tuple[str, dict[str, list[str]], LLMResponse]:
+) -> tuple[SynthesisAttempt[dict[str, list[str]]], int]:
 ...
-    best, _ = await synthesise_with_retry(
+    return await synthesise_with_retry(
 ...
-    return best.text, best.verdict, best.response
```

and `_recap_llm_summarise` (`:1019-1067`) unpacks `shipped, attempts`, reads `shipped.text`, `shipped.verdict`, `shipped.response`, stores `collected_data["synthesis_attempts"] = attempts`, and writes `shipped.prompt` rather than `user_prompt` to `recap_prompt.txt` under `--debug` (the dry-run branch still writes `user_prompt`, since nothing was called). Tests: `tests/test_cli_league_recap.py:1650`, `:4055`, `:4094`, `:4133`, `:4170`, `:4195`, `:4215`, `:4232`, `:6688` read `recap_prompt.txt` after `--dry-run` or a single attempt and are unaffected; add one retried-run case asserting the file equals the stub's second prompt, mirroring `tests/test_cli_review.py:4566`.

### Hunk 13 (finding 14): attempts and usage in the payload

With hunk 12 in place, `league_recap.py:569-651` adds to `metadata`:

```diff
                         "synthesis_summary": collected_data.get("synthesis_summary"),
+                        # 1 or 2: whether the editorial is a first or a retried draft.
+                        # Null when no editorial was asked for (#389).
+                        "synthesis_attempts": collected_data.get("synthesis_attempts"),
+                        "synthesis_usage": collected_data.get("synthesis_usage"),
```

where `synthesis_usage` is `{"input_tokens": ..., "output_tokens": ...}` from `shipped.response.usage`. `review` gets the same two fields from `_review_llm_summarise`'s return value. Docs: the `league-recap` JSON metadata list in `docs/command-reference.md` and the `league-recap` and `review` rows in `.agents/TOOLS.md`. Tests: one JSON-mode case per command asserting the fields.

### Hunk 14 (finding 13): present-tense output style

`.claude/output-styles/fpl-mate.md:57`

```diff
-... Capture stderr when output looks thinner than you expected. As of v2.3.0, a command failure under `--format json` is a `{command, error}` envelope on stdout (exit 1) - the same channel as success, so check for an `error` key before assuming a thin or empty parse means the command hung.
+... Capture stderr when output looks thinner than you expected. A command failure under `--format json` is a `{command, error}` envelope on stdout (exit 1) - the same channel as success, so check for an `error` key before assuming a thin or empty parse means the command hung.
```

`:133`

```diff
-- **The ceilings were recalibrated in v2.2.0.** Don't anchor on absolute bands you remember from an older run - read the current numbers and compare like with like
+- **Read the current numbers, not remembered bands.** The ceilings move between releases, so compare like with like within one run
```

`:137`

```diff
-- **GK ceilings ramp in with the calendar through GW6 (v2.3.0).** Before that fix an elite, ever-present keeper's score was capped in the low 70s pre-GW6 regardless of form, because the ceiling assumed a full sample. Now the ceiling itself scales down to match what the calendar could plausibly have shown by that gameweek, so an elite keeper reads correctly high from the opening weeks - a low score for a keeper before GW6 is now a real read (rotation risk, poor form), not a scale artefact
+- **GK ceilings scale with the calendar through GW6.** The ceiling matches what the season could plausibly have shown by that gameweek, so an elite keeper reads correctly high from the opening weeks and a low pre-GW6 keeper score is a real read (rotation risk, poor form), not a scale artefact
```

Tests: `tests/test_output_style_sync.py` checks command names only; unaffected.

### Hunk 15 (finding 20): exact figures, the model's own sentences

Two parts. First, the rule wording. `fpl_cli/prompts/league_recap.py:61` (rule 7) and the "verbatim" clauses at `:62-63`:

```diff
-- A League History entry phrased as an observed count over a span (e.g. "3 in the last 11, with 8 not recorded") must be repeated that way, never simplified to "in a row" or "consecutive" unless the section itself already uses that phrasing
+- A League History entry keeps its meaning when you use it, in your own sentence: a count stated over a span (e.g. "3 in the last 11, with 8 not recorded") stays a count over a span with its unrecorded gameweeks mentioned, never simplified to "in a row" or "consecutive" unless the section itself says so. The figures, the span and the qualifier are the facts; the sentence around them is yours
```

and in rules 8 and 9, `Take the count verbatim, repeat its "not judged" qualifier alongside it or leave the line out` becomes `Keep the count and its "not judged" qualifier exact, in your own words, or leave the line out`, and `with its season names, points, ranks, percentages and counts repeated verbatim` becomes `with its season names, points, ranks, percentages and counts kept exact`. Hunk 1 already rewrites rule 3's "own wording" clause the same way.

Second, the sentences themselves. `fpl_cli/services/league_history_notes.py:523-535` builds the season-count line as `"{manager}: {n} {label} this season ({span}), the latest this gameweek, with {k} gameweek(s) not judged either way"`, and the streak builder near `:388` the `"{n} {label} in a row ({span})"` shape, with a `", with no completed moves in {k}"` style tail for held gameweeks. These lines reach three surfaces (console, report, prompt), and only the prompt copy is quoted by a writer. Give the prompt surface a reader's sentence while the report and console keep the ledger form, by rendering `NoteSurface.PROMPT` entries through a second template, for example `"{manager} has {n} {label} this season, the latest this gameweek ({span}; {k} gameweek(s) could not be judged)"` and `"{manager}: {n} {label} in a row ({span}), with {k} gameweek(s) in that run holding no move"`. The exact wording is the author's call; the point is that the sentence a columnist is told to keep exact should be one a columnist could have written.

Tests: `tests/test_league_history_notes.py` pins the current sentences four times and `tests/test_cli_league_recap.py` four times; a prompt-only template leaves the report and console assertions intact and adds assertions for the prompt rendering. `tests/test_league_recap.py:4310-4311` (`"not recorded"`, `"never simplified to"`) hold against the rule 7 text above.

### Hunk 16 (finding 21): say where the editorial is read

`fpl_cli/prompts/league_recap.py:35-37`

```diff
 <context>
 Your audience is every member of this league. They want entertainment first, information second. Write with personality - name names, call out embarrassing decisions, celebrate great picks.
+The editorial is pasted into the league's WhatsApp group and read on phones. Short paragraphs, bold for the names and labels that should jump out, no tables and no headings beyond the one headline asked for below: headings and rules are stripped on the way into the chat, and nobody reads a wall of text in a group.
 </context>
```

`normalise_recap_editorial()` is unaffected: bold labels are not headings. Tests: `tests/test_league_recap.py:6565-6571` assert on `<output_format>` lines only.

### Hunk R (optional, findings 1, 3, 5, 6 and 20 together): a consolidated rules block

This is the larger step the audit points at and it should not ship without the A/B in "Next step". It replaces the 25 rules with 16 built around one stated principle, keeps every constraint that has an issue behind it, and keeps the `<context>` (as amended by hunk 16), `<tone>` (as amended by hunk 3) and `<output_format>` blocks. Roughly 2,100 tokens against today's 3,400 for the block.

```text
<rules>
Every member of this league knows the real numbers, so a wrong one is noticed and remembered, and the entertainment only works while every fact is right. The data below is laid out so that each fact has one section that owns it. These rules say which.

- This is a recap, not a preview: no advice, no recommendations, and nothing about future gameweeks
- Every section owns the facts it states, and a fact is yours to use with its figures, spans and qualifiers kept exactly as the section states them, in your own sentences. Counts, totals, ties, roles, clubs and nets are read off that section, never derived from another one, added up, or filled in from memory. The Awards section names only the single best and single worst of each kind, and the GW Standings table is one gameweek's snapshot, so neither says how many managers did something, who else did it, or what happened in earlier gameweeks
- Stick to what happened this gameweek. The only facts spanning more than this gameweek you may state are the ones a section hands you: streaks and season counts from the "## League History" section, a manager's FPL seasons before this one from the "## Prior Seasons" section, season fine totals from the "## Season Fines" section, and where this gameweek's fine sits in a manager's season from the "## Fines" section. Keep each one's meaning: a count stated over a span ("3 in the last 11, with 8 not recorded") is repeated that way, never simplified to "in a row" or "consecutive"; a "not judged" or "incomplete" qualifier travels with its number or the number stays out; a fine is a manager's "second", "another" or "again" only when the Fines section says so of that manager. A streak, trend or total none of them states is not yours to mention, however obvious it looks, and never derive or extrapolate a season total yourself
- A League History season-count line ("4 gameweek wins this season") and a Season Fines total are optional colour: use one when it sharpens something that happened this gameweek ("Bob's fourth last-place of the season"), or to ground a retrospective at the halfway point and the finale when the section carries the season's full counts. Never open or close on them, never list them out, and a week nobody was fined rarely needs the fines table at all
- Each League History entry is about one named manager. Two managers never share a record, streak or "club" unless a single entry names them together, and one gameweek's worth of anything is not a streak
- "Previously led", "topped the table" and "fell from the top" match the "Previous gameweek's leader" statement at the top of the GW Standings section exactly; if it names no leader, nobody led
- "Joint", "tied", "level", "shared", "equal" and "the highest" or "the lowest" follow the GW Standings statements of who holds each gameweek score and league position alone and which are shared. A tie in the Pos column is on season totals, not gameweek scores; managers level on points but placed apart are "level on points", never joint
- A manager's earlier seasons come only from the "## Prior Seasons" section, its figures kept exact, and they are FPL-wide: "their 11th season of FPL", never "in this league", "a founder member" or "back for another year here", because nothing says when anyone joined this league. A manager listed with no prior seasons has no past to describe, one whose record could not be fetched gets no claim either way, and one gameweek against a season total is no trajectory ("improving", "off last year's pace")
- Fines triggered this gameweek are a highlight
- The biggest bench haul is always funny, so lean into it, but a bench "outscored the team" only when its points are strictly greater than the GW points
- A chip is a narrative hook: mockery for a flop, grudging respect for one that paid off. The "## Chips Played" section and its total are the roster; name every user of a chip or none, and never count tags in the standings table
- Captains come from the "## Captains" section and its counts: a manager is an outlier, a dissenter or "one of the N on Salah" only as that section groups them
- Transfers come from the "## Transfers" section and draft moves from the "## Waivers and Free Agents" section: who moved, who stood still, each move and its swing, the hit, and the count of movers, and no move either does not list. Where a line says a manager's moves or net are unknown, supply neither; a move tagged [free agent] is a signing and one tagged [waiver] is a claim; draft has no hits. Without either section or a transfers note, say nothing about transfers, waivers, hits or moves in and out: absence of the data is nothing to report, not licence to invent
- A roster line carries two kinds of figure. The brackets (move count, hit, net overall, and on a mixed draft line each kind's own net) describe the manager's gameweek; the swing after each move describes that move alone. Give a move its swing, the manager their net ("-6 net overall", "-6 across both transfers after the hit"), and a kind its own net; a net overall is never one move's or one kind's. The hit is charged for the gameweek's transfers together: a manager "took a hit", never a hit "to bring in" a named player
- A waiver is a competition. A manager the section lists as having claimed a player and lost him to a rival was active: they spent a claim at the priority stated and were beaten, so say they tried and missed, never that they "sat it out" or "showed restraint". Only managers listed as making no moves and submitting no claims sat out, and even then no motive (discipline, laziness, apathy) is yours to assign. A mover's "also claimed and lost" tail is extra activity, not a move they made
- The "Contested players" lines are the only source for who else wanted a player: who claimed him, who won him and who was beaten to him, with the priority each beaten manager gave the claim (their own ranking of their claims that week, not the league's waiver order). Each "Contested players" line is one race with fixed roles, so write every race as its own clause naming its winner and its beaten managers, and never fold two lines into one clause ("X and Y were beaten to A and B respectively"): a manager who won one race and lost the next is then put in the wrong role. A manager was "beaten to both A and B" only when both lines name them. A race whose beaten managers carry no (free agent) or (other move) tag was won on the league's waiver order, which is all you may say about anyone's waiver position; a (free agent) race was first come first served, so say nothing about order; where the winner could not be identified, name no winner
- Spell every player and manager exactly as the data does, and give a player only the club the "## Player Clubs" section or the club beside their name gives: your own knowledge of who plays where goes a season out of date. A player with no club in the data is named alone ("Haaland's 2 points")
</rules>
```

Test assertions this block breaks, with the replacement string that holds against it: `tests/test_league_recap.py:3614` (`"## Captains"`, holds), `:3615` (`"verbatim"`, becomes `"kept exact"`), `:3998` (`'the "## Waivers and Free Agents" section is the source of truth for waiver claims and free-agent signings'`, becomes `'draft moves from the "## Waivers and Free Agents" section'`), `:3999` (`"never call a move tagged [free agent] a waiver claim"`, becomes `"a move tagged [free agent] is a signing and one tagged [waiver] is a claim"`), `:4000` (`"Do NOT infer waiver activity from the Awards section"`, becomes `"The Awards section names only the single best and single worst of each kind"`), `:4004` (`'If there is no "## Transfers" section, no "## Waivers and Free Agents" section and no transfers note'`, becomes `"Without either section or a transfers note"`), `:4058` (`'treat the "## Transfers" section as the source of truth'`, becomes `'Transfers come from the "## Transfers" section'`), `:4059` (becomes the Awards sentence above), `:4060` (`"not licence to invent"`, holds), `:4068` (`"NEVER state a club for a player other than the club given for them"`, becomes `'give a player only the club the "## Player Clubs" section or the club beside their name gives'`), `:4071-4072` (hold), `:4297-4299` (first becomes `"Stick to what happened this gameweek. The only facts"`; `'"## League History" section'` holds; `'"## Prior Seasons" rule below'` becomes `'"## Prior Seasons" section'`), `:4305-4306` (hold), `:4310-4311` (hold), `:6568-6571` (hold, `<output_format>` is untouched), `:6956-6958` (as in hunk 4). The data sections' embedded instructions (finding 19) are unchanged and still agree.

## Verification (Step 7) and next step

The saved reports settle the attribution question as far as observation can: the change the issue describes happened on one model while the prompt grew, and the model change adds a second, smaller effect from GW6 onward. What the reports cannot do is show that a shorter prompt gives the editorial its voice back without letting the wrong claims the rules were written against back in. Three experiments can, and all use knobs that already exist.

1. **Rules against voice, same model.** Take the `recap_system.txt` and `recap_prompt.txt` a `--dry-run` writes for GW6 in each format, swap in the hunk R rules block (or just hunks 1 to 5, 15 and 16), and run the same user prompt through both system prompts on the shipped model. Grade both ways: `check_recap_editorial()` over each editorial for wrong claims, and the four register measures used above (words per sentence, figures per hundred words, bold spans, five-word runs copied from League History) for voice. Report every measure per format and gate on the draft row, since that is where the copying is and a pooled mean hides it; read the draft pair of WhatsApp conversions, not only the classic. One change at a time where a hunk regresses: re-add the lost constraint in its shortest form, not the verbose original. Then paste both through `convert-md-to-whatsapp` and read them as the league would.
2. **Size the model's share.** Run `fpl league-recap -g 6 --summarise --debug` once more with `FPL_SYNTHESIS_MODEL=claude-sonnet-5` exported and compare its figures per hundred words and sentence length against the saved GW6 editorial. The review control already suggests the doubling is the model's; a second data point either confirms it or sends it back to the prompt. Two API calls.
3. **Then sweep effort.** With hunk 9 applied, repeat the winning prompt at `effort: medium` and `low` through `settings.yaml`, and compare the check counts, the output token counts (hunk 13 makes them visible) and the stop reasons. The migration notes' starting point for content generation is `low`, but strict instruction following is the one case they pull the other way, so the sweep decides.

Before any hunk lands: `ruff check fpl_cli/ scripts/`, `pyright fpl_cli/ scripts/`, `python3 -m pytest tests/`, and the assertion updates named per hunk. Grep for every removed phrase across `tests/`, `docs/` and `.agents/` first; the string assertions above were found by grep and the list may not be complete.

Re-run this audit at the next model change. The release skill already has a preflight item for the default model pin (`.agents/skills/release/SKILL.md:59`, from #362); adding "re-read the prompts against the new model's migration notes" beside it would make the two move together. The register measures above take a minute to recompute over the vault's `01_Reports/`, and are the cheapest way to notice the next drift before a reader does.
