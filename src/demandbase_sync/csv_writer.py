"""CSV generation for a single batch's mapped Opportunity records."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import List

from .mapping import FieldMapping, target_field_order


def write_csv(mapped_records: List[dict], mapping: List[FieldMapping], path: Path) -> Path:
    """Write mapped records to `path` with headers in mapping-file order.

    `mapped_records` is the list of already-mapped (target_field -> value)
    dicts for records that succeeded mapping -- records that failed
    per-record mapping (RecordMappingError) must be excluded by the caller
    before this is called, since they don't get written to the CSV at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = target_field_order(mapping)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for record in mapped_records:
            writer.writerow(record)
    return path


def build_csv_filename(job_id: str, run_date) -> str:
    """demandbase_opportunity_import_YYYYMMDD_<JobId>.csv

    The JobId suffix is what resolves the same-day collision case from §10
    (rather than the "one run per day" alternative), since JobId is unique
    per Demandbase job and is only known after job creation -- which always
    happens before CSV generation in the orchestrator's sequencing.
    """
    date_str = run_date.strftime("%Y%m%d")
    safe_job_id = str(job_id).replace("/", "_").replace("\\", "_")
    return f"demandbase_opportunity_import_{date_str}_{safe_job_id}.csv"
