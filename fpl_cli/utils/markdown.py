"""Markdown heading/section matching, tolerant of LLM-authored drift.

Locating a markdown section by its heading, bounded by the next heading of the
same or shallower depth, comes up wherever LLM-authored markdown is parsed: the
gw-prep validator scripts recover structured data from sub-agent output, and
`fpl_cli.prompts.review` locates the GW Narrative section in a research
provider's response. Markdown authored this way is produced from a
verbatim-heading instruction, so a matcher that insists on an exact heading
silently mislocates the section whenever the heading is decorated -- the
failure mode behind issue #63, where a formation suffix on `#### Starting XI`
zeroed the row count on a perfectly valid squad.

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

The same module also carries entity normalisation, the other way markdown
arrives damaged from an LLM return path: a payload escaped somewhere in transit
loses its syntax, most visibly a `&gt;` blockquote marker that renders as
literal text. `unescape_specials` recovers it and `find_entities` reports what
it deliberately left alone.

`HeadingMatcher` assumes a fixed target heading text. A caller that needs to
locate a heading matching a pattern instead (e.g. one embedding a variable
gameweek number) should match that heading itself with its own regex, then use
`fence_flags`/`parse_heading` directly to compute a fence-aware, same-or-
shallower-depth section boundary from it -- the same primitives `_locate`
composes here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence

__all__ = [
    "HeadingMatcher",
    "as_matcher",
    "fence_flags",
    "find_entities",
    "find_section",
    "has_heading",
    "leaf_body",
    "parse_heading",
    "section_body",
    "unescape_specials",
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
# whitespace, or glued on -- but a glued qualifier must not continue into a
# letter, or "Bench-Warmers" would read as a qualified "Bench" (it may still
# continue into a digit, e.g. the punctuation in "XI:3-4-3" must strip along
# with the digits that follow it, not get left stranded on its own).
_TRAILING_ANNOTATION_RE = re.compile(
    r"(?:\s+(?:[0-9]|[^\s\w])|(?<=\S)(?:[0-9]|[^\s\w])(?![A-Za-z])).*$"
)
_WHITESPACE_RE = re.compile(r"\s+")


def fence_flags(lines: Iterable[str]) -> Iterator[bool]:
    """Yield, per line, whether it sits inside a fenced code block.

    The fence delimiters themselves report True, so a caller can skip every
    flagged line and never treat fenced content as markdown structure.
    """
    fence: tuple[str, int] | None = None
    for line in lines:
        match = _FENCE_RE.match(line.strip())
        if fence is None:
            if match:
                token = match.group(0)
                fence = (token[0], len(token))
                yield True
            else:
                yield False
            continue
        # Per CommonMark, a fence only closes on a run of the same character at
        # least as long as the one that opened it -- a shorter run of the same
        # character (or any run of a different one) is still fenced content.
        if match and match.group(0)[0] == fence[0] and len(match.group(0)) >= fence[1]:
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


def _strip_wrapping(text: str) -> str:
    """Strip emphasis, bracketed annotations and a leading annotation token,
    leaving any trailing qualifier untouched."""
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

    return core


def _strip_trailing_annotation(core: str) -> str:
    """Strip a trailing qualifier introduced by punctuation or a digit: either
    separated by whitespace (which strips to end of line unconditionally, e.g.
    "Draft League — provisional"), or glued on -- but a glued qualifier must not
    continue into a *letter*, or "Bench-Warmers" would read as a qualified
    "Bench". It may still continue into a digit, or the punctuation of a glued
    qualifier like "XI:3-4-3" would itself be left stranded when the strip
    starts at the digit instead.
    """
    return _TRAILING_ANNOTATION_RE.sub("", core).strip()


def _core_text(text: str) -> str:
    """Strip annotations from a heading text, leaving its core wording."""
    core = _strip_trailing_annotation(_strip_wrapping(text))
    return _WHITESPACE_RE.sub(" ", core).strip().casefold()


def _wrapping_stripped_text(text: str) -> str:
    """Like `_core_text`, but keeps a trailing qualifier intact.

    A trailing digit can be an alias's own text (the "Starting 11" alias) rather
    than an annotation, so stripping it unconditionally would turn a legitimately
    decorated alias into a false negative once any other drift is layered on top
    (e.g. "Starting 11 (3-4-3)"). Trying this alongside `_core_text` covers that
    case without weakening the anti-collision guarantee `_core_text` provides.
    """
    return _WHITESPACE_RE.sub(" ", _strip_wrapping(text)).strip().casefold()


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
        return (
            _normalise(text) in self._targets
            or _core_text(text) in self._targets
            or _wrapping_stripped_text(text) in self._targets
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HeadingMatcher({self.heading!r})"


def as_matcher(heading: str | HeadingMatcher) -> HeadingMatcher:
    """Coerce a heading string to a matcher, passing matchers through."""
    return heading if isinstance(heading, HeadingMatcher) else HeadingMatcher(heading)


def _locate(
    lines: Sequence[str], matcher: HeadingMatcher
) -> tuple[tuple[int, int] | None, list[bool]]:
    """Shared implementation for `find_section` and `section_body`.

    Returns the section bounds (or None) alongside the fence flags computed to
    find them, so a body lookup doesn't have to recompute fence state over the
    same lines a second time.
    """
    flags = list(fence_flags(lines))

    start: int | None = None
    for i, line in enumerate(lines):
        if not flags[i] and matcher.matches(line):
            start = i
            break
    if start is None:
        return None, flags

    for i in range(start + 1, len(lines)):
        if flags[i]:
            continue
        parsed = parse_heading(lines[i])
        if parsed is None or parsed[0] > matcher.depth:
            continue
        if matcher.matches(lines[i]):
            continue
        return (start, i), flags
    return (start, len(lines)), flags


def find_section(
    lines: Sequence[str], heading: str | HeadingMatcher
) -> tuple[int, int] | None:
    """Return (start, end) line indices for `heading`'s section, or None.

    `start` is the heading line itself; `end` is exclusive. The section ends at
    the next heading of the same or shallower depth -- except a repeated or
    re-qualified occurrence of the *same* heading, which extends the section
    rather than truncating it at the first of the pair.
    """
    return _locate(lines, as_matcher(heading))[0]


def section_body(
    lines: Sequence[str], heading: str | HeadingMatcher
) -> list[str] | None:
    """Return the body lines of `heading`'s section, or None if absent.

    The heading line and any repeated occurrence of it are dropped; everything
    else in the section, fenced content included, is preserved verbatim.
    """
    matcher = as_matcher(heading)
    bounds, flags = _locate(lines, matcher)
    if bounds is None:
        return None
    start, end = bounds
    return [
        line
        for i, line in enumerate(lines[start:end], start=start)
        if flags[i] or not matcher.matches(line)
    ]


def has_heading(lines: Sequence[str], heading: str | HeadingMatcher) -> bool:
    """True if `heading` appears outside any fenced code block."""
    return find_section(lines, heading) is not None


def leaf_body(lines: Sequence[str], heading: str | HeadingMatcher) -> list[str] | None:
    """Like `section_body`, but also stops at the first nested heading.

    For a heading whose section is a data leaf (a table, a short note) rather
    than a container for sub-headings, a nested heading inside its body is
    drift -- a hallucinated sub-topic, not more of the section's data -- so
    scanning must not continue past it.
    """
    body = section_body(lines, heading)
    if body is None:
        return None
    flags = list(fence_flags(body))
    for i, line in enumerate(body):
        if not flags[i] and parse_heading(line) is not None:
            return body[:i]
    return body


# -- HTML entity normalisation -------------------------------------------------

# The five HTML-special characters, in every escaped form a standard escaper
# emits: the named references, plus decimal and hex numeric ones (Python's
# html.escape produces &#x27; for the apostrophe, Go and ERB produce &#39;).
_NAMED_SPECIALS = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
_CODEPOINT_SPECIALS = {0x22: '"', 0x26: "&", 0x27: "'", 0x3C: "<", 0x3E: ">"}

_SPECIAL_ENTITY_RE = re.compile(
    r"&(?:(lt|gt|amp|quot|apos)|#(?:([0-9]{1,7})|[xX]([0-9A-Fa-f]{1,6})));",
    re.IGNORECASE,
)

# Any surviving entity reference, named or numeric, for the residual scan.
_ANY_ENTITY_RE = re.compile(
    r"&(?:[A-Za-z][A-Za-z0-9]{1,30}|#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6});"
)


def _resolve_special(match: re.Match[str]) -> str:
    name, decimal, hexadecimal = match.groups()
    if name is not None:
        return _NAMED_SPECIALS[name.lower()]
    codepoint = int(decimal) if decimal is not None else int(hexadecimal, 16)
    # A numeric reference to anything else (&#916; for Δ) is left alone and
    # picked up by `find_entities` instead -- decoding arbitrary codepoints is
    # a wider licence than recovering markdown syntax needs.
    return _CODEPOINT_SPECIALS.get(codepoint, match.group(0))


def unescape_specials(text: str) -> str:
    """Decode escaped `<`, `>`, `&`, `"` and `'` back to literal characters.

    A payload that arrives HTML-escaped loses its markdown syntax: a blockquote
    marker written `&gt;` renders as literal text instead of opening a quote
    block. Reports assembled from LLM-authored prose have no legitimate use for
    an entity reference, so recovering these five is safe -- but only these
    five, so that a numeric reference to some other character survives to be
    reported rather than silently rewritten.

    Decoding repeats to a fixed point, recovering a doubly-escaped payload
    (`&amp;gt;`) in one call. It terminates because every replacement is
    strictly shorter than what it replaces.
    """
    while True:
        decoded = _SPECIAL_ENTITY_RE.sub(_resolve_special, text)
        if decoded == text:
            return text
        text = decoded


def find_entities(text: str) -> list[tuple[int, str]]:
    """Return `(line number, entity)` for every entity reference in `text`.

    Line numbers are 1-based. Run after `unescape_specials` to surface escaping
    it deliberately does not undo, so a payload mangled in a shape this module
    does not recognise is visible at the point of writing rather than when
    someone reads a broken report days later.
    """
    return [
        (number, match.group(0))
        for number, line in enumerate(text.split("\n"), start=1)
        for match in _ANY_ENTITY_RE.finditer(line)
    ]
