"""Fines configuration parsing and types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fpl_cli.cli._context import ConfigError

VALID_RULE_TYPES = frozenset({"last-place", "red-card", "below-threshold"})

# The whole of the block `fpl init` writes and the example in defaults.yaml
# documents. Checked, unlike the keys inside a rule, because the two typos
# fail differently: a stray key in a rule leaves the rule parsed and running,
# while `clasic:` empties that format's rule list and reads downstream as "no
# fines configured" -- which stamps `fine_rules_evaluated: []` onto the ledger
# for a gameweek whose rules were never read (#136's false acquittal, and
# append-only). A rule list that silently disappears has to be an error.
VALID_FINES_KEYS = frozenset({"classic", "draft", "escalation_note"})


@dataclass(frozen=True)
class FineRule:
    """A single fine rule from config."""

    type: str
    penalty: str = "Fine triggered"
    # `int | float` because a float threshold compares correctly and always
    # has -- narrowing it to `int` now would reject a settings.yaml that
    # works today.
    threshold: int | float | None = None


@dataclass(frozen=True)
class FinesConfig:
    """Parsed fines configuration."""

    classic: list[FineRule] = field(default_factory=list)
    draft: list[FineRule] = field(default_factory=list)
    escalation_note: str | None = None


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

    if not classic_raw and not draft_raw:
        return None

    classic = [_parse_rule(r, "classic") for r in classic_raw]
    draft = [_parse_rule(r, "draft") for r in draft_raw]
    escalation_note = fines_raw.get("escalation_note")

    return FinesConfig(classic=classic, draft=draft, escalation_note=escalation_note)


def _rule_list(raw: Any, format_name: str) -> list[Any]:
    """The rules configured for one format, still unparsed."""
    if not raw:
        return []
    if not isinstance(raw, list):
        msg = f"'fines.{format_name}' must be a list of rules, got {type(raw).__name__}"
        raise ConfigError(msg)
    return raw


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

    penalty = raw.get("penalty", "Fine triggered")
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
    )
