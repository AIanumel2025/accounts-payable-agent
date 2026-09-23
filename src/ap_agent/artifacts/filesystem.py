"""Generic filesystem artifact writers shared by Phase 2 and Phase 3.

Source: notebook cell 26 ("PHASE 2 — CELL 1"), active-definition table §2
`artifacts/filesystem.py`. `calculate_file_sha256` and `write_json_atomically`
are the **Phase 2/3 binding** (§3.2 of the modularisation map): they are
globally shadowed later in the notebook (cell 63's `write_json_atomically`,
cell 73's `calculate_file_sha256`), but Phase 2 and Phase 3 were validated
against these cell-26 bytes, and the two `write_json_atomically`
implementations write different bytes (D-8). This module keeps only the
cell-26 behaviour; the cell-63 byte format belongs to the Phase 4
normalisation milestone under a distinct name, per D-8.

No heavy or optional dependency (`cv2`, `numpy`) is imported at module
level: both are imported lazily inside `save_png_atomically`, the only
function that needs them (CLAUDE.md).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

__all__ = [
    "calculate_file_sha256",
    "write_json_atomically",
    "save_png_atomically",
]


def calculate_file_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()

    with file_path.open("rb") as file_handle:
        for block in iter(
            lambda: file_handle.read(1024 * 1024),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def write_json_atomically(
    destination: Path,
    payload: dict,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = destination.with_suffix(
        destination.suffix + ".tmp"
    )

    temporary_path.write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )

    temporary_path.replace(destination)


def save_png_atomically(
    image: "np.ndarray",
    destination: Path,
) -> None:
    import cv2

    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = destination.with_name(
        destination.stem + ".tmp" + destination.suffix
    )

    success = cv2.imwrite(str(temporary_path), image)

    if not success:
        raise IOError(f"Could not write image: {temporary_path}")

    temporary_path.replace(destination)
