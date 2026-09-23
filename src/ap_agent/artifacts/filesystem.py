"""Generic filesystem artifact writers shared by Phase 2, Phase 3 and Phase 4.

Source: notebook cell 26 ("PHASE 2 — CELL 1") for `calculate_file_sha256`,
`write_json_atomically` and `save_png_atomically`; cell 38 ("PHASE 3 —
CELL 3") for `write_text_atomically`; cell 41 ("PHASE 3 — CORRECTION CELL
4B") for `save_pil_image_atomically`; cell 63 ("PHASE 4 — CELL 5") for
`write_json_safe_atomically` (active-definition table §2
`artifacts/filesystem.py`).

`calculate_file_sha256` and `write_json_atomically` are the **Phase 2/3
binding** (§3.2 of the modularisation map): they are globally shadowed
later in the notebook (cell 63's `write_json_atomically`, cell 73's
`calculate_file_sha256`), but Phase 2 and Phase 3 were validated against
these cell-26 bytes, and the two `write_json_atomically` implementations
write different bytes (D-8). This module keeps only the cell-26 behaviour
under its original name; the cell-63 byte format is `write_json_safe_atomically`
below (M5, per D-8's proposed distinct name): it JSON-safe-converts the
payload first (`convert_to_json_safe`, recursing through dataclasses,
`Enum`, `Decimal`, dates) and writes with `ensure_ascii=False`, where the
Phase 2/3 writer instead relies on `json.dumps(..., default=str)` and
ASCII-escapes non-ASCII characters. Re-running Phase 2 or 3 code after
Phase 4 in the notebook would already have changed the Phase 2/3 artifact
bytes had the names not been kept separate.

No heavy or optional dependency (`cv2`, `numpy`, `PIL`) is imported at
module level: each is imported lazily inside the function that needs it
(CLAUDE.md).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ap_agent.artifacts.serialization import convert_to_json_safe

if TYPE_CHECKING:
    import numpy as np
    from PIL import Image

__all__ = [
    "calculate_file_sha256",
    "write_json_atomically",
    "save_png_atomically",
    "write_text_atomically",
    "save_pil_image_atomically",
    "write_json_safe_atomically",
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


def write_text_atomically(
    destination: Path,
    text: str,
) -> None:
    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = destination.with_suffix(
        destination.suffix + ".tmp"
    )

    temporary_path.write_text(
        text,
        encoding="utf-8",
    )

    temporary_path.replace(destination)


def save_pil_image_atomically(
    image: "Image.Image",
    destination: Path,
) -> None:
    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = destination.with_name(
        destination.stem
        + ".tmp"
        + destination.suffix
    )

    image.convert("RGB").save(
        temporary_path,
        format="PNG",
    )

    temporary_path.replace(destination)


def write_json_safe_atomically(
    destination: Path,
    payload: Any,
) -> None:
    """Phase 4 JSON writer (notebook cell 63's `write_json_atomically`).

    Distinct byte format from `write_json_atomically` above (D-8): the
    payload is converted with `convert_to_json_safe` first (so dataclasses,
    `Enum`, `Decimal`, `UUID` and dates serialise without a `default=`
    fallback) and written with `ensure_ascii=False`.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = destination.with_suffix(
        destination.suffix + ".tmp"
    )

    temporary_path.write_text(
        json.dumps(
            convert_to_json_safe(payload),
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    temporary_path.replace(destination)
