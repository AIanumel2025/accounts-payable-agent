"""Phase 1-5 configuration contracts.

Sources (active-definition table §2 `config/settings.py`):
  - `IngestionConfig`: notebook cell 10 (pydantic `BaseModel`, frozen).
  - `PreprocessingConfig`: notebook cell 26 ("PHASE 2 — CELL 1").
  - `OCRConfig`: notebook cell 34 ("PHASE 3 — CELL 1").
  - `NormalizationConfig`: notebook cell 51 ("PHASE 4 — CELL 1").
  - `FinancialValidationConfig`: notebook cell 68 ("PHASE 5 — CELL 1").

None of these classes have a default `artifact_root`: the notebook built
its `/content/...` paths as separate module globals and passed them in
explicitly when constructing each config instance. This module keeps that
shape (`artifact_root` is a required field) and defines no such global
path and no module-level config *instances* (the modularisation map
requires configuration to be threaded explicitly, never read as a hidden
global; §5.1, §6.1).

Deviation (documented, not a fix): all four dataclass configs ran, in the
notebook, with `from __future__ import annotations` active (every cell
from 26 onward inherited that IPython flag; R-12). This module does not
enable it, so that its `IngestionConfig` (a pydantic model, validated in a
cell that ran *without* the flag) keeps building the exact schema it built
in the notebook. This has no behavioural effect on the plain `dataclass`
configs, whose field types are never introspected at runtime.
"""

from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from dataclasses import dataclass

from ap_agent.models.normalization import InvoiceFieldName

__all__ = [
    "IngestionConfig",
    "PreprocessingConfig",
    "OCRConfig",
    "NormalizationConfig",
    "FinancialValidationConfig",
]


class IngestionConfig(BaseModel):
    """Configuration controlling document intake and storage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_root: Path
    maximum_file_size_bytes: int = Field(default=25 * 1024 * 1024, gt=0)


@dataclass(frozen=True)
class PreprocessingConfig:
    artifact_root: Path

    # PDF rendering resolution
    pdf_dpi: int = 300

    # Deskew controls
    enable_deskew: bool = True
    maximum_deskew_angle: float = 15.0

    # Image enhancement controls
    enable_clahe: bool = True
    clahe_clip_limit: float = 2.0
    clahe_tile_grid_size: tuple[int, int] = (8, 8)

    enable_denoising: bool = True
    denoising_strength: int = 7

    # Provisional quality thresholds
    minimum_width: int = 800
    minimum_height: int = 800
    minimum_blur_score: float = 45.0
    minimum_contrast_score: float = 20.0
    minimum_brightness: float = 35.0
    maximum_brightness: float = 235.0

    preprocessing_version: str = "phase2-v1"


@dataclass(frozen=True)
class OCRConfig:
    artifact_root: Path

    language: str = "eng"
    ocr_engine_name: str = "tesseract"
    ocr_version: str = "ocr-v1"

    # Tesseract configuration:
    # OEM 3 = automatic engine selection
    # PSM 6 = assume one structured block of text
    engine_mode: int = 3
    page_segmentation_mode: int = 6

    # Initial confidence thresholds.
    # These will be evaluated against the real invoices.
    minimum_token_confidence: float = 60.0
    minimum_page_mean_confidence: float = 70.0
    maximum_low_confidence_ratio: float = 0.35

    minimum_meaningful_tokens: int = 5
    minimum_meaningful_characters: int = 20

    preserve_raw_tesseract_output: bool = True

    @property
    def tesseract_config_string(self) -> str:
        return f"--oem {self.engine_mode} --psm {self.page_segmentation_mode}"


@dataclass(frozen=True)
class NormalizationConfig:
    artifact_root: Path

    normalization_version: str = "normalization-v1"

    minimum_field_confidence: float = 70.0
    ambiguity_score_margin: float = 10.0

    required_fields: tuple[InvoiceFieldName, ...] = (
        InvoiceFieldName.SUPPLIER_NAME,
        InvoiceFieldName.INVOICE_NUMBER,
        InvoiceFieldName.INVOICE_DATE,
        InvoiceFieldName.CURRENCY,
        InvoiceFieldName.TOTAL_AMOUNT,
    )

    monetary_quantization: Decimal = Decimal("0.01")

    maximum_line_items: int = 500

    preserve_field_candidates: bool = True
    preserve_raw_values: bool = True


@dataclass(frozen=True)
class FinancialValidationConfig:
    """Configuration for deterministic financial checks."""

    artifact_root: Path

    validation_version: str = "financial-validation-v1"

    monetary_tolerance: Decimal = Decimal("0.01")

    quantity_tolerance: Decimal = Decimal("0.001")

    percentage_tolerance: Decimal = Decimal("0.01")

    maximum_absolute_amount: Decimal = Decimal("1000000000.00")

    required_financial_fields: tuple[InvoiceFieldName, ...] = (
        InvoiceFieldName.CURRENCY,
        InvoiceFieldName.TOTAL_AMOUNT,
    )

    preserve_inherited_review: bool = True
    require_currency_consistency: bool = True
    require_date_consistency: bool = True

    fail_closed_on_missing_total: bool = True
    fail_closed_on_integrity_error: bool = True
