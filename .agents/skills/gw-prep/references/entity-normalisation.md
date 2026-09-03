# Entity normalisation (shared post-write step)

Every skill that assembles a file out of sub-agent prose runs this step
immediately after writing that file. Referenced by gw-prep Phase D,
squad-builder Phase E and update-gw-prep Phase D — this file is the single
source for the contract below, so a change to the script's output shape is
edited here, not in three SKILL.md copies.

## Why

Sub-agent sections are concatenated verbatim. A section that arrived
HTML-escaped somewhere in the return path therefore lands in the file with its
markdown broken: a `&gt;` blockquote marker renders as literal text instead of
opening a quote block, a comparison operator in prose reads as `&gt;`, and the
entities go on to confuse the table parsing in the validation phases that
follow. Nothing else in the pipeline looks at whether the prose survived
assembly.

## Invocation

```bash
python3 "$FPL_CLI_DIR/.agents/skills/gw-prep/scripts/normalise_entities.py" --file "{path to the file just written}"
```

Within gw-prep itself, `"${CLAUDE_SKILL_DIR}/scripts/normalise_entities.py"` is
equivalent and matches the surrounding phases.

The script rewrites the file in place, decoding `<`, `>`, `&`, `"` and `'`, and
costs nothing when nothing was escaped. Decoding is deliberately narrower than
a blanket unescape: a numeric reference to anything else (`&#916;` for Δ)
survives into `residual` rather than being silently rewritten.

## Contract

Parse stdout as JSON:

```json
{"ok": true, "changed": false, "unescaped": 0, "residual": [{"line": 12, "entity": "&nbsp;"}]}
```

| Field | Meaning |
|---|---|
| `ok` | `false` only when `residual` is non-empty |
| `changed` | whether the file was rewritten |
| `unescaped` | characters recovered (length difference, not entity count) |
| `residual` | entity references left undecoded, with 1-based line numbers |

On a file that cannot be read, decoded as UTF-8, or written, it exits 1 with
`{"error": true, "messages": [...]}` — still JSON on stdout, never a traceback.

## Posture: warn, never block

An entity is a cosmetic defect, not a rule violation, so this step never stops
the pipeline.

- `ok: true` → silent continue, whether or not it changed anything.
- `ok: false` → emit an in-chat warning and proceed:
  > ⚠️ `{filename}` still contains {N} HTML entit(y/ies) after normalisation —
  > line {line}: `{entity}`, ... Check those lines render as intended.
- Script missing, or a non-zero exit → emit a warning naming the failure and
  proceed. Fail-open for infrastructure errors, as gw-prep Phase D1 does.
