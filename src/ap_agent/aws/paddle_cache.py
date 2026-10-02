"""Deterministic PaddleOCR assets on a read-only Lambda image (M11E.1).

PaddleX keeps its model weights *and* downloaded fonts under one cache directory, and also writes lock files, temporary
files and function-result caches there. On Lambda only `/tmp` is writable, so the image bakes the models and fonts into
a read-only directory at build time (`AP_AGENT_PADDLEX_BAKED_DIR`, e.g. `/opt/paddlex`) and, before PaddleOCR is
imported, this module builds the *working* cache directory (`PADDLE_PDX_CACHE_HOME`, e.g. `/tmp/paddlex`) from symlinks to
the baked `official_models` and `fonts` directories. Nothing is downloaded at run time: weights and fonts are served from
the image and PaddleX's own scratch files go to `/tmp`.

A no-op unless `AP_AGENT_PADDLEX_BAKED_DIR` is set. Standard library only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, MutableMapping, Optional

__all__ = ["BAKED_DIR_ENVIRONMENT_VARIABLE", "PaddleCacheError", "prepare_paddlex_cache"]

BAKED_DIR_ENVIRONMENT_VARIABLE = "AP_AGENT_PADDLEX_BAKED_DIR"
_CACHE_ENVIRONMENT_VARIABLE = "PADDLE_PDX_CACHE_HOME"
_SHARED_DIRECTORIES = ("official_models", "fonts")


class PaddleCacheError(RuntimeError):
    """The baked model assets are missing or the working cache cannot be built."""


def prepare_paddlex_cache(environment: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """Returns the working cache directory when one was prepared, else `None`."""

    env: Mapping[str, str] = os.environ if environment is None else environment
    baked_text = env.get(BAKED_DIR_ENVIRONMENT_VARIABLE, "").strip()

    if not baked_text:
        return None

    working_text = env.get(_CACHE_ENVIRONMENT_VARIABLE, "").strip()
    baked = Path(baked_text)

    if not working_text or Path(working_text) == baked:
        raise PaddleCacheError(f"{_CACHE_ENVIRONMENT_VARIABLE} must name a writable directory different from the baked one.")

    for name in _SHARED_DIRECTORIES:
        if not (baked / name).is_dir():
            raise PaddleCacheError(f"The baked OCR assets are incomplete: {name} is missing.")

    working = Path(working_text)
    working.mkdir(parents=True, exist_ok=True)

    for name in _SHARED_DIRECTORIES:
        link = working / name

        if link.is_symlink() or link.exists():
            continue

        link.symlink_to(baked / name, target_is_directory=True)

    return working
