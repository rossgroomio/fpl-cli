"""Shared markdown heading/section matching for gw-prep validator scripts.

`extract_classic_squad.py` and `validate_draft_waivers.py` both solve the same
problem: locate a markdown section by its heading, bounded by the next heading
of the same or shallower depth. The markdown they read is authored by
sub-agents from a verbatim-heading instruction, so a matcher that insists on an
exact heading silently mislocates the section whenever the sub-agent decorates
the heading -- the failure mode behind issue #63, where a formation suffix on
`#### Starting XI` zeroed the row count on a perfectly valid squad.

`HeadingMatcher` therefore compares a *normalised core* of the heading text
rather than the raw string, tolerating the four drift shapes seen or plausible
in practice:

    #### Starting XI (3-4-3)      trailing bracketed annotation
    #### Starting XI - 3-4-3      trailing qualifier after punctuation/digit
    #### (3-4-3) Starting XI      leading annotation
    #### starting xi              case variant

What it must never do is match a *different* heading that merely shares a
prefix -- `#### Bench Order` and `#### Bench-Warmers` are separate headings in
the output templates, not qualified spellings of `#### Bench`. Two rules keep
that line: an extra whitespace-separated *word* is never an annotation, and a
qualifier glued on with no separating space must not run into a word character.

Abbreviation/substitution drift (`#### XI` for `#### Starting XI`) can't be
derived from the text, so it is opt-in per call site via `aliases` rather than
guessed globally.

Scanning is fence-aware throughout: a `#` line inside a fenced code block is
code, not a heading, so it neither opens nor closes a section.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence

__all__ = [
    "HeadingMatcher",
    "as_matcher",
    "fence_flags",
    "find_section",
    "has_heading",
    "parse_heading",
    "section_body",
]

_FENCE_RE = re.compile(r"^(?:`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})(?:\s+(.*))?$")

# Markdown emphasis wrapping the whole heading text ("#### **Bench**").
_EMPHASIS_RE = re.compile(r"^[*_`~]+|[*_`~]+$")
# A balanced bracketed annotation anywhere in the text ("(3-4-3)", "[draft]").
_BRACKETED_RE = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}")
# A leading annotation token, digit-initial so it can't eat a real first word.
_LEADING_ANNOTATION_RE = re.compile(r"^[0-9]\S*(?:\s+|$)")
# A trailing qualifier introduced by punctuation or a digit: either separated by
# whitespace, or glued on -- but a glued qualifier must not continue into a word
# character, or "Bench-Warmers" would read as a qualified "Bench".
_TRAILING_ANNOTATION_RE = re.compile(
    r"(?:\s+(?:[0-9]|[^\s\w])|(?<=\S)(?:[0-9]|[^\s\w])(?!\w)).*$"
)
_WHITESPACE_RE = re.compile(r"\s+")


def fence_flags(lines: Iterable[str]) -> Iterator[bool]:
    """Yield, per line, whether it sits inside a fenced code block.

    The fence delimiters themselves report True, so a caller can skip every
    flagged line and never treat fenced content as markdown structure.
    """
    fence: str | None = None
    for line in lines:
        match = _FENCE_RE.match(line.strip())
        if fence is None:
            if match:
                fence = match.group(0)[0]
                yield True
            else:
                yield False
            continue
        if match and match.group(0)[0] == fence:
            fence = None
        yield True


def parse_heading(line: str) -> tuple[int, str] | None:
    """Split an ATX heading line into (depth, text), or None if it isn't one."""
    match = _HEADING_RE.match(line.strip())
    if match is None:
        return None
    return len(match.group(1)), match.group(2) or ""


def _normalise(text: str) -> str:
    """Casefold and collapse a heading text without discarding any of it."""
    stripped = _EMPHASIS_RE.sub("", text.strip())
    return _WHITESPACE_RE.sub(" ", stripped).strip().casefold()


def _core_text(text: str) -> str:
    """Strip annotations from a heading text, leaving its core wording."""
    core = _EMPHASIS_RE.sub("", text.strip())

    previous = None
    while previous != core:  # nested brackets need more than one pass
        previous = core
        core = _BRACKETED_RE.sub(" ", core)
    core = _WHITESPACE_RE.sub(" ", core).strip()

    while True:
        leading = _LEADING_ANNOTATION_RE.match(core)
        if leading is None:
            break
        core = core[leading.end() :].strip()

    core = _TRAILING_ANNOTATION_RE.sub("", core).strip()
    return _WHITESPACE_RE.sub(" ", core).strip().casefold()


class HeadingMatcher:
    """Matches one heading at a fixed depth, tolerating annotation drift.

    `heading` carries its own `#` marker ("#### Starting XI"); `aliases` are
    marker-less alternate spellings accepted at the same depth.
    """

    def __init__(self, heading: str, aliases: Sequence[str] = ()) -> None:
        parsed = parse_heading(heading)
        if parsed is None:
            raise ValueError(f"Not a markdown heading: {heading!r}")
        self.heading = heading
        self.depth, text = parsed
        self._targets = {_normalise(text), *(_normalise(a) for a in aliases)}
        if "" in self._targets:
            raise ValueError(f"Heading text must not be empty: {heading!r}")

    def matches(self, line: str) -> bool:
        """True if `line` is this heading, bare, qualified or aliased."""
        parsed = parse_heading(line)
        if parsed is None:
            return False
        depth, text = parsed
        if depth != self.depth:
            return False
        return _normalise(text) in self._targets or _core_text(text) in self._targets

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HeadingMatcher({self.heading!r})"


def as_matcher(heading: str | HeadingMatcher) -> HeadingMatcher:
    """Coerce a heading string to a matcher, passing matchers through."""
    return heading if isinstance(heading, HeadingMatcher) else HeadingMatcher(heading)


def find_section(
    lines: Sequence[str], heading: str | HeadingMatcher
) -> tuple[int, int] | None:
    """Return (start, end) line indices for `heading`'s section, or None.

    `start` is the heading line itself; `end` is exclusive. The section ends at
    the next heading of the same or shallower depth -- except a repeated or
    re-qualified occurrence of the *same* heading, which extends the section
    rather than truncating it at the first of the pair.
    """
    matcher = as_matcher(heading)
    flags = list(fence_flags(lines))

    start: int | None = None
    for i, line in enumerate(lines):
        if not flags[i] and matcher.matches(line):
            start = i
            break
    if start is None:
        return None

    for i in range(start + 1, len(lines)):
        if flags[i]:
            continue
        parsed = parse_heading(lines[i])
        if parsed is None or parsed[0] > matcher.depth:
            continue
        if matcher.matches(lines[i]):
            continue
        return (start, i)
    return (start, len(lines))


def section_body(
    lines: Sequence[str], heading: str | HeadingMatcher
) -> list[str] | None:
    """Return the body lines of `heading`'s section, or None if absent.

    The heading line and any repeated occurrence of it are dropped; everything
    else in the section, fenced content included, is preserved verbatim.
    """
    matcher = as_matcher(heading)
    found = find_section(lines, matcher)
    if found is None:
        return None
    start, end = found
    flags = list(fence_flags(lines))
    return [
        line
        for i, line in enumerate(lines[start:end], start=start)
        if flags[i] or not matcher.matches(line)
    ]


def has_heading(lines: Sequence[str], heading: str | HeadingMatcher) -> bool:
    """True if `heading` appears outside any fenced code block."""
    return find_section(lines, heading) is not None
