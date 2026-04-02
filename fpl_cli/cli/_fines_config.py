"""Fines configuration parsing and types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALID_RULE_TYPES = frozenset({"last-place", "red-card", "below-threshold"})


@dataclass(frozen=True)
class FineRule:
    """A single fine rule from config."""

    type: str
    penalty: str = "Fine triggered"
    threshold: int | None = None


@dataclass(frozen=True)
class FinesConfig:
    """Parsed fines configuration."""

    classic: list[FineRule] = field(default_factory=list)
    draft: list[FineRule] = field(default_factory=list)
    escalation_note: str | None = None


def parse_fines_config(settings: dict[str, Any]) -> FinesConfig | None:
    """Parse fines config from settings dict.

    Returns None when no fines are configured.
    """
    fines_raw = settings.get("fines")
    if not fines_raw:
        return None

    classic_raw = fines_raw.get("classic") or []
    draft_raw = fines_raw.get("draft") or []

    if not classic_raw and not draft_raw:
        return None

    classic = [_parse_rule(r, "classic") for r in classic_raw]
    draft = [_parse_rule(r, "draft") for r in draft_raw]
    escalation_note = fines_raw.get("escalation_note")

    return FinesConfig(classic=classic, draft=draft, escalation_note=escalation_note)


def _parse_rule(raw: dict[str, Any], format_name: str) -> FineRule:
    """Parse a single rule dict into a FineRule."""
    rule_type = raw.get("type")
    if not rule_type:
        msg = f"Fine rule in '{format_name}' is missing required 'type' field"
        raise ValueError(msg)

    if rule_type not in VALID_RULE_TYPES:
        msg = f"Unknown fine rule type '{rule_type}'. Valid types: {', '.join(sorted(VALID_RULE_TYPES))}"
        raise ValueError(msg)

    penalty = raw.get("penalty", "Fine triggered")
    if not isinstance(penalty, str):
        msg = f"Fine rule '{rule_type}' penalty must be a string, got {type(penalty).__name__}"
        raise ValueError(msg)

    threshold = raw.get("threshold")
    if rule_type == "below-threshold" and threshold is None:
        msg = "Fine rule 'below-threshold' requires a 'threshold' value"
        raise ValueError(msg)

    return FineRule(
        type=rule_type,
        penalty=penalty,
        threshold=threshold,
    )
