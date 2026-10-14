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

Section scanning is fence-aware throughout: a `#` line inside a fenced code
block is code, not a heading, so it neither opens nor closes a section.

The same module also carries entity normalisation, the other way markdown
arrives damaged from an LLM return path: a payload escaped somewhere in transit
loses its syntax, most visibly a `&gt;` blockquote marker that renders as
literal text. `unescape_specials` recovers it and `find_entities` reports what
it deliberately left alone.

Those two are the deliberate exception to the fence-awareness above, and it is
not an oversight. Section scanning must respect fences because a fenced `#` is
content that only *looks* like structure. Escaping is the reverse: it is
applied to the whole payload in transit, fenced regions included, so a
fence-aware decoder would leave exactly the content it skipped still broken.
The cost is that a fence deliberately displaying an escaped entity as an
example would be decoded too -- accepted, because a report of football
recommendations has no such example to protect, whereas a mangled fence in an
escaped payload is the failure being repaired.

`HeadingMatcher` assumes a fixed target heading text. A caller that needs to
locate a heading matching a pattern instead (e.g. one embedding a variable
gameweek number) should match that heading itself with its own regex, then use
`fence_flags`/`parse_heading` directly to compute a fence-aware, same-or-
shallower-depth section boundary from it -- the same primitives `_locate`
composes here.

Once a section is found, what gw-prep and squad-builder put in it is mostly
tables of players, so the module also reads those: `iter_tables` and
`split_cells` for the rows, `pipe_rows` to tell what a malformed table
left unread, and `split_name_team` / `club_code` for the
"Nyoni (LIV, MID)" cells in them. The recommendations parser behind
`fpl review --compare-recs` and the gw-prep validator scripts each used to
carry their own copy, and the copies disagreed on what ends a table and what a
club suffix looks like (#402). How a caller matches a column name or a player
name stays with the caller, since those differ on purpose.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

__all__ = [
    "HeadingMatcher",
    "MarkdownTable",
    "as_matcher",
    "club_code",
    "fence_flags",
    "find_entities",
    "find_section",
    "has_heading",
    "iter_tables",
    "leaf_body",
    "parse_heading",
    "pipe_rows",
    "section_body",
    "split_cells",
    "split_name_team",
    "strip_cell_emphasis",
    "unescape_specials",
    "unwrap_emphasis",
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


# A run of one emphasis marker wrapping the whole text, closed by the same
# run: "**Bench**", "_Bench_", "`Bench`", "~~Bench~~".
_WRAPPING_EMPHASIS_RE = re.compile(r"^(\*{1,3}|_{1,3}|`+|~~)(.+)\1$", re.DOTALL)


def unwrap_emphasis(text: str) -> str:
    """Strip emphasis wrapping the whole of `text`, balanced pairs only.

    For text that will be printed again, unlike `_EMPHASIS_RE` above, which
    serves comparison and can afford to be greedy: a heading demoted into a
    report has to stay well-formed markdown, so an unbalanced marker
    ("**Bench") is left alone, and so is a pair that only looks like a wrap
    because two separate spans share the line ("**a** and **b**"). Nested
    wraps unwrap layer by layer.
    """
    text = text.strip()
    while (match := _WRAPPING_EMPHASIS_RE.match(text)) is not None:
        marker, inner = match.group(1), match.group(2)
        if marker in inner:
            break
        text = inner.strip()
    return text


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


# -- Tables --------------------------------------------------------------------

# A delimiter row: every cell a run of dashes, optionally colon-aligned.
_SEPARATOR_RE = re.compile(r"^\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?$")
# A delimiter written the way one is in practice, three dashes a cell or more.
# Mid-table, only this shape starts a stacked table; anything shorter is a
# placeholder row.
_STACKED_DELIMITER_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?$")
# A pipe that breaks a column, as opposed to an escaped `\|` inside a cell.
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


@dataclass(frozen=True)
class MarkdownTable:
    """A markdown table found by `iter_tables`.

    `headers` are the header cells trimmed and with `\\|` unescaped, but
    otherwise as written, so each caller decides how loosely to match a column
    name. `heading` is the nearest heading line above the table, or None.
    """

    headers: list[str]
    rows: list[list[str]]
    heading: str | None


def split_cells(line: str) -> list[str]:
    """A table row's cells, trimmed, with an escaped `\\|` kept inside its cell."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _CELL_SPLIT_RE.split(inner)]


def _is_separator(line: str) -> bool:
    return _SEPARATOR_RE.match(line.strip()) is not None


def _is_pipe_row(line: str) -> bool:
    return line.strip().startswith("|")


def iter_tables(lines: Sequence[str]) -> Iterator[MarkdownTable]:
    """Every markdown table in `lines`, with the heading it sits under.

    A table opens on a pipe row followed by a delimiter row and runs to the
    first line that is not a pipe row. Inside it, a row shaped like a delimiter
    is a placeholder (`| - | - |`) and is skipped, so it never ends the table
    and costs the real row above it (#402); a row with every cell empty is
    skipped too. The exception is a row followed by a full delimiter, three
    dashes a cell or more: that row heads a second table stacked straight
    under the first, and reading it as more of the first fed a different
    table's rows to whoever wanted the first. So a placeholder written as
    `|---|---|` reads as a stacked header -- rarer than a stacked table, and
    the price of telling the two apart at all.

    Fence-aware, so a table quoted inside a code block is not read as one.
    """
    fenced = list(fence_flags(lines))
    heading: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if fenced[i]:
            i += 1
            continue
        if parse_heading(line) is not None:
            heading = line
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            rows: list[list[str]] = []
            j = i + 2
            while j < len(lines) and not fenced[j] and _is_pipe_row(lines[j]):
                if _is_separator(lines[j]):
                    j += 1
                    continue
                if j + 1 < len(lines) and _STACKED_DELIMITER_RE.match(lines[j + 1].strip()):
                    break  # row j heads a table stacked straight under this one
                cells = split_cells(lines[j])
                if any(cells):
                    rows.append(cells)
                j += 1
            yield MarkdownTable(headers=split_cells(line), rows=rows, heading=heading)
            i = j
            continue
        i += 1


def pipe_rows(lines: Sequence[str]) -> list[str]:
    """Every table row in `lines` that carries a value, read into a table or not.

    The rows `iter_tables` reads as headers and body rows, plus any it could
    not place: a header with no `|---|` row under it (or an em-dash one), or
    rows a blank line cut off from their header. A caller that reads one table
    can set this against what it read, and say so when rows went unread rather
    than report a clean result. Fenced lines, delimiter-shaped rows and rows
    with every cell empty are left out, as `iter_tables` leaves them out.
    """
    fenced = list(fence_flags(lines))
    return [
        line
        for i, line in enumerate(lines)
        if not fenced[i] and _is_pipe_row(line) and not _is_separator(line) and any(split_cells(line))
    ]


# A club code is exactly three capitals in FPL ("ARS", "NFO"). Anything shorter
# is a captaincy marker ("(C)", "(VC)"), and a position code is three capitals
# too, so those are named and skipped: the waiver template's cells read
# "Nyoni (LIV, MID)". `GK` is named as well, though no code could match it, so
# that "(WHU, GK)" counts as a bracket of codes.
_TEAM_CODE_RE = re.compile(r"[A-Z]{3}")
_POSITION_CODES = frozenset({"GK", "GKP", "DEF", "MID", "FWD"})
_TRAILING_PAREN_RE = re.compile(r"\s*\(([^()]*)\)\s*$")
_CODE_SPLIT_RE = re.compile(r"[\s,/;]+")
# Emphasis wrapping a whole cell. Without the `~` a heading's `_EMPHASIS_RE`
# takes, so a struck-out name ("~~Salah~~") is never cleaned into a live one.
_CELL_EMPHASIS_RE = re.compile(r"^[*_`]+|[*_`]+$")
# The lineup template appends "⚡ Override: {reason}" to a player it forced in
# or out; the reason is prose, never part of the name.
_OVERRIDE_RE = re.compile(r"\s*⚡.*$")


def strip_cell_emphasis(text: str) -> str:
    """`text` with the emphasis markers wrapping it taken off: "**Drop**" -> "Drop".

    Greedy, unlike `unwrap_emphasis`: it serves comparison, never text that is
    printed again.
    """
    return _CELL_EMPHASIS_RE.sub("", text.strip()).strip()


def club_code(text: str | None) -> str | None:
    """`text` as a club code fit to compare ("ARS"), or None.

    None for a position code, and for the "???" the review writes when it
    could not resolve a club.
    """
    if not text:
        return None
    code = text.strip().upper()
    return code if _TEAM_CODE_RE.fullmatch(code) and code not in _POSITION_CODES else None


def _parenthetical_club(inner: str) -> str | None:
    """The club in a bracket that holds only codes: "(ARS)", "(LIV, MID)".

    Every token has to be a code. A bracket of prose, which the squad-builder
    captain line puts there by design ("Salah (home vs WHU)"), names an
    opponent or a tag as often as a club, and reading "WHU" off it ruled the
    real Salah out of his own captaincy.
    """
    tokens = [t for t in _CODE_SPLIT_RE.split(inner.strip()) if t]
    if not tokens or not all(_TEAM_CODE_RE.fullmatch(t) or t in _POSITION_CODES for t in tokens):
        return None
    return next((club_code(t) for t in tokens if club_code(t) is not None), None)


def split_name_team(cell: str) -> tuple[str, str | None]:
    """A player cell as (name, club code), the initial left on the name.

    Cells carry a club suffix ("Tzolis (ARS)", "Nyoni (LIV, MID)"), sometimes a
    qualifier ("Haaland (if fit)") or a captaincy marker ("Salah (C)"), and in
    the lineup tables an override note. All of them come off the name, but
    only a bracket of codes gives the club. The club code is worth keeping: it
    is what separates `Munoz` (LIV) from `Muñoz` (NFO) once the accent is
    folded. How far to fold the name itself is the caller's matching rule.
    """
    text = strip_cell_emphasis(_OVERRIDE_RE.sub("", cell))
    team: str | None = None
    while (match := _TRAILING_PAREN_RE.search(text)) is not None:
        team = team or _parenthetical_club(match.group(1))
        text = text[: match.start()].strip()
    return strip_cell_emphasis(text), team


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

    Unlike the section scanning above, this is deliberately *not* fence-aware:
    transit escaping hits fenced regions too, so skipping them would leave the
    content inside still broken.
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

    Not fence-aware, for the same reason `unescape_specials` isn't: a residual
    entity inside a fence is worth reporting too.
    """
    return [
        (number, match.group(0))
        for number, line in enumerate(text.split("\n"), start=1)
        for match in _ANY_ENTITY_RE.finditer(line)
    ]
