"""Canonical path constants for the fpl-cli project.

Every module that needs config/, data/, or templates/ should import
from here instead of computing its own Path(__file__) chain.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
