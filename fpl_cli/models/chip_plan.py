"""Chip planning models for tracking chip usage and planned chip plays."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fpl_cli.paths import DATA_DIR
from fpl_cli.season import CHIP_SPLIT_GW

logger = logging.getLogger(__name__)

CHIP_PLAN_FILE = DATA_DIR / "chip_plan.json"


class ChipType(str, Enum):
    """FPL chip types."""

    WILDCARD = "wildcard"
    FREE_HIT = "freehit"
    BENCH_BOOST = "bboost"
    TRIPLE_CAPTAIN = "3xc"


class PlannedChip(BaseModel):
    """A chip planned for a future gameweek."""

    chip: ChipType
    gameweek: int
    notes: str = ""

    model_config = ConfigDict(use_enum_values=True)


class UsedChip(BaseModel):
    """A chip that has been played, synced from FPL API."""

    chip: ChipType
    gameweek: int

    model_config = ConfigDict(use_enum_values=True)


class ChipPlan(BaseModel):
    """Chip planning state: planned chips and usage history."""

    chips: list[PlannedChip] = Field(default_factory=list)
    chips_used: list[UsedChip] = Field(default_factory=list)
    current_gw: int = 0
    last_updated: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )

    model_config = ConfigDict(use_enum_values=True)

    def get_available_chips(self, current_gw: int) -> list[ChipType]:
        """Get chips available in the current half of the season.

        Each chip is available once before the GW19 deadline and once after.
        """
        in_second_half = current_gw > CHIP_SPLIT_GW
        used_in_half = {
            u.chip
            for u in self.chips_used
            if (u.gameweek > CHIP_SPLIT_GW) == in_second_half
        }
        return [c for c in ChipType if c.value not in used_in_half]

    def cleanup_exhausted_plans(self) -> list[PlannedChip]:
        """Remove planned chips where the chip type is exhausted for that half.

        Returns the cleared entries.
        """
        cleared: list[PlannedChip] = []
        remaining: list[PlannedChip] = []
        for planned in self.chips:
            if ChipType(planned.chip) in self.get_available_chips(planned.gameweek):
                remaining.append(planned)
            else:
                cleared.append(planned)
        self.chips = remaining
        return cleared

    @classmethod
    def load(cls, path: Path | None = None) -> ChipPlan:
        """Load from file, or return empty plan. Handles corrupt files."""
        target = path or CHIP_PLAN_FILE
        if not target.exists():
            return cls()
        try:
            return cls.model_validate_json(target.read_bytes())
        except (json.JSONDecodeError, ValidationError):
            logger.debug("Resetting chip plan due to schema change or corruption")
            return cls()

    def save(self, path: Path | None = None) -> None:
        """Save chip plan to JSON file."""
        target = path or CHIP_PLAN_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        self.last_updated = datetime.now(tz=timezone.utc)
        target.write_text(self.model_dump_json(indent=2), encoding="utf-8")
