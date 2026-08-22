---
name: preview-ingest
description: >
  Turn a season preview written in prose (newsletter, scout guide, your own
  notes) into a structured team intel file that fpl-cli can read. Use when the
  user says "ingest preview", "add a season preview", "extract this preview",
  "build my previews folder", or hands over a team preview article and wants it
  usable by squad-builder and gw-prep.
model: opus
compatibility:
  claude-code: full (parallel per-team ingestion via Agent tool)
  codex: full (single sequential pass)
  cursor: full (single sequential pass)
  copilot: full (single sequential pass)
---
<!-- CLI commands composed: intel, intel schema, intel show, intel resolve, intel init -->

# Preview Ingest

Convert season preview prose into `<config dir>/previews/{TEAM}.yaml`, the
format `fpl intel` reads.

**Environment:** All `fpl` commands require:
`cd "$FPL_CLI_DIR" && source .venv/bin/activate`

## Why this is a separate step

Preview prose is unstable: a different publication, a different year, or a
different author changes the shape entirely, and much of it is paywalled. The
consuming side — `fpl intel`, and the three skills that read it — depends only
on a versioned schema. This skill is the seam between the two. Extraction is a
reading task and belongs to a model; everything downstream is deterministic.

Run it once per team, in August. There is no in-season maintenance: intel
expires on a schedule the loader already knows (`fpl intel --show-decay`).

## Phase A: Scope

1. Confirm the schema reference is available:
   ```bash
   fpl intel --format json    # metadata.previews_dir is the write target
   ```
2. Identify the sources. One preview per team, any format the user has:
   a markdown file, a pasted article, a folder of files, their own notes.
3. Establish the team each source covers. If a file does not name a Premier
   League team unambiguously, ask rather than guess — a preview written into the
   wrong team's file is worse than no preview.
4. If the user wants the whole league scaffolded first:
   ```bash
   fpl intel init    # one empty stub per team; stubs never count as coverage
   ```

**Claude Code:** with more than three sources, launch one ingestion agent per
team in a single parallel Agent tool block, each running Phases B–D for its own
team. They write to different files and do not interact.

**Other agents:** run Phases B–D sequentially per team.

## Phase B: Extract

Read the source and fill the schema. Print the annotated reference before your
first extraction:

```bash
fpl intel schema
```

Every field except `team`, `season`, `source` and `published` is optional.

| Field | Fill from |
|---|---|
| `predicted_finish` | the source's aggregate/consensus prediction when it gives one, else the author's own |
| `team_strength.attack` / `.defence` / `.set_pieces` | percentile ranks if the source gives them, else omit |
| `team_strength.notes` | one line on style or shape |
| `transfers_in` / `transfers_out` | named signings and departures |
| `players[].status` | `starter` / `rotation` / `fringe` from a projected XI or depth talk |
| `players[].injury` | injuries with an expected return |
| `players[].role_change` | a changed position or job in the side |
| `players[].set_pieces` / `.penalties` | only when the source names the taker |
| `players[].new_signing` | arrivals with no Premier League record |
| `players[].notes` | anything else specific to that player |
| `narrative` | what does not fit a field |

### Extraction rules

1. **Record only what the source says.** If it praises a team's set-piece
   threat without naming the taker, fill `team_strength.set_pieces` and leave
   every `players[].set_pieces` empty. Inventing a penalty taker is the single
   most damaging thing this skill can do — it will be believed.
2. **Do not smooth over disagreement.** If an aggregate prediction and the
   author's own differ, put the aggregate in `predicted_finish` (it forms a
   coherent table across teams; a lone author's picks may not) and record the
   author's own view in `narrative`.
3. **Attribute honestly.** `source` and `author` are surfaced wherever this
   intel is used, so an opinion reads as an opinion.
4. **Leave players out.** A player the source does not discuss gets no entry.
   Absence is information; a padded roster is not.
5. **Do not copy the source.** Compress each claim to a clause. Never paste
   paragraphs of a paid newsletter into the file.
6. **`published` is the article's date, not today's.** It is what the season
   staleness check reads.
7. **Names as the source writes them.** Do not guess `code` — Phase C resolves
   codes deterministically and is right more often than recall.

## Phase C: Resolve player codes

```bash
fpl intel resolve {TEAM}            # dry run: shows every match and how it was made
fpl intel resolve {TEAM} --write    # writes codes back, preserving your comments
```

`exact` and `fuzzy` matches are safe. Anything reported `ambiguous` or
`unmatched` needs a decision:

- **ambiguous** — the command lists candidates with their codes. Pick from the
  source's context (position, role) and write the code in by hand.
- **unmatched** — usually a player who has left, was never in the game, or is
  spelled unrecognisably. Check with `fpl player "{name}"`. If they are not in
  the game, delete the entry rather than leaving it uncoded.

Never invent a code to clear a warning. An entry with no code is skipped by
consumers; an entry with a wrong code silently attaches intel to another player.

## Phase D: Verify

```bash
fpl intel show {TEAM}     # renders what you wrote, decayed to the current GW
fpl intel                 # coverage across the league, and any load warnings
```

Check:
- No load warnings naming your file.
- The team code is not reported as unknown.
- No warning about teams sharing a `predicted_finish` — when extracting a
  single source's predicted table the finishes should form a permutation, so a
  duplicate almost always means a row was misread. Re-check both files against
  the source before shrugging it off.
- Player count matches what you intended.
- `fpl intel show {TEAM} -g 5` still shows something sensible — if the whole
  file empties out at GW5, it was all injuries and transfers, which is fine but
  worth telling the user.

## Phase E: Report

Tell the user, per team:
- what was written, and where
- how many players carry intel, and how many needed hand-resolution
- anything in the source you deliberately did not encode, and why
- current league coverage and what it permits

**Coverage is the thing to flag.** `fpl intel` reports `usable_as`:

- `full` (enough of the league covered — `metadata.coverage` has the live numbers) — intel can
  support or oppose a pick.
- `negative_filter_only` (below that) — usable for injuries and rotation risk,
  never to promote a player, because written-up teams would otherwise look
  better purely for having been written up.
- `none` — nothing loaded, or everything has aged out.

If the user stops halfway through the league, say plainly that squad-builder
will treat the intel as a negative filter only, and that finishing the set is
what unlocks the rest.
