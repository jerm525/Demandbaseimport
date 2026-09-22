"""
Writes to the two audit tables. Schemas are fixed and must not be altered
(see build prompt §3) -- this module only inserts/updates rows.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Optional

from .db import DBConnection


@dataclass
class RecordOutcome:
    sfdc_opportunity_id: str
    sync_action: str  # INSERT | UPDATE
    record_status: str  # SUCCESS | FAILED
    created_date: dt.datetime
    last_modified_date: dt.datetime
    error_message: Optional[str] = None


def insert_job_started(
    conn: DBConnection,
    jobs_table_fqname: str,
    job_id: str,
    entity_type: str,
    run_start_time: dt.datetime,
) -> None:
    """Immediately after job creation succeeds, and before generating or
    submitting the CSV -- this ordering is required for restartability."""
    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT INTO {jobs_table_fqname}
            (JobId, EntityType, Status, RunStartTime, CreatedTimestamp)
        VALUES (?, ?, 'IN_PROGRESS', ?, ?)
        """,
        (job_id, entity_type, run_start_time, dt.datetime.utcnow()),
    )
    conn.commit()
    cur.close()


def finalize_job(
    conn: DBConnection,
    jobs_table_fqname: str,
    job_id: str,
    status: str,
    run_end_time: dt.datetime,
    records_sent: int,
    records_succeeded: int,
    records_failed: int,
    max_source_modified_date: Optional[dt.datetime],
) -> None:
    """Update the job row's Status/RunEndTime/counts.

    `max_source_modified_date` must only reflect records that actually
    succeeded THIS run (see §9) -- callers compute that before calling this.
    A FAILED job must be called with max_source_modified_date=None so the
    watermark pointer is never advanced.
    """
    cur = conn.cursor()
    cur.execute(
        f"""
        UPDATE {jobs_table_fqname}
        SET Status = ?, RunEndTime = ?, RecordsSent = ?, RecordsSucceeded = ?,
            RecordsFailed = ?, MaxSourceModifiedDate = ?
        WHERE JobId = ?
        """,
        (
            status,
            run_end_time,
            records_sent,
            records_succeeded,
            records_failed,
            max_source_modified_date,
            job_id,
        ),
    )
    conn.commit()
    cur.close()


def insert_record_rows(
    conn: DBConnection,
    records_table_fqname: str,
    job_id: str,
    outcomes: List[RecordOutcome],
) -> None:
    if not outcomes:
        return
    cur = conn.cursor()
    sync_timestamp = dt.datetime.utcnow()
    rows = [
        (
            o.sfdc_opportunity_id,
            job_id,
            sync_timestamp,
            o.created_date,
            o.last_modified_date,
            o.sync_action,
            "Salesforce",  # SourceSystem: SQL view sourced ultimately from SFDC
            o.record_status,
            o.error_message,
        )
        for o in outcomes
    ]
    cur.executemany(
        f"""
        INSERT INTO {records_table_fqname}
            (SFDC_OpportunityId, JobId, SyncTimestamp, SourceCreatedDate, SourceModifiedDate,
             SyncAction, SourceSystem, RecordStatus, ErrorMessage)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    cur.close()


def compute_max_succeeded_modified_date(
    outcomes: List[RecordOutcome],
) -> Optional[dt.datetime]:
    succeeded = [o.last_modified_date for o in outcomes if o.record_status == "SUCCESS"]
    if not succeeded:
        return None
    return max(succeeded)
