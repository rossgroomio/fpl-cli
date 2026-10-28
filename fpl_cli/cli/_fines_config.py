"""Fines configuration parsing and types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fpl_cli.api.premier_league import award_month, award_month_key
from fpl_cli.cli._context import ConfigError

VALID_RULE_TYPES = frozenset({"last-place", "red-card", "below-threshold"})

# The draft house rule that gives the manager owning the Premier League's
# Player of the Month at the month's end one fine to hand out (#492).
PLAYER_OF_THE_MONTH_VOUCHER = "player-of-the-month"
VALID_VOUCHER_TYPES = frozenset({PLAYER_OF_THE_MONTH_VOUCHER})
# Everything a voucher carries. Checked, unlike a rule's keys, because the
# key a typo drops is the override the "not checked" warnings send the user
# to set, and the feed would go on reading "not checked" with no hint why.
VALID_VOUCHER_KEYS = frozenset({"type", "penalty", "penalty_phrasings", "overrides"})

# The whole of the block `fpl init` writes and the example in defaults.yaml
# documents. Checked, unlike the keys inside a rule, because the two typos
# fail differently: a stray key in a rule leaves the rule parsed and running,
# while `clasic:` empties that format's rule list and reads downstream as "no
# fines configured" -- which stamps `fine_rules_evaluated: []` onto the ledger
# for a gameweek whose rules were never read (#136's false acquittal, and
# append-only). A rule list that silently disappears has to be an error.
VALID_FINES_KEYS = frozenset({
    "classic", "draft", "draft_vouchers", "escalation_note", "penalty_phrasings",
})

# What a rule carries when its `penalty:` is left out. Nobody chose it, so it
# is no league's forfeit and the editorial is never told to vary it (#503).
DEFAULT_PENALTY = "Fine triggered"


def _named_forfeit(penalty: str) -> str | None:
    """`penalty` as the league configured it, or None where it named none:
    left unset, or set blank. Only a named forfeit is the editorial's to
    vary."""
    penalty = penalty.strip()
    return penalty if penalty and penalty != DEFAULT_PENALTY else None


@dataclass(frozen=True)
class FineRule:
    """A single fine rule from config."""

    type: str
    penalty: str = DEFAULT_PENALTY
    # `int | float` because a float threshold compares correctly and always
    # has -- narrowing it to `int` now would reject a settings.yaml that
    # works today.
    threshold: int | float | None = None
    # The league's own ways of referring to `penalty`, which the editorial
    # varies between instead of repeating it word for word (#503). None means
    # the rule set no list of its own; an empty tuple is a list set empty,
    # which opts the rule's forfeit out of the block's.
    penalty_phrasings: tuple[str, ...] | None = None

    @property
    def forfeit(self) -> str | None:
        """The penalty as the league configured it, or None where it named
        none: left unset, or set blank. Only a named forfeit is the
        editorial's to vary."""
        return _named_forfeit(self.penalty)


@dataclass(frozen=True)
class VoucherRule:
    """A fine one manager may hand out to anyone, never one ruled against
    them (#492).

    Kept apart from `FineRule` rather than added as a rule type: every rule
    type rules against a manager (`_RULE_HANDLERS` in `_fines.py`), so a
    voucher there would be recorded, and counted by `fpl league-fines`, as a
    fine on the manager who earned it. Nothing here reaches the ledger.
    """

    type: str
    penalty: str = DEFAULT_PENALTY
    # As a rule's (#503): the league's own ways of naming `penalty`.
    penalty_phrasings: tuple[str, ...] | None = None
    # The winner by month ("2026-09" -> his FPL `code`), for when the awards
    # feed breaks or names the wrong man. Wins over the feed.
    overrides: Mapping[str, int] = field(default_factory=dict)

    @property
    def forfeit(self) -> str | None:
        """The penalty as the league configured it, or None where it named none."""
        return _named_forfeit(self.penalty)


@dataclass(frozen=True)
class FinesConfig:
    """Parsed fines configuration."""

    classic: list[FineRule] = field(default_factory=list)
    draft: list[FineRule] = field(default_factory=list)
    escalation_note: str | None = None
    # Phrasings for the one forfeit no rule-level list claims.
    penalty_phrasings: tuple[str, ...] = ()
    # Grants rather than rules, read by the draft recap alone (#492).
    draft_vouchers: list[VoucherRule] = field(default_factory=list)

    def draft_voucher(self, voucher_type: str) -> VoucherRule | None:
        """The configured draft voucher of `voucher_type`, if any. At most
        one each: `parse_fines_config` refuses a second."""
        return next((voucher for voucher in self.draft_vouchers if voucher.type == voucher_type), None)

    def phrasings_for(self, forfeit: str) -> tuple[str, ...]:
        """The phrasings the editorial may use for `forfeit`.

        Keyed on the forfeit, not the rule, because that is what the
        editorial names: two rules carrying one forfeit share one entry in
        the prompt. A forfeit any rule-level list claims takes those lists
        alone, merged, and never the block's -- a rule's own list replaces
        the block's, and an empty one opts out. Otherwise the block's list,
        which `parse_fines_config` only accepts when it can describe a
        single forfeit. A voucher's list counts as a rule's: its forfeit is
        named in the same prompt section.
        """
        own = [
            rule.penalty_phrasings for rule in (*self.classic, *self.draft, *self.draft_vouchers)
            if rule.forfeit == forfeit and rule.penalty_phrasings is not None
        ]
        if not own:
            return self.penalty_phrasings
        merged: list[str] = []
        for phrasings in own:
            merged.extend(p for p in phrasings if p not in merged)
        return tuple(merged)


def parse_fines_config(settings: dict[str, Any]) -> FinesConfig | None:
    """Parse fines config from settings dict.

    Returns None when no fines are configured.

    Every way a hand-edited `fines:` block can be wrong raises `ConfigError`
    and nothing else, so one boundary per command (`config_failure_boundary`)
    covers the lot. That is why the shape checks below exist alongside the
    rule checks: a list where a mapping belongs used to reach `.get` and raise
    `AttributeError`, which no boundary catches and which reads as a crash
    rather than as the config mistake it is (#170).
    """
    fines_raw = settings.get("fines")
    if not fines_raw:
        return None

    if not isinstance(fines_raw, dict):
        msg = (
            f"'fines' must be a mapping holding 'classic' and/or 'draft' rule lists, "
            f"got {type(fines_raw).__name__}"
        )
        raise ConfigError(msg)

    unknown = sorted(set(fines_raw) - VALID_FINES_KEYS)
    if unknown:
        msg = (
            f"Unknown key(s) in 'fines': {', '.join(repr(k) for k in unknown)}. "
            f"Valid keys: {', '.join(sorted(VALID_FINES_KEYS))}"
        )
        raise ConfigError(msg)

    classic_raw = _rule_list(fines_raw.get("classic"), "classic")
    draft_raw = _rule_list(fines_raw.get("draft"), "draft")
    vouchers_raw = _rule_list(fines_raw.get("draft_vouchers"), "draft_vouchers", noun="vouchers")

    # A block of vouchers alone is still a block: it configures no rule, so
    # every reader of the rule lists sees the empty ones it always saw.
    if not classic_raw and not draft_raw and not vouchers_raw:
        return None

    classic = [_parse_rule(r, "classic") for r in classic_raw]
    draft = [_parse_rule(r, "draft") for r in draft_raw]
    draft_vouchers = [_parse_voucher(v) for v in vouchers_raw]
    _check_one_voucher_each(draft_vouchers)
    escalation_note = fines_raw.get("escalation_note")
    penalty_phrasings = _phrasings(fines_raw.get("penalty_phrasings"), "'fines.penalty_phrasings'") or ()
    if penalty_phrasings:
        _check_block_phrasings_name_one_forfeit([*classic, *draft, *draft_vouchers])

    return FinesConfig(
        classic=classic,
        draft=draft,
        escalation_note=escalation_note,
        penalty_phrasings=penalty_phrasings,
        draft_vouchers=draft_vouchers,
    )


def _check_block_phrasings_name_one_forfeit(rules: Sequence[FineRule | VoucherRule]) -> None:
    """Refuse a block-level list that would have to describe two forfeits.

    The block's list covers every forfeit no rule-level list claims, so with
    "Pint on video" on one rule and "Buy the round" on another it would hand
    "pint of shame" to the editorial as a way of saying "Buy the round" --
    changing the forfeit, which the prompt forbids. No reading of the list
    can be right there, so it is a config mistake, not a guess to make.
    """
    claimed = {rule.forfeit for rule in rules if rule.penalty_phrasings is not None}
    unclaimed = sorted({
        forfeit for rule in rules
        if (forfeit := rule.forfeit) is not None and forfeit not in claimed
    })
    if len(unclaimed) > 1:
        msg = (
            "'fines.penalty_phrasings' covers every forfeit without a list of its own, but "
            f"the rules carry {len(unclaimed)} different ones ({', '.join(repr(f) for f in unclaimed)}). "
            "Give each rule its own 'penalty_phrasings' instead"
        )
        raise ConfigError(msg)


def _phrasings(raw: Any, where: str) -> tuple[str, ...] | None:
    """A `penalty_phrasings` list, each entry stripped, blanks and repeats
    dropped; None where the key is absent.

    Checked rather than coerced: a bare string where the list belongs would
    otherwise be read a character at a time, and the editorial would be told
    the league calls its forfeit "p", "i" and "n".
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        msg = f"{where} must be a list of strings, got {type(raw).__name__}"
        raise ConfigError(msg)
    phrasings: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            msg = f"{where} must be a list of strings, got an entry of type {type(item).__name__}"
            raise ConfigError(msg)
        phrasing = item.strip()
        if phrasing and phrasing not in phrasings:
            phrasings.append(phrasing)
    return tuple(phrasings)


def _rule_list(raw: Any, key: str, *, noun: str = "rules") -> list[Any]:
    """The rules (or vouchers) configured under one key, still unparsed."""
    if not raw:
        return []
    if not isinstance(raw, list):
        msg = f"'fines.{key}' must be a list of {noun}, got {type(raw).__name__}"
        raise ConfigError(msg)
    return raw


def _parse_voucher(raw: Any) -> VoucherRule:
    """Parse one `draft_vouchers` entry into a VoucherRule."""
    if not isinstance(raw, dict):
        msg = f"Voucher in 'draft_vouchers' must be a mapping with a 'type' field, got {type(raw).__name__}"
        raise ConfigError(msg)

    voucher_type = raw.get("type")
    if not voucher_type:
        msg = "Voucher in 'draft_vouchers' is missing required 'type' field"
        raise ConfigError(msg)
    # The type is checked as a string first: `in` hashes it, and a list there
    # (a YAML slip) would raise `TypeError` past the config boundary (#170).
    if not isinstance(voucher_type, str) or voucher_type not in VALID_VOUCHER_TYPES:
        msg = f"Unknown voucher type {voucher_type!r}. Valid types: {', '.join(sorted(VALID_VOUCHER_TYPES))}"
        raise ConfigError(msg)
    unknown = sorted((key for key in raw if key not in VALID_VOUCHER_KEYS), key=str)
    if unknown:
        msg = (
            f"Unknown key(s) in voucher '{voucher_type}': {', '.join(repr(k) for k in unknown)}. "
            f"Valid keys: {', '.join(sorted(VALID_VOUCHER_KEYS))}"
        )
        raise ConfigError(msg)

    penalty = raw.get("penalty", DEFAULT_PENALTY)
    if not isinstance(penalty, str):
        msg = f"Voucher '{voucher_type}' penalty must be a string, got {type(penalty).__name__}"
        raise ConfigError(msg)

    return VoucherRule(
        type=voucher_type,
        penalty=penalty,
        penalty_phrasings=_phrasings(
            raw.get("penalty_phrasings"), f"Voucher '{voucher_type}' penalty_phrasings",
        ),
        overrides=_voucher_overrides(raw.get("overrides"), voucher_type),
    )


def _voucher_overrides(raw: Any, voucher_type: str) -> dict[str, int]:
    """A voucher's `overrides`, month ("2026-09") to player code, the months
    normalised to two digits; empty where the key is absent.

    Checked rather than coerced, like `threshold`: a quoted code would never
    match a squad's integer one, so the override would quietly name nobody,
    and `2026-09-01` (which YAML reads as a date) is no month.
    """
    if raw is None:
        return {}
    where = f"Voucher '{voucher_type}' overrides"
    if not isinstance(raw, dict):
        msg = f'{where} must map a month ("2026-09") to a player code, got {type(raw).__name__}'
        raise ConfigError(msg)
    overrides: dict[str, int] = {}
    # How each month was written, so two spellings of one month ("2026-9"
    # and "2026-09") are refused rather than the later quietly winning.
    written: dict[str, str] = {}
    for month, code in raw.items():
        named = award_month(month)
        if named is None:
            msg = f'{where}: {month!r} is not a month. Write it as "2026-09"'
            raise ConfigError(msg)
        if isinstance(code, bool) or not isinstance(code, int) or code <= 0:
            msg = (
                f"{where}: the player for {month} must be his FPL code, a whole number "
                f"(`code` in `fpl player <name> --format json`), got {code!r}"
            )
            raise ConfigError(msg)
        key = award_month_key(*named)
        if key in written:
            msg = f"{where}: {written[key]!r} and {month!r} are the same month. Set it once"
            raise ConfigError(msg)
        written[key] = month
        overrides[key] = code
    return overrides


def _check_one_voucher_each(vouchers: Sequence[VoucherRule]) -> None:
    """Refuse a voucher type listed twice: each would announce the same
    award, with no telling which penalty the holder hands out."""
    seen: set[str] = set()
    for voucher in vouchers:
        if voucher.type in seen:
            msg = f"'fines.draft_vouchers' lists '{voucher.type}' more than once"
            raise ConfigError(msg)
        seen.add(voucher.type)


def _parse_rule(raw: Any, format_name: str) -> FineRule:
    """Parse a single rule dict into a FineRule."""
    if not isinstance(raw, dict):
        msg = (
            f"Fine rule in '{format_name}' must be a mapping with a 'type' field, "
            f"got {type(raw).__name__}"
        )
        raise ConfigError(msg)

    rule_type = raw.get("type")
    if not rule_type:
        msg = f"Fine rule in '{format_name}' is missing required 'type' field"
        raise ConfigError(msg)

    if rule_type not in VALID_RULE_TYPES:
        msg = f"Unknown fine rule type '{rule_type}'. Valid types: {', '.join(sorted(VALID_RULE_TYPES))}"
        raise ConfigError(msg)

    penalty = raw.get("penalty", DEFAULT_PENALTY)
    if not isinstance(penalty, str):
        msg = f"Fine rule '{rule_type}' penalty must be a string, got {type(penalty).__name__}"
        raise ConfigError(msg)

    threshold = raw.get("threshold")
    if rule_type == "below-threshold" and threshold is None:
        msg = "Fine rule 'below-threshold' requires a 'threshold' value"
        raise ConfigError(msg)

    # Checked for the same reason `penalty` is, and more urgently: this is the
    # one config value that ends up in an arithmetic comparison, so `threshold:
    # "40"` -- a quoted number is an ordinary YAML slip -- reaches `user_pts <
    # rule.threshold` as a `TypeError` that nothing reports. `status` catches it
    # and drops the whole section from the payload; `evaluate_league_fines`
    # logs it per manager and records nobody as ruled. A bool is rejected with
    # the rest: `True` is an `int` to Python and would compare as 1, so every
    # score above zero silently clears a threshold nobody meant to set.
    if threshold is not None and (
        isinstance(threshold, bool) or not isinstance(threshold, (int, float))
    ):
        msg = (
            f"Fine rule '{rule_type}' threshold must be a number, "
            f"got {type(threshold).__name__}"
        )
        raise ConfigError(msg)

    return FineRule(
        type=rule_type,
        penalty=penalty,
        threshold=threshold,
        penalty_phrasings=_phrasings(
            raw.get("penalty_phrasings"), f"Fine rule '{rule_type}' penalty_phrasings",
        ),
    )
