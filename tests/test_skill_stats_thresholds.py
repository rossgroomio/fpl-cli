"""Skill `fpl stats` queries must not hardcode a minutes floor.

`--min-minutes` is a filter, not a quality bar: after ``N-1`` completed
gameweeks no player can have logged more than ``(N - 1) * 90`` minutes, so a
literal floor is arithmetically unreachable for the opening weeks of every
season. `fpl stats` then returns a well-formed envelope carrying ``"data": []``
-- no error, no warning -- which a sub-agent reads as "nobody qualifies".

The skills therefore derive the floor from the gameweek (``min(cap, (N - 1) *
45)``) and pass it as a placeholder. This module is the tripwire: a literal
creeping back in is silent at review time and silent at runtime, and only shows
up as four empty data blocks in an August gameweek-prep run.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

SKILL_DOCS = sorted(
    [
        *REPO_ROOT.glob(".agents/skills/*/SKILL.md"),
        *REPO_ROOT.glob(".agents/skills/*/references/*.md"),
    ]
)

# The FPL Mate output style is not a skill, but it carries the same
# early-season guidance to the same agents and ships as the reference copy
# downstream vaults sync from (#172). #253 fixed every skill doc and left it
# behind, so the `ep_next` rule below reads this wider set (#260).
OUTPUT_STYLE = REPO_ROOT / ".claude" / "output-styles" / "fpl-mate.md"

EP_NEXT_DOCS = sorted([*SKILL_DOCS, OUTPUT_STYLE])

# `--min-minutes 450` and `--min-minutes=450` -- Click accepts either -- but not
# `--min-minutes {mins_pos}`.
LITERAL_MIN_MINUTES = re.compile(r"--min-minutes(?:=|\s+)(\d+)")

# The placeholder form the skills are expected to use instead.
PLACEHOLDER_MIN_MINUTES = re.compile(r"--min-minutes(?:=|\s+)\{[a-z_]+\}")


def _skill_name(doc: Path) -> str:
    """The skill a doc belongs to -- `<skill>/SKILL.md` or `<skill>/references/*.md`."""
    return doc.parent.name if doc.name == "SKILL.md" else doc.parent.parent.name


def test_skill_docs_were_found():
    # Guard the guard: a stale glob would silently check nothing.
    assert any(p.name == "SKILL.md" for p in SKILL_DOCS)


def test_the_pattern_catches_both_click_spellings():
    # Click accepts `--min-minutes 450` and `--min-minutes=450` for the same
    # option, so a guard that only knows the space form waves the other through.
    assert LITERAL_MIN_MINUTES.search("fpl stats --min-minutes 450 -n 15")
    assert LITERAL_MIN_MINUTES.search("fpl stats --min-minutes=450 -n 15")
    assert not LITERAL_MIN_MINUTES.search("fpl stats --min-minutes {mins_pos} -n 15")
    assert PLACEHOLDER_MIN_MINUTES.search("fpl stats --min-minutes={mins_pos} -n 15")


@pytest.mark.parametrize("doc", SKILL_DOCS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_hardcoded_minutes_floor(doc: Path):
    text = doc.read_text(encoding="utf-8")
    offences = [
        f"line {text[: m.start()].count(chr(10)) + 1}: {m.group(0)!r}"
        for m in LITERAL_MIN_MINUTES.finditer(text)
    ]
    assert not offences, (
        f"{doc.relative_to(REPO_ROOT)} passes a fixed --min-minutes to `fpl stats`."
        " A literal floor is unreachable early in a season and empties the result"
        " silently; derive it from the gameweek instead, e.g."
        " `min(450, (N - 1) * 45)`, and pass it as a placeholder:\n" + "\n".join(offences)
    )


def test_the_skills_that_filter_on_minutes_use_the_placeholder():
    # The rule above is only meaningful while some skill still filters on
    # minutes -- if every call dropped the flag, the tripwire would pass vacuously.
    users = [d for d in SKILL_DOCS if PLACEHOLDER_MIN_MINUTES.search(d.read_text(encoding="utf-8"))]
    assert {_skill_name(d) for d in users} == {"gw-prep", "squad-builder"}


# ---------------------------------------------------------------------------
# The early-season `ep_next` sort is not a second opinion (issue #236)
# ---------------------------------------------------------------------------
#
# Both skills swap the positional sorts for `ep_next` before GW6. That is worth
# doing -- FPL scales the projection by chance of playing, and `--available-only`
# keeps doubtful players -- but until FPL's fixture factor moves off 1.0 the
# projection tracks `form` almost exactly: at GW4 of 2026/27 every row of all
# four positional shortlists had `ep_next == form`, in the order a `form` sort
# gives. A skill that sells the swap as a projection hands a sub-agent an
# ordering it will read as independent of the one-or-two-match sample it is
# actually made of.

# The `| `{rank_mid}` | `ep_next` | ... |` substitution rows.
EP_NEXT_RANK_ROW = re.compile(r"\|\s*`\{rank_\w+\}`\s*\|\s*`ep_next`\s*\|")

# The caveat those docs have to carry alongside them.
EP_NEXT_TRACKS_FORM = re.compile(r"`ep_next` tracks `form`")

# The claim that started this: `ep_next` described as prior-informed, which is
# what `quality_score` is and what `ep_next` early in a season is not.
# "alternative" is the output style's spelling of the same mistake (#260).
# "estimate" is deliberately not listed: that is the correct description of
# `quality_score`, and the CLI's own warning uses it.
PRIOR_INFORMED_EP_NEXT = re.compile(r"prior-informed (?:projection|alternative)")


def test_the_ep_next_patterns_match_what_the_skills_actually_write():
    # Guard the guard: a table row reworded past the regex would let the caveat
    # drop out unnoticed.
    assert EP_NEXT_RANK_ROW.search("| `{rank_mid}` | `ep_next` | `form` |")
    assert not EP_NEXT_RANK_ROW.search("| `{rank_mid}` | `form` | `form` |")
    assert EP_NEXT_TRACKS_FORM.search("in the opening gameweeks `ep_next` tracks `form` almost exactly")
    assert PRIOR_INFORMED_EP_NEXT.search("FPL's own prior-informed projection for the coming gameweek")
    assert PRIOR_INFORMED_EP_NEXT.search("suggests `-s ep_next` as a prior-informed alternative")
    # `quality_score` genuinely is prior-informed, in the CLI's own words, so
    # the guard must not fire on the one sentence these docs need to keep.
    assert not PRIOR_INFORMED_EP_NEXT.search("Read quality_score as a prior-informed estimate")


def test_output_style_was_found():
    # Guard the guard: a moved or renamed output style would silently stop
    # checking the single file #253 left carrying the claim.
    assert OUTPUT_STYLE.is_file()


@pytest.mark.parametrize("doc", EP_NEXT_DOCS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_agent_doc_calls_ep_next_prior_informed(doc: Path):
    text = doc.read_text(encoding="utf-8")
    offences = [
        f"line {text[: m.start()].count(chr(10)) + 1}: {m.group(0)!r}"
        for m in PRIOR_INFORMED_EP_NEXT.finditer(text)
    ]
    assert not offences, (
        f"{doc.relative_to(REPO_ROOT)} calls a projection prior-informed."
        " `quality_score` is the field the CLI blends with last season's"
        " pedigree; FPL's `ep_next` tracks `form` until the fixture factor"
        " moves off 1.0, so it carries no prior at all in the window the"
        " skills sort on it:\n" + "\n".join(offences)
    )


@pytest.mark.parametrize("doc", SKILL_DOCS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_docs_that_rank_on_ep_next_say_it_tracks_form(doc: Path):
    text = doc.read_text(encoding="utf-8")
    if not EP_NEXT_RANK_ROW.search(text):
        pytest.skip("does not substitute `ep_next` into an early-season sort")
    assert EP_NEXT_TRACKS_FORM.search(text), (
        f"{doc.relative_to(REPO_ROOT)} swaps the early-season sorts for"
        " `ep_next` without saying that `ep_next` tracks `form` in that window."
        " Without it a sub-agent reads the shortlist order as a projection"
        " rather than as the form ordering it is (issue #236)."
    )


def test_the_skills_that_rank_on_ep_next_are_the_two_expected():
    # As above: the rule is only meaningful while some skill still makes the
    # swap -- if both dropped it, the tripwire would pass vacuously.
    users = [d for d in SKILL_DOCS if EP_NEXT_RANK_ROW.search(d.read_text(encoding="utf-8"))]
    assert {_skill_name(d) for d in users} == {"gw-prep", "squad-builder"}
