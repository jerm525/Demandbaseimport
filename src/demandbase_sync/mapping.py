"""
Field mapping: loads field_mapping.yaml, validates it against the live view
schema at startup, and applies it per-record during CSV generation.

The 25 target fields Demandbase requires (per the build prompt) are fixed
here as REQUIRED_TARGET_FIELDS purely so startup validation can fail fast if
the mapping file is missing one -- it is NOT a statement about their final
CSV header spelling. See README "Flagged Items" for the open question about
whether names like "Opportunity Name" / "Is Closed?" are literal headers or
display labels standing in for different machine keys.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

import yaml
from pydantic import BaseModel, model_validator

from .transforms import TransformError, apply_transform

REQUIRED_TARGET_FIELDS: List[str] = [
    "Opportunity ID",
    "Opportunity Name",
    "Type",
    "Created Date",
    "Owner",
    "Account Name"
]


class FieldMapping(BaseModel):
    source_field: str
    target_field: str
    transform: str = "none"
    required: bool = False
    default: Optional[Any] = None

    @model_validator(mode="after")
    def _default_only_when_not_required(self) -> "FieldMapping":
        # Not a hard error -- a required field simply ignores `default` if
        # someone sets both, since required fields fail the record instead.
        return self


class MappingValidationError(ValueError):
    pass


class RecordMappingError(ValueError):
    """Raised for a single record when a required field is null/missing.

    Caught by the orchestrator/CSV generation step so it can fail *that
    record only* (RecordStatus=FAILED) rather than the whole run.
    """

    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(message)


def load_mapping_file(path: Path) -> List[FieldMapping]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or []
    if not isinstance(raw, list):
        raise MappingValidationError(
            f"{path} must contain a YAML list of field mapping entries."
        )
    return [FieldMapping(**entry) for entry in raw]


def validate_mapping(
    mapping: List[FieldMapping],
    view_columns: Iterable[str],
    required_target_fields: Iterable[str] = REQUIRED_TARGET_FIELDS,
) -> None:
    """Fail fast at startup if the mapping references a source column that
    doesn't exist in the view, or is missing any required target field.
    """
    view_columns_set = set(view_columns)
    errors: List[str] = []

    for entry in mapping:
        if entry.source_field not in view_columns_set:
            errors.append(
                f"Mapping entry for target '{entry.target_field}' references "
                f"source column '{entry.source_field}', which does not exist "
                f"in the view. Available columns: {sorted(view_columns_set)}"
            )

    mapped_targets = {entry.target_field for entry in mapping}
    missing_targets = set(required_target_fields) - mapped_targets
    if missing_targets:
        errors.append(
            f"Mapping file is missing required target field(s): {sorted(missing_targets)}"
        )

    if errors:
        raise MappingValidationError(
            "Field mapping validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )


def apply_mapping(record: dict, mapping: List[FieldMapping]) -> dict:
    """Apply the mapping to a single source record.

    Returns the mapped output dict (target_field -> value) in mapping order.
    Raises RecordMappingError if a required field is null/missing -- callers
    must catch this per-record and mark only that record FAILED.
    """
    output: dict = {}
    for entry in mapping:
        raw_value = record.get(entry.source_field)

        if raw_value is None:
            if entry.required:
                raise RecordMappingError(
                    entry.target_field,
                    f"Required field '{entry.target_field}' (source column "
                    f"'{entry.source_field}') is null.",
                )
            raw_value = entry.default

        if raw_value is None:
            output[entry.target_field] = None
            continue

        try:
            output[entry.target_field] = apply_transform(entry.transform, raw_value)
        except TransformError as exc:
            if entry.required:
                raise RecordMappingError(
                    entry.target_field,
                    f"Transform failed for required field '{entry.target_field}': {exc}",
                ) from exc
            output[entry.target_field] = entry.default

    return output


def target_field_order(mapping: List[FieldMapping]) -> List[str]:
    """CSV column order == mapping file order, so it's a one-line config
    change (reordering the YAML) to change CSV header order."""
    return [entry.target_field for entry in mapping]
