"""Filesystem helpers shared across services."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, file_mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically (tempfile + os.replace).

    Creates the parent directory first, so the first write into a fresh
    FPL_CLI_DATA_DIR cannot fail on a missing directory. A failure part-way
    never leaves a half-written file at ``path``, and the temp file is
    removed. ``file_mode``, when given, is applied after the replace
    (skipped on Windows, where chmod is a no-op for these bits).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            suffix=path.suffix or ".tmp",
            delete=False,
        ) as f:
            f.write(text)
            tmp_path = f.name
        os.replace(tmp_path, path)
        tmp_path = None
        if file_mode is not None and os.name != "nt":
            path.chmod(file_mode)
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)
