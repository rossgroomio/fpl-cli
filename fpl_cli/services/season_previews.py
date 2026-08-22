"""Season preview intel: hand-curated pre-season notes, decayed by gameweek.

Season previews carry the things the API and historical data cannot see in
August -- who is nailed on, who is injured into the autumn, who took over set
pieces, how strong a squad looks after the summer window. They are supplied by
the user (from whatever source they read) as one YAML file per team in
``user_config_dir()/previews/``; nothing is shipped but an example, because
preview prose is somebody else's copyright.

The load-bearing idea is **per-section decay**. A preview is not one artefact
with one expiry: an injury note is worthless once the API carries real news, a
projected XI is superseded by four gameweeks of real minutes, and a judgement
on squad strength stays useful until the team ratings have enough current-season
data to stand on their own. :data:`SECTION_DECAY` encodes those half-lives, so
callers ask for a gameweek and receive only the sections still worth reading,
each with a confidence between 0 and 1. Files stay on disk untouched all season
and stop influencing decisions on their own -- staleness is never a chore the
user has to remember.

Consumers must not re-derive that policy. :meth:`SeasonPreviewsService.coverage`
also decides, centrally, whether a partially-filled preview set may be used for
positive recommendations at all (see :data:`COVERAGE_THRESHOLD`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from fpl_cli.paths import SHIPPED_CONFIG_DIR, user_config_dir
from fpl_cli.season import get_season_year, season_label
from fpl_cli.utils.text import strip_diacritics

logger = logging.getLogger(__name__)

PREVIEWS_DIRNAME = "previews"
"""Directory name under the user config dir (and the shipped config dir)."""

EXAMPLE_STEM = "EXAMPLE"
"""Template file stem, skipped by the loader so an unedited copy stays quiet."""

SCHEMA_VERSION = 1
"""Bump when the file layout changes incompatibly; older files are rejected."""

PL_TEAM_COUNT = 20
"""Premier League team count, used as the coverage denominator by default."""

COVERAGE_THRESHOLD = 0.75
"""Fraction of teams that must have a preview before positive use is allowed.

Below this, a preview set is systematically biased: the teams the user bothered
to write up carry "nailed on, takes corners" annotations and the rest carry
nothing, so absence of a flag reads as absence of merit. Under the threshold the
intel is still useful as a *negative* filter (injuries, rotation risk) -- it just
must not promote anyone.
"""

SECTION_DECAY: dict[str, tuple[int, int]] = {
    # section: (full confidence through this GW, zero confidence from this GW)
    "injuries": (1, 2),
    "transfers": (3, 4),
    "projected_xi": (3, 7),
    "role_notes": (4, 9),
    "set_piece_duty": (6, 13),
    "team_strength": (6, 13),
    "narrative": (6, 13),
}
"""Per-section half-lives, in gameweeks, keyed to what supersedes each section.

- ``injuries`` -- the FPL API's own ``news`` / ``chance_of_playing`` fields are
  authoritative the moment the season starts.
- ``transfers`` -- the summer window shuts in early September; after that the
  bootstrap roster is the truth.
- ``projected_xi`` -- real ``minutes`` beat anybody's projection by GW7.
- ``role_notes`` -- a predicted role change is visible in the data by GW9.
- ``set_piece_duty`` -- takes most of the autumn to show up in returns.
- ``team_strength`` / ``narrative`` -- expire at GW13 to mirror
  ``team_ratings_prior.BLENDING_CUTOFF_GW = 12``, the last gameweek at which the
  ratings still blend in a prior at all. Past that the tool has enough real data
  and an August opinion is noise.
"""


def previews_dir() -> Path:
    """User previews directory.

    Resolved at call time, never bound to a module constant: an
    ``FPL_CLI_CONFIG_DIR`` set after import (or by ``.env``) must still be
    honoured, and ``tests/test_paths.py`` enforces it.
    """
    return user_config_dir() / PREVIEWS_DIRNAME


def example_file() -> Path:
    """Shipped template that ``fpl intel init`` copies into the user dir."""
    return SHIPPED_CONFIG_DIR / PREVIEWS_DIRNAME / f"{EXAMPLE_STEM}.yaml"


def section_confidence(section: str, gameweek: int) -> float:
    """Confidence in *section* at *gameweek*, from 1.0 down to 0.0.

    Full confidence through the section's plateau, then a linear taper to zero
    at its expiry. Gameweek is the right clock rather than the file's age: what
    makes a projected XI stale is the existence of real minutes, not how long
    ago somebody wrote it down.

    An unknown section is treated as expired rather than trusted forever -- a
    field this module does not know how to age out must not outlive the ones it
    does.
    """
    decay = SECTION_DECAY.get(section)
    if decay is None:
        return 0.0
    full_until, expires_at = decay
    if gameweek <= full_until:
        return 1.0
    if gameweek >= expires_at:
        return 0.0
    return round((expires_at - gameweek) / (expires_at - full_until), 3)


def live_sections(gameweek: int) -> list[str]:
    """Sections with any confidence left at *gameweek*."""
    return [s for s in SECTION_DECAY if section_confidence(s, gameweek) > 0]


def expired_sections(gameweek: int) -> list[str]:
    """Sections fully aged out at *gameweek*."""
    return [s for s in SECTION_DECAY if section_confidence(s, gameweek) <= 0]


PLAYER_FIELD_SECTIONS: dict[str, str] = {
    "status": "projected_xi",
    "injury": "injuries",
    "role_change": "role_notes",
    "set_pieces": "set_piece_duty",
    "penalties": "set_piece_duty",
    "new_signing": "projected_xi",  # superseded by real minutes, not the window closing
    "notes": "narrative",
}
"""Which decay section governs each emitted player field.

The single source for both emission gating in :meth:`PlayerNote.as_dict` and
the ``sections_present`` summary in :meth:`TeamPreview.as_dict`; a second copy
of this table in a display layer drifts the first time a field is added.
"""


class PlayerStatus(StrEnum):
    """How securely a player is expected to start."""

    STARTER = "starter"
    ROTATION = "rotation"
    FRINGE = "fringe"
    UNKNOWN = "unknown"


class Usability(StrEnum):
    """What a preview set may be used for, given its coverage."""

    FULL = "full"
    NEGATIVE_FILTER_ONLY = "negative_filter_only"
    NONE = "none"


@dataclass(frozen=True)
class Coverage:
    """How much of the league has a preview, and what that permits."""

    teams: int
    of: int
    usable_as: Usability

    @property
    def pct(self) -> float:
        """Fraction of the league covered, 0.0 when the denominator is zero."""
        return round(self.teams / self.of, 3) if self.of else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"teams": self.teams, "of": self.of, "pct": self.pct, "usable_as": self.usable_as.value}


@dataclass(frozen=True)
class PlayerNote:
    """Per-player intel. Every field beyond ``name`` is optional.

    ``code`` is the FPL ``element_code`` (stable across seasons), resolved at
    ingest time rather than read time so a mid-season transfer or a diacritic
    mismatch cannot silently drop a player later.
    """

    name: str
    code: int | None = None
    status: PlayerStatus = PlayerStatus.UNKNOWN
    injury: str | None = None
    role_change: str | None = None
    set_pieces: list[str] = field(default_factory=list)
    penalties: bool | None = None
    new_signing: bool = False
    notes: str | None = None

    def as_dict(self, gameweek: int) -> dict[str, Any] | None:
        """Player record with expired sections stripped, or None if nothing survives.

        A record reduced to a bare name carries no information, so it is dropped
        rather than emitted as noise for a consumer to filter again.
        """
        conf = {section: section_confidence(section, gameweek) for section in SECTION_DECAY}
        out: dict[str, Any] = {"name": self.name, "code": self.code}
        if conf["projected_xi"] > 0 and self.status is not PlayerStatus.UNKNOWN:
            out["status"] = self.status.value
        if conf["injuries"] > 0 and self.injury:
            out["injury"] = self.injury
        if conf["role_notes"] > 0 and self.role_change:
            out["role_change"] = self.role_change
        if conf["set_piece_duty"] > 0:
            if self.set_pieces:
                out["set_pieces"] = list(self.set_pieces)
            if self.penalties is not None:
                out["penalties"] = self.penalties
        # new_signing rides the projected_xi clock, not the transfers clock: the
        # team-level in/out lists are superseded by the roster the moment the
        # window shuts, but the flag's information -- "no PL stats because he
        # just arrived, not because he is fringe" -- is only superseded by real
        # minutes, and a deadline-day signing has none at the window close.
        if conf["projected_xi"] > 0 and self.new_signing:
            out["new_signing"] = True
        if conf["narrative"] > 0 and self.notes:
            out["notes"] = self.notes

        if len(out) <= 2:
            return None
        return out


@dataclass(frozen=True)
class TeamStrength:
    """Team-level judgements. Percentiles are 0-100; all fields optional."""

    attack: int | None = None
    defence: int | None = None
    set_pieces: int | None = None
    notes: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in ("attack", "defence", "set_pieces", "notes"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass(frozen=True)
class TeamPreview:
    """One team's preview, as loaded. Decay is applied on emission."""

    team: str
    source: str
    published: date
    author: str | None = None
    url: str | None = None
    predicted_finish: int | None = None
    team_strength: TeamStrength | None = None
    transfers_in: list[str] = field(default_factory=list)
    transfers_out: list[str] = field(default_factory=list)
    players: list[PlayerNote] = field(default_factory=list)
    narrative: str | None = None
    path: Path | None = None

    @property
    def has_content(self) -> bool:
        """Whether this preview says anything beyond its own provenance.

        A file with valid headers and nothing else -- a stub from
        ``fpl intel init`` that was never filled in -- must not count as
        coverage, or scaffolding the league would report 20/20 and unlock
        positive use with no intel behind it.
        """
        return bool(
            self.players
            or self.team_strength
            or self.narrative
            or self.predicted_finish is not None
            or self.transfers_in
            or self.transfers_out
        )

    def as_dict(self, gameweek: int) -> dict[str, Any]:
        """Emit this preview with sections expired at *gameweek* removed.

        ``section_confidence`` is emitted alongside so a consumer can weight a
        categorical field (``status: starter``) that cannot be scaled
        numerically the way a rating could. ``sections_present`` names the
        unexpired sections this file actually carries data for -- computed here,
        where the field-to-section mapping lives, so a display layer does not
        re-derive it from the emitted keys.
        """
        conf = {section: section_confidence(section, gameweek) for section in SECTION_DECAY}
        present: set[str] = set()
        out: dict[str, Any] = {
            "team": self.team,
            "source": self.source,
            "author": self.author,
            "url": self.url,
            "published": self.published.isoformat(),
        }

        if conf["team_strength"] > 0:
            if self.predicted_finish is not None:
                out["predicted_finish"] = self.predicted_finish
            if self.team_strength is not None:
                strength = self.team_strength.as_dict()
                if strength:
                    out["team_strength"] = strength
            if "predicted_finish" in out or "team_strength" in out:
                present.add("team_strength")

        if conf["transfers"] > 0:
            if self.transfers_in:
                out["transfers_in"] = list(self.transfers_in)
            if self.transfers_out:
                out["transfers_out"] = list(self.transfers_out)
            if self.transfers_in or self.transfers_out:
                present.add("transfers")

        if conf["narrative"] > 0 and self.narrative:
            out["narrative"] = self.narrative
            present.add("narrative")

        players = [p for p in (note.as_dict(gameweek) for note in self.players) if p is not None]
        if players:
            out["players"] = players
            for player in players:
                present.update(
                    PLAYER_FIELD_SECTIONS[key] for key in player if key in PLAYER_FIELD_SECTIONS
                )

        out["section_confidence"] = {s: c for s, c in conf.items() if c > 0}
        out["sections_present"] = sorted(present)
        return out


def _as_mapping(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _as_str(value: Any) -> str | None:
    """Non-empty string, or None. Numbers and dates are not coerced."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _as_str_list(value: Any) -> list[str]:
    """List of non-empty strings; a bare string is treated as a single entry."""
    if isinstance(value, str):
        single = _as_str(value)
        return [single] if single else []
    if not isinstance(value, list):
        return []
    return [text for text in (_as_str(item) for item in value) if text]


def _as_date(value: Any) -> date | None:
    """PyYAML gives a date for an unquoted ISO date; tolerate a quoted one too."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


_SEASON_RE = re.compile(r"^(\d{2}|\d{4})\s*[-/]\s*(\d{2}|\d{4})$")


def _normalise_season(value: Any) -> str | None:
    """Season label canonicalised to the ``2026-27`` form.

    Every reasonable spelling of a start/end year pair -- ``2026/27``,
    ``2026-2027``, ``26-27`` -- must map to the same label, or a current-season
    file gets rejected over punctuation with a warning claiming it is old. An
    unrecognised shape is kept verbatim so the staleness check still rejects it
    loudly rather than this function guessing at meaning.
    """
    text = _as_str(value)
    if not text:
        return None
    match = _SEASON_RE.match(text)
    if not match:
        return text.replace("/", "-")
    start, end = match.groups()
    if len(start) == 2:
        # Two-digit start years are unambiguous here: the FPL era is all 20xx.
        start = f"20{start}"
    return f"{start}-{end[-2:]}"


class SeasonPreviewsService:
    """Read-only loader for per-team season previews.

    One YAML file per team in :func:`previews_dir`, keyed by FPL short name
    (``ARS.yaml``). Unlike ``FixturePredictionsService`` there is no shipped
    fallback to layer over: preview content is entirely user-supplied, so a
    missing directory is the ordinary case and yields an empty set rather than a
    warning. A file that is unreadable, malformed, missing required fields, or
    from a previous season is skipped with the reason recorded in
    :attr:`load_warnings` for the CLI to surface -- silently ignoring a file its
    owner hand-wrote would be worse than saying nothing at all.

    Loading is pure: no network, no bootstrap data. Cross-checking team names
    and player codes against the real roster needs an API client, so it lives in
    the free functions below and is driven by the CLI.
    """

    def __init__(self, previews_path: Path | None = None):
        # Path resolution is deferred to first load so constructing the service
        # stays total and an FPL_CLI_CONFIG_DIR set after import is honoured.
        self._explicit_path = Path(previews_path) if previews_path is not None else None
        self._previews: list[TeamPreview] | None = None
        self._warnings: list[str] = []

    @property
    def previews_path(self) -> Path:
        """Directory the previews are read from."""
        return self._explicit_path if self._explicit_path is not None else previews_dir()

    @property
    def load_warnings(self) -> list[str]:
        """Reasons files were skipped (unreadable, malformed, incomplete, stale)."""
        self._load()
        return list(self._warnings)

    def _warn(self, message: str) -> None:
        """Record a skip reason for :attr:`load_warnings` to surface.

        Deliberately not logger.warning: fpl-cli configures no logging handlers,
        so a warning record reaches logging's lastResort handler and prints raw
        on top of whatever the CLI already displayed.
        """
        self._warnings.append(message)
        logger.debug("%s", message)

    def _load(self) -> list[TeamPreview]:
        if self._previews is not None:
            return self._previews

        directory = self.previews_path
        previews: list[TeamPreview] = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
                if path.stem.upper() == EXAMPLE_STEM:
                    continue
                preview = self._read(path)
                if preview is not None:
                    previews.append(preview)

        seen: dict[str, TeamPreview] = {}
        for preview in previews:
            if preview.team in seen:
                self._warn(
                    f"Ignoring preview {preview.path}: team {preview.team} already loaded"
                    f" from {seen[preview.team].path}"
                )
                continue
            seen[preview.team] = preview

        self._previews = sorted(seen.values(), key=lambda p: p.team)

        # Previews are typically extracted from a single source's predicted
        # table, where the finishes form a permutation -- two teams sharing one
        # usually means the table was misread during ingest. Warn, don't skip:
        # genuinely disagreeing sources are legitimate, just uncommon.
        by_finish: dict[int, list[str]] = {}
        for preview in self._previews:
            if preview.predicted_finish is not None:
                by_finish.setdefault(preview.predicted_finish, []).append(preview.team)
        for finish, teams in sorted(by_finish.items()):
            if len(teams) > 1:
                self._warn(
                    f"Previews for {', '.join(teams)} all predict finish {finish};"
                    f" from a single source's table this usually means one was misread"
                )

        return self._previews

    def _read(self, path: Path) -> TeamPreview | None:
        """Parse one preview file, or None when it is unusable (reason recorded)."""
        try:
            with open(path, encoding="utf-8") as handle:
                raw = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as exc:
            self._warn(f"Ignoring unreadable preview {path}: {exc}")
            return None

        data = _as_mapping(raw)
        if data is None:
            kind = "empty file" if raw is None else f"got {type(raw).__name__}"
            self._warn(f"Ignoring preview {path}: expected a mapping, {kind}")
            return None

        declared_version = data.get("schema_version", SCHEMA_VERSION)
        if declared_version != SCHEMA_VERSION:
            self._warn(
                f"Ignoring preview {path}: schema_version {declared_version!r}"
                f" is not {SCHEMA_VERSION}"
            )
            return None

        team = _as_str(data.get("team"))
        source = _as_str(data.get("source"))
        published = _as_date(data.get("published"))
        missing = [
            name
            for name, value in (("team", team), ("source", source), ("published", published))
            if value is None
        ]
        if missing:
            self._warn(f"Ignoring preview {path}: missing or malformed {', '.join(missing)}")
            return None
        # Narrow for the type checker: `missing` being empty proves these are set.
        assert team is not None and source is not None and published is not None

        if team.upper() == EXAMPLE_STEM:
            # The filename check in _load only covers the canonical EXAMPLE.yaml;
            # a renamed or duplicated copy ("EXAMPLE (1).yaml") would otherwise
            # load the fictional template content as real intel and count toward
            # the coverage gate.
            self._warn(
                f"Ignoring preview {path}: team is the {EXAMPLE_STEM} template sentinel --"
                f" copy the template to <TEAM>.yaml and set a real team code"
            )
            return None

        stale = self._stale_reason(data, published)
        if stale:
            self._warn(f"Ignoring preview {path}: {stale}")
            return None

        return TeamPreview(
            team=team.upper(),
            source=source,
            published=published,
            author=_as_str(data.get("author")),
            url=_as_str(data.get("url")),
            predicted_finish=self._read_finish(data.get("predicted_finish"), path),
            team_strength=self._read_strength(data.get("team_strength")),
            transfers_in=_as_str_list(data.get("transfers_in")),
            transfers_out=_as_str_list(data.get("transfers_out")),
            players=self._read_players(data.get("players"), path),
            narrative=_as_str(data.get("narrative")),
            path=path,
        )

    @staticmethod
    def _stale_reason(data: dict[str, Any], published: date) -> str | None:
        """Why the file describes a season that is no longer current, or None.

        The explicit ``season`` label wins when present -- it catches the copied
        file whose date was refreshed but whose contents were not. Otherwise the
        publication date decides, matching ``FixturePredictionsService``, with
        one carve-out: season previews are routinely written in May and June for
        the season that starts in August, so a date from May onward of the
        current season's start year counts as current even though it falls
        before the July season cutover.
        """
        declared = _normalise_season(data.get("season"))
        current = season_label()
        if declared:
            if declared != current:
                return f"it declares season {declared}, and the current season is {current}"
            return None
        season_year = get_season_year()
        if get_season_year(published) >= season_year:
            return None
        if published >= date(season_year, 5, 1):
            return None
        return f"published {published.isoformat()}, which is a previous season"

    def _read_finish(self, value: Any, path: Path) -> int | None:
        """Predicted league position, or None. Out-of-range values are dropped.

        A bool is rejected explicitly: Python makes ``True`` an ``int``, so a
        stray ``predicted_finish: yes`` would otherwise become 1st place.
        """
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= PL_TEAM_COUNT:
            self._warn(f"Dropping predicted_finish {value!r} in {path}: expected 1-{PL_TEAM_COUNT}")
            return None
        return value

    @staticmethod
    def _read_strength(value: Any) -> TeamStrength | None:
        data = _as_mapping(value)
        if data is None:
            return None

        def percentile(key: str) -> int | None:
            raw = data.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return None
            return int(raw) if 0 <= raw <= 100 else None

        strength = TeamStrength(
            attack=percentile("attack"),
            defence=percentile("defence"),
            set_pieces=percentile("set_pieces"),
            notes=_as_str(data.get("notes")),
        )
        return strength if strength.as_dict() else None

    def _read_players(self, value: Any, path: Path) -> list[PlayerNote]:
        if not isinstance(value, list):
            return []

        notes: list[PlayerNote] = []
        for entry in value:
            data = _as_mapping(entry)
            if data is None:
                self._warn(f"Dropping a player entry in {path}: expected a mapping")
                continue
            name = _as_str(data.get("name"))
            if name is None:
                self._warn(f"Dropping a player entry in {path}: missing name")
                continue

            raw_status = _as_str(data.get("status"))
            try:
                status = PlayerStatus(raw_status.lower()) if raw_status else PlayerStatus.UNKNOWN
            except ValueError:
                self._warn(
                    f"Dropping status {raw_status!r} for {name} in {path}:"
                    f" expected one of {', '.join(s.value for s in PlayerStatus)}"
                )
                status = PlayerStatus.UNKNOWN

            code = data.get("code")
            if isinstance(code, bool) or not isinstance(code, int):
                if code is not None:
                    # A quoted "123456" parses as a string; discarding it silently
                    # would leave the file permanently unfixable by --write, which
                    # skips entries that already carry any code value.
                    self._warn(
                        f"Dropping code {code!r} for {name} in {path}: expected a bare integer"
                    )
                code = None

            penalties = data.get("penalties")
            notes.append(
                PlayerNote(
                    name=name,
                    code=code,
                    status=status,
                    injury=_as_str(data.get("injury")),
                    role_change=_as_str(data.get("role_change")),
                    set_pieces=_as_str_list(data.get("set_pieces")),
                    penalties=penalties if isinstance(penalties, bool) else None,
                    new_signing=data.get("new_signing") is True,
                    notes=_as_str(data.get("notes")),
                )
            )
        return notes

    def get_previews(self) -> list[TeamPreview]:
        """All usable previews, sorted by team short name."""
        return list(self._load())

    def get_preview(self, team: str) -> TeamPreview | None:
        """One team's preview by short name (case-insensitive), or None."""
        wanted = team.strip().upper()
        return next((p for p in self._load() if p.team == wanted), None)

    def coverage(
        self,
        gameweek: int,
        total_teams: int = PL_TEAM_COUNT,
        valid_teams: set[str] | None = None,
    ) -> Coverage:
        """Coverage at *gameweek*, and the resulting usage policy.

        Centralised deliberately: three skills consume this intel, and a
        threshold restated in three markdown files drifts the first time one is
        edited.

        Gameweek matters because a full set of previews that has entirely aged
        out is worth exactly as much as no previews: reporting it as usable
        would invite a consumer to print usage guidance over an empty payload.

        *valid_teams*, when known, restricts the numerator to clubs actually in
        the league: a set carried forward across a relegation must not report
        full coverage on the strength of files for clubs that left. Offline the
        caller has no team list and every file counts, matching the rest of the
        command's graceful degradation.
        """
        teams = sum(
            1
            for preview in self._load()
            if preview.has_content and (not valid_teams or preview.team in valid_teams)
        )
        if teams == 0 or not live_sections(gameweek):
            usable = Usability.NONE
        elif total_teams and teams / total_teams >= COVERAGE_THRESHOLD:
            usable = Usability.FULL
        else:
            usable = Usability.NEGATIVE_FILTER_ONLY
        return Coverage(teams=teams, of=total_teams, usable_as=usable)

    def as_dicts(self, gameweek: int) -> list[dict[str, Any]]:
        """Every preview with content, decayed to *gameweek*, expired ones dropped."""
        emitted = [preview.as_dict(gameweek) for preview in self._load() if preview.has_content]
        return [entry for entry in emitted if entry.get("section_confidence")]


def unknown_teams(previews: list[TeamPreview], valid_short_names: set[str]) -> list[str]:
    """Preview team codes that match no Premier League team this season.

    Catches both a typo and a file left behind for a relegated side.
    """
    return sorted({p.team for p in previews if p.team not in valid_short_names})


def team_set_warning(
    previews: list[TeamPreview],
    valid_short_names: set[str],
    coverage: Coverage,
) -> str | None:
    """Describe drift between the preview set and the live team list, or None.

    Promotion and relegation are the one way a per-team file goes wrong without
    going stale by date, and a set copied forward and bulk-edited to the new
    season label passes every other check while still describing last season's
    twenty clubs.

    Which half of the diff is meaningful depends on how complete the set is.
    Below full coverage, a missing club is simply work the user has not done
    yet, so only the clubs that should not be there at all are worth naming. At
    full coverage a missing club is real drift, so the shared
    :func:`describe_team_set_mismatch` reports both directions -- the same
    message ``team_ratings.yaml`` and the review summariser use.
    """
    if not valid_short_names:
        return None

    if coverage.usable_as is Usability.FULL:
        from fpl_cli.utils.teams import describe_team_set_mismatch

        return describe_team_set_mismatch(
            f"{PREVIEWS_DIRNAME}/",
            [preview.team for preview in previews],
            valid_short_names,
            verb="covers",
        )

    extra = unknown_teams(previews, valid_short_names)
    if not extra:
        return None
    return f"{PREVIEWS_DIRNAME}/ still covers {', '.join(extra)}"


def unresolved_players(previews: list[TeamPreview]) -> list[tuple[str, str]]:
    """``(team, name)`` for players carrying no ``code``.

    Resolution belongs to ingest; anything unresolved here means a consumer
    cannot reliably join the note to a real player, so it is worth reporting.
    """
    return sorted((p.team, note.name) for p in previews for note in p.players if note.code is None)


# -- Name resolution ---------------------------------------------------------
#
# Preview prose names players the way a reader would ("Bruno G.", "Ødegaard",
# "Bruno Guimaraes"), which joins to nothing. Resolving those to element_code
# is deterministic work, so it belongs here rather than in an LLM ingest step
# that is good at reading prose and bad at remembering six-digit integers.


_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def normalise_name(value: str) -> str:
    """Casefold, strip accents and punctuation, and collapse whitespace.

    ``Ødegaard`` and ``Odegaard`` must resolve to the same player, and
    ``Bruno G.`` must not be separated from ``Bruno G`` by a full stop.
    Diacritic folding is :func:`fpl_cli.utils.text.strip_diacritics`, the same
    table every other cross-source name comparison uses -- it also covers the
    non-decomposable letters (Ø, Ł, Đ) that a bare NFKD pass leaves intact.
    """
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", strip_diacritics(value))).strip().lower()


@dataclass(frozen=True)
class NameMatch:
    """One resolution outcome for a preview player name."""

    name: str
    code: int | None = None
    matched_name: str | None = None
    how: str = "unmatched"
    candidates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "how": self.how}
        if self.code is not None:
            out["code"] = self.code
            out["matched_name"] = self.matched_name
        if self.candidates:
            out["candidates"] = list(self.candidates)
        return out


def _squad_index(squad: list[Any]) -> list[tuple[Any, set[str], list[str]]]:
    """``(player, normalised aliases, haystack tokens)`` computed once per squad.

    Normalisation does real unicode and regex work, so it must not be repeated
    per query name -- a preview resolves every note against the same squad.
    """
    index: list[tuple[Any, set[str], list[str]]] = []
    for player in squad:
        aliases = {
            normalise_name(player.web_name),
            normalise_name(player.second_name),
            normalise_name(f"{player.first_name} {player.second_name}"),
        }
        haystack = normalise_name(f"{player.first_name} {player.second_name} {player.web_name}")
        index.append((player, aliases, haystack.split()))
    return index


def _resolve_against_index(name: str, index: list[tuple[Any, set[str], list[str]]]) -> NameMatch:
    wanted = normalise_name(name)
    if not wanted:
        return NameMatch(name=name)

    exact: list[Any] = []
    partial: list[Any] = []
    for player, aliases, tokens in index:
        if wanted in aliases:
            exact.append(player)
            continue
        if all(any(token == part or part.startswith(token) for part in tokens) for token in wanted.split()):
            partial.append(player)

    for matches, how in ((exact, "exact"), (partial, "fuzzy")):
        if len(matches) == 1:
            return NameMatch(name=name, code=matches[0].code, matched_name=matches[0].web_name, how=how)
        if len(matches) > 1:
            return NameMatch(
                name=name,
                how="ambiguous",
                candidates=sorted(f"{p.web_name} ({p.code})" for p in matches),
            )
    return NameMatch(name=name)


def resolve_name(name: str, squad: list[Any]) -> NameMatch:
    """Match one preview name against a team's squad.

    *squad* holds ``Player`` models; only ``code``, ``web_name``,
    ``first_name`` and ``second_name`` are read, so any object carrying those
    attributes works.

    Exact match on a display or full name wins outright. Otherwise every query
    token must appear in the player's combined names, which is what carries
    ``Bruno Guimaraes`` to the player the game displays as ``Bruno G.``. An
    ambiguous result is reported as ambiguous rather than guessed: a silently
    wrong code is far worse than one the user is asked to fill in.
    """
    return _resolve_against_index(name, _squad_index(squad))


def resolve_preview_names(preview: TeamPreview, squad: list[Any], *, only_missing: bool = True) -> list[NameMatch]:
    """Resolve a preview's player names against *squad*.

    With *only_missing*, players that already carry a code are left alone --
    a hand-corrected code must survive a re-run.
    """
    index = _squad_index(squad)
    return [
        _resolve_against_index(note.name, index)
        for note in preview.players
        if not (only_missing and note.code is not None)
    ]


def write_resolved_codes(path: Path, matches: list[NameMatch], *, overwrite: bool = False) -> int:
    """Write resolved codes back into a preview file, preserving comments.

    Round-trip YAML: these files are hand-written and heavily commented, so
    reserialising them from plain dicts would destroy the user's own notes.
    Returns the number of codes written.

    By default an entry that already carries a valid integer code is left alone
    -- a hand-corrected code must survive a re-run. With *overwrite* (the
    ``--all`` path, which re-resolved those very entries) a differing resolved
    code replaces the existing one; without it, ``--all --write`` would display
    corrections it never saves. A code that is not a bare integer (a quoted
    string, say) never counts as present: the loader discards it, so keeping it
    would leave the file permanently unfixable.
    """
    from ruamel.yaml import YAML

    from fpl_cli.utils.files import atomic_write_text

    by_name = {m.name: m.code for m in matches if m.code is not None}
    if not by_name:
        return 0

    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True  # type: ignore[assignment]
    # Match the indentation the shipped example and `fpl intel init` use, so a
    # --write does not reflow every list in a file the user hand-formatted.
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return 0

    written = 0
    for entry in data.get("players") or []:
        if not isinstance(entry, dict):
            continue
        existing = entry.get("code")
        has_valid_code = isinstance(existing, int) and not isinstance(existing, bool)
        if has_valid_code and not overwrite:
            continue
        entry_name = entry.get("name")
        # The loader strips names (`_as_str`), so match keys are stripped; the
        # raw scalar may not be when the user quoted surrounding whitespace.
        code = by_name.get(entry_name.strip()) if isinstance(entry_name, str) else None
        if code is not None and code != existing:
            entry["code"] = code
            written += 1

    if written:
        from io import StringIO

        buffer = StringIO()
        yaml_rt.dump(data, buffer)
        atomic_write_text(path, buffer.getvalue())
    return written
