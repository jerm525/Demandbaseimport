"""
Run orchestrator. Implements the sequencing from §8 of the build prompt,
including the §8.5 batching exception for oversized change sets.

Per-batch manifest files
-------------------------
Demandbase's target field set doesn't include LastModifiedDate/CreatedDate
or a raw SyncAction flag (those are source-side/audit-side concepts, not
Demandbase fields), so once a record is mapped into the outbound CSV that
information is gone from the CSV itself. To make crash-mid-poll restart
(TC6) actually able to reconstruct audit rows -- rather than just resuming
the HTTP poll and then having nowhere to write results -- each batch also
writes a small manifest JSON file (staging/manifest_<job_id>.json) alongside
its CSV, carrying exactly the fields audit.py needs per record. This is an
implementation detail, not a schema change: nothing outside this module
reads or writes it.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import audit, db
from .archival import archive_failure, archive_success, purge_old_files
from .config import AppConfig
from .csv_writer import build_csv_filename, write_csv
from .demandbase_client import (
    DemandbaseClient,
    DemandbaseJobError,
    DemandbaseTimeoutError,
    JobStatusResult,
)
from .logging_setup import JobIdAdapter, configure_run_logger
from .mapping import (
    FieldMapping,
    RecordMappingError,
    apply_mapping,
    load_mapping_file,
    validate_mapping,
)
from .notifier import Notifier, build_notifier

logger = logging.getLogger(__name__)


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


@dataclass
class ManifestEntry:
    sfdc_opportunity_id: str
    sync_action: str
    created_date: Optional[str]
    last_modified_date: Optional[str]
    mapping_failed: bool
    mapping_error: Optional[str]


def _write_manifest(path: Path, entries: List[ManifestEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([asdict(e) for e in entries], fh)


def _load_manifest(path: Path) -> List[ManifestEntry]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return [ManifestEntry(**entry) for entry in raw]


def _find_batch_files(staging_dir: Path, job_id: str) -> tuple[Optional[Path], Optional[Path]]:
    """Locate this job's manifest + CSV in staging/ by JobId suffix."""
    manifest_path = staging_dir / f"manifest_{job_id}.json"
    csv_matches = list(staging_dir.glob(f"*_{job_id}.csv"))
    csv_path = csv_matches[0] if csv_matches else None
    return (manifest_path if manifest_path.exists() else None, csv_path)


def _determine_sync_action(record: dict) -> str:
    """INSERT if the record has never had a successful sync, UPDATE otherwise.

    Relies on `SyncActionHint`, a derived (non-schema-changing) column added
    to the §4 extraction query -- see db.py -- that flags which branch of the
    LEFT JOIN matched, since that information isn't otherwise recoverable
    from `src.*` alone.
    """
    return record.get("SyncActionHint") or "INSERT"


def _split_into_batches(records: List[dict], batch_size: int) -> List[List[dict]]:
    if batch_size <= 0:
        return [records] if records else []
    return [records[i : i + batch_size] for i in range(0, len(records), batch_size)]


def _build_manifest_entries(
    records: List[dict],
    mapping: List[FieldMapping],
) -> tuple[List[dict], List[ManifestEntry]]:
    """Apply field mapping to every record in a batch.

    Returns (mapped_records_for_csv, manifest_entries_for_all_records).
    Records that fail mapping (required field null) are excluded from the
    CSV but still get a manifest entry marked mapping_failed=True, so audit
    can write their FAILED row without ever having sent them to Demandbase.
    """
    mapped_for_csv: List[dict] = []
    manifest_entries: List[ManifestEntry] = []

    for record in records:
        sfdc_id = record.get("SFDC_OpportunityId")
        sync_action = _determine_sync_action(record)
        created_date = record.get("CreatedDate")
        last_modified_date = record.get("LastModifiedDate")

        try:
            mapped = apply_mapping(record, mapping)
        except RecordMappingError as exc:
            manifest_entries.append(
                ManifestEntry(
                    sfdc_opportunity_id=sfdc_id,
                    sync_action=sync_action,
                    created_date=_iso(created_date),
                    last_modified_date=_iso(last_modified_date),
                    mapping_failed=True,
                    mapping_error=str(exc),
                )
            )
            continue

        mapped_for_csv.append(mapped)
        manifest_entries.append(
            ManifestEntry(
                sfdc_opportunity_id=sfdc_id,
                sync_action=sync_action,
                created_date=_iso(created_date),
                last_modified_date=_iso(last_modified_date),
                mapping_failed=False,
                mapping_error=None,
            )
        )

    return mapped_for_csv, manifest_entries


def _outcomes_from_job_result(
    manifest_entries: List[ManifestEntry],
    job_result: Optional[JobStatusResult],
    job_level_failure: bool,
) -> List[audit.RecordOutcome]:
    """Reconcile manifest entries with the Demandbase job result into final
    per-record audit.RecordOutcome rows.

    Rules:
      - Records that failed mapping never got sent; always FAILED.
      - If the job itself failed to complete (job_level_failure=True, or no
        job_result), every sent record is FAILED -- a failed job can't be
        assumed to have synced anything.
      - If the job succeeded/partial-succeeded and the poller returned
        per-record detail (record_errors), use it to mark individual sent
        records FAILED/SUCCESS.
      - *** UNCONFIRMED BEHAVIOR (see README) ***: if the job is
        PARTIAL_SUCCESS but the poller did NOT return per-record detail
        (the real endpoint's shape isn't confirmed yet), we cannot know
        which specific records failed. To avoid ever incorrectly advancing
        the watermark for a record that actually failed, every sent record
        in that batch is conservatively marked FAILED so they're retried by
        the next run's self-healing query. This trades some duplicate
        re-sends for correctness and must be revisited once Demandbase
        confirms per-record polling detail.
    """
    outcomes: List[audit.RecordOutcome] = []

    failed_ids_from_detail = None
    if job_result is not None and job_result.record_errors:
        failed_ids_from_detail = {
            err.get("SFDC_OpportunityId") or err.get("sfdc_opportunity_id")
            for err in job_result.record_errors
        }

    for entry in manifest_entries:
        created = _parse_iso(entry.created_date)
        modified = _parse_iso(entry.last_modified_date)

        if entry.mapping_failed:
            outcomes.append(
                audit.RecordOutcome(
                    sfdc_opportunity_id=entry.sfdc_opportunity_id,
                    sync_action=entry.sync_action,
                    record_status="FAILED",
                    created_date=created,
                    last_modified_date=modified,
                    error_message=entry.mapping_error,
                )
            )
            continue

        if job_level_failure or job_result is None:
            outcomes.append(
                audit.RecordOutcome(
                    sfdc_opportunity_id=entry.sfdc_opportunity_id,
                    sync_action=entry.sync_action,
                    record_status="FAILED",
                    created_date=created,
                    last_modified_date=modified,
                    error_message="Demandbase job did not complete successfully.",
                )
            )
            continue

        if failed_ids_from_detail is not None:
            if entry.sfdc_opportunity_id in failed_ids_from_detail:
                outcomes.append(
                    audit.RecordOutcome(
                        sfdc_opportunity_id=entry.sfdc_opportunity_id,
                        sync_action=entry.sync_action,
                        record_status="FAILED",
                        created_date=created,
                        last_modified_date=modified,
                        error_message="Reported as failed by Demandbase job result.",
                    )
                )
            else:
                outcomes.append(
                    audit.RecordOutcome(
                        sfdc_opportunity_id=entry.sfdc_opportunity_id,
                        sync_action=entry.sync_action,
                        record_status="SUCCESS",
                        created_date=created,
                        last_modified_date=modified,
                    )
                )
            continue

        if job_result.status == "completed":
            outcomes.append(
                audit.RecordOutcome(
                    sfdc_opportunity_id=entry.sfdc_opportunity_id,
                    sync_action=entry.sync_action,
                    record_status="SUCCESS",
                    created_date=created,
                    last_modified_date=modified,
                )
            )
        else:
            # PARTIAL_SUCCESS with no per-record detail -- see docstring.
            outcomes.append(
                audit.RecordOutcome(
                    sfdc_opportunity_id=entry.sfdc_opportunity_id,
                    sync_action=entry.sync_action,
                    record_status="FAILED",
                    created_date=created,
                    last_modified_date=modified,
                    error_message=(
                        "Job reported PARTIAL_SUCCESS but no per-record detail was "
                        "available; conservatively marked FAILED for retry."
                    ),
                )
            )

    return outcomes


def _finalize_and_audit(
    conn,
    config: AppConfig,
    job_id: str,
    manifest_entries: List[ManifestEntry],
    job_result: Optional[JobStatusResult],
    job_level_failure: bool,
    run_end_time: datetime,
    log: JobIdAdapter,
) -> str:
    outcomes = _outcomes_from_job_result(manifest_entries, job_result, job_level_failure)
    audit.insert_record_rows(conn, config.database.records_table_fqname, job_id, outcomes)

    succeeded = sum(1 for o in outcomes if o.record_status == "SUCCESS")
    failed = sum(1 for o in outcomes if o.record_status == "FAILED")
    sent = len([e for e in manifest_entries if not e.mapping_failed])

    if job_level_failure or job_result is None:
        status = "FAILED"
        watermark = None
    elif failed == 0:
        status = "SUCCESS"
        watermark = audit.compute_max_succeeded_modified_date(outcomes)
    else:
        status = "PARTIAL_SUCCESS"
        watermark = audit.compute_max_succeeded_modified_date(outcomes)

    audit.finalize_job(
        conn,
        config.database.jobs_table_fqname,
        job_id,
        status,
        run_end_time,
        records_sent=sent,
        records_succeeded=succeeded,
        records_failed=failed,
        max_source_modified_date=watermark,
    )
    log.info(
        f"Batch job {job_id} finalized: status={status} sent={sent} "
        f"succeeded={succeeded} failed={failed}"
    )
    return status


def _process_new_batch(
    conn,
    config: AppConfig,
    client: DemandbaseClient,
    mapping: List[FieldMapping],
    batch_records: List[dict],
    run_id: str,
    batch_index: int,
    log: JobIdAdapter,
    notifier: Notifier,
) -> str:
    """Full per-batch cycle for a brand-new batch (§8 step 4)."""
    run_start_time = datetime.now(timezone.utc)
    import_name = f"opportunity_sync_{run_id}_batch{batch_index}"

    try:
        job_id = client.create_import_job(import_name)
    except DemandbaseJobError as exc:
        log.error(f"Batch {batch_index}: job creation failed, no job row created: {exc}")
        notifier.alert(
            "Demandbase sync: job creation failed",
            f"Batch {batch_index} of run {run_id} failed to create a Demandbase job: {exc}",
        )
        raise

    log.set_job_id(job_id)
    log.info(f"Batch {batch_index}: created Demandbase job {job_id} with {len(batch_records)} records")

    # Required ordering: insert IN_PROGRESS row immediately after job
    # creation succeeds, before generating/submitting the CSV.
    audit.insert_job_started(
        conn, config.database.jobs_table_fqname, job_id, config.demandbase.entity_type, run_start_time
    )

    mapped_for_csv, manifest_entries = _build_manifest_entries(batch_records, mapping)

    staging_dir = config.paths.staging_dir
    filename = build_csv_filename(job_id, run_start_time)
    csv_path = staging_dir / filename
    manifest_path = staging_dir / f"manifest_{job_id}.json"
    write_csv(mapped_for_csv, mapping, csv_path)
    _write_manifest(manifest_path, manifest_entries)

    job_level_failure = False
    job_result: Optional[JobStatusResult] = None
    try:
        client.submit_csv(job_id, csv_path)
        job_result = client.poll_to_completion(job_id)
    except DemandbaseTimeoutError as exc:
        log.error(f"Batch {batch_index}: polling timed out for job {job_id}: {exc}")
        job_level_failure = True
        notifier.alert("Demandbase sync: job timed out", str(exc))
    except DemandbaseJobError as exc:
        log.error(f"Batch {batch_index}: job {job_id} failed: {exc}")
        job_level_failure = True
        notifier.alert("Demandbase sync: job failed", str(exc))

    run_end_time = datetime.now(timezone.utc)
    status = _finalize_and_audit(
        conn, config, job_id, manifest_entries, job_result, job_level_failure, run_end_time, log
    )

    if status == "FAILED":
        archive_failure(csv_path, config.paths.error_dir)
        notifier.alert(
            "Demandbase sync: batch FAILED",
            f"Batch {batch_index} of run {run_id} (job {job_id}) FAILED.",
        )
    else:
        archive_success(csv_path, config.paths.archive_dir)
        if status == "PARTIAL_SUCCESS":
            notifier.alert(
                "Demandbase sync: batch PARTIAL_SUCCESS",
                f"Batch {batch_index} of run {run_id} (job {job_id}) had failed records.",
            )
    manifest_path.unlink(missing_ok=True)
    log.set_job_id(None)
    return status


def _resume_in_progress_job(
    conn,
    config: AppConfig,
    client: DemandbaseClient,
    in_progress_job: dict,
    log: JobIdAdapter,
    notifier: Notifier,
) -> None:
    """§8 step 1 / TC6: resume polling an IN_PROGRESS job instead of
    starting a new run."""
    job_id = str(in_progress_job["JobId"])
    log.set_job_id(job_id)
    log.info(f"Found IN_PROGRESS job {job_id} from a prior run; resuming instead of starting new work.")

    manifest_path, csv_path = _find_batch_files(config.paths.staging_dir, job_id)
    if manifest_path is None or csv_path is None:
        log.error(
            f"IN_PROGRESS job {job_id} found but its staging manifest/CSV could not be "
            "located; cannot safely reconstruct audit rows. Marking job FAILED and alerting "
            "rather than guessing."
        )
        job_level_failure = True
        manifest_entries: List[ManifestEntry] = []
        job_result = None
        if csv_path is not None:
            archive_failure(csv_path, config.paths.error_dir)
        audit.finalize_job(
            conn,
            config.database.jobs_table_fqname,
            job_id,
            "FAILED",
            datetime.now(timezone.utc),
            records_sent=0,
            records_succeeded=0,
            records_failed=0,
            max_source_modified_date=None,
        )
        notifier.alert(
            "Demandbase sync: unresumable IN_PROGRESS job",
            f"Job {job_id} was IN_PROGRESS but its staging files were missing on restart.",
        )
        return

    manifest_entries = _load_manifest(manifest_path)

    job_level_failure = False
    job_result: Optional[JobStatusResult] = None
    try:
        job_result = client.poll_to_completion(job_id)
    except DemandbaseTimeoutError as exc:
        log.error(f"Resumed job {job_id} timed out: {exc}")
        job_level_failure = True
        notifier.alert("Demandbase sync: resumed job timed out", str(exc))
    except DemandbaseJobError as exc:
        log.error(f"Resumed job {job_id} failed: {exc}")
        job_level_failure = True
        notifier.alert("Demandbase sync: resumed job failed", str(exc))

    run_end_time = datetime.now(timezone.utc)
    status = _finalize_and_audit(
        conn, config, job_id, manifest_entries, job_result, job_level_failure, run_end_time, log
    )

    if status == "FAILED":
        archive_failure(csv_path, config.paths.error_dir)
    else:
        archive_success(csv_path, config.paths.archive_dir)
    manifest_path.unlink(missing_ok=True)
    log.set_job_id(None)


def run(
    config: AppConfig,
    conn=None,
    client: Optional[DemandbaseClient] = None,
    notifier: Optional[Notifier] = None,
) -> str:
    """Top-level entry point implementing §8's sequencing.

    `conn` and `client` are injectable for tests; production code (run.py)
    leaves them None so real SQL Server / Demandbase connections are used.
    Returns the run_id.
    """
    run_id = uuid.uuid4().hex[:12]
    log = configure_run_logger(config.paths, run_id)
    notifier = notifier or build_notifier(config)

    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection(config)
    if client is None:
        client = DemandbaseClient(config)

    try:
        log.info(f"Run {run_id} starting (environment={config.environment.value})")

        # §8 step 1: an IN_PROGRESS job blocks any new work this run (TC9).
        in_progress = db.check_in_progress_job(conn, config.database.jobs_table_fqname)
        if in_progress is not None:
            _resume_in_progress_job(conn, config, client, in_progress, log, notifier)
            log.info(f"Run {run_id} complete (resume-only run).")
            _run_retention_cleanup(config, log)
            return run_id

        # Mapping: load + validate against the live view schema at startup.
        mapping = load_mapping_file(config.field_mapping_path)
        view_columns = db.fetch_view_schema(conn, config.database.view_fqname)
        validate_mapping(mapping, view_columns)

        # §4: watermark pre-filter (optional performance optimization only).
        watermark = db.get_last_successful_watermark(conn, config.database.jobs_table_fqname)
        changed_records = db.fetch_changed_records(
            conn,
            config.database.view_fqname,
            config.database.records_table_fqname,
            watermark,
        )
        log.info(f"Run {run_id}: {len(changed_records)} changed record(s) selected.")

        if not changed_records:
            if config.demandbase.create_job_on_empty_changeset:
                _process_new_batch(conn, config, client, mapping, [], run_id, 0, log, notifier)
            else:
                log.info("No changed records and create_job_on_empty_changeset is False; skipping job creation.")
            _run_retention_cleanup(config, log)
            return run_id

        # §8.5: split into batches when the change set exceeds the
        # (unconfirmed placeholder) max_records_per_batch limit.
        batches = _split_into_batches(changed_records, config.demandbase.max_records_per_batch)
        if len(batches) > 1:
            log.info(
                f"Run {run_id}: change set of {len(changed_records)} exceeds "
                f"max_records_per_batch={config.demandbase.max_records_per_batch}; "
                f"splitting into {len(batches)} sequential batches."
            )

        statuses = []
        for idx, batch in enumerate(batches):
            status = _process_new_batch(conn, config, client, mapping, batch, run_id, idx, log, notifier)
            statuses.append(status)
            if idx < len(batches) - 1 and config.demandbase.inter_batch_delay_seconds > 0:
                time.sleep(config.demandbase.inter_batch_delay_seconds)

        log.info(f"Run {run_id} complete. Batch statuses: {statuses}")
        _run_retention_cleanup(config, log)
        return run_id
    finally:
        if owns_conn:
            conn.close()


def _run_retention_cleanup(config: AppConfig, log: JobIdAdapter) -> None:
    """§10: purge files older than 90 days from archive/ and error/, once
    per run, after all batches."""
    deleted = purge_old_files(
        [config.paths.archive_dir, config.paths.error_dir], config.paths.retention_days
    )
    log.info(f"Retention cleanup: deleted {len(deleted)} file(s) older than {config.paths.retention_days} days.")
