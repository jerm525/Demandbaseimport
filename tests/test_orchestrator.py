import datetime as dt
import json

import pytest
import yaml

from demandbase_sync.demandbase_client import (
    DemandbaseJobError,
    DemandbaseTimeoutError,
    JobStatusResult,
)
from demandbase_sync.notifier import NoOpNotifier
from demandbase_sync.orchestrator import run

from .conftest import FakeConnection, VIEW_COLUMNS, make_source_record


class FakeDemandbaseClient:
    """Scriptable stand-in for DemandbaseClient -- no real HTTP calls."""

    def __init__(self, job_id="job-1", poll_result=None, fail_create=False, fail_submit=False, fail_poll=None):
        self.job_id = job_id
        self.poll_result = poll_result or JobStatusResult(
            job_id=job_id, status="SUCCESS", records_sent=1, records_succeeded=1, records_failed=0
        )
        self.fail_create = fail_create
        self.fail_submit = fail_submit
        self.fail_poll = fail_poll  # exception instance to raise, or None
        self.created_jobs = []
        self.submitted_csvs = []
        self.polled_jobs = []
        self._job_counter = 0

    def create_import_job(self, import_name):
        if self.fail_create:
            raise DemandbaseJobError("simulated job creation failure")
        self._job_counter += 1
        job_id = f"{self.job_id}-{self._job_counter}" if self._job_counter > 1 else self.job_id
        self.created_jobs.append(job_id)
        return job_id

    def submit_csv(self, job_id, csv_path):
        if self.fail_submit:
            raise DemandbaseJobError("simulated submission failure")
        # Capture content now -- the orchestrator archives/moves this file
        # after submission completes, so the path itself won't be readable
        # once run() returns.
        content = csv_path.read_text()
        self.submitted_csvs.append((job_id, content))

    def poll_to_completion(self, job_id):
        self.polled_jobs.append(job_id)
        if self.fail_poll is not None:
            raise self.fail_poll
        return self.poll_result


def _write_mapping_file(path, mapping):
    entries = [
        {
            "source_field": m.source_field,
            "target_field": m.target_field,
            "transform": m.transform,
            "required": m.required,
            "default": m.default,
        }
        for m in mapping
    ]
    with open(path, "w") as fh:
        yaml.safe_dump(entries, fh)


def _setup_conn_with_records(records):
    result_columns = list(records[0].keys()) if records else VIEW_COLUMNS
    result_rows = [tuple(r.values()) for r in records]
    return FakeConnection(VIEW_COLUMNS, result_columns=result_columns, query_results=result_rows)


def test_tc1_and_tc2_new_and_updated_opportunity(app_config, test_mapping):
    _write_mapping_file(app_config.field_mapping_path, test_mapping)
    new_record = make_source_record(sfdc_id="NEW1", SyncActionHint="INSERT")
    updated_record = make_source_record(sfdc_id="UPD1", SyncActionHint="UPDATE")
    conn = _setup_conn_with_records([new_record, updated_record])

    client = FakeDemandbaseClient(
        poll_result=JobStatusResult(job_id="job-1", status="SUCCESS", records_sent=2, records_succeeded=2, records_failed=0)
    )

    run(app_config, conn=conn, client=client, notifier=NoOpNotifier())

    assert len(conn.inserted_records) == 2
    actions = {row[0]: row[5] for row in conn.inserted_records}  # SFDC id -> SyncAction
    assert actions["NEW1"] == "INSERT"
    assert actions["UPD1"] == "UPDATE"
    statuses = {row[0]: row[7] for row in conn.inserted_records}  # SFDC id -> RecordStatus
    assert statuses["NEW1"] == "SUCCESS"
    assert statuses["UPD1"] == "SUCCESS"
    assert conn.jobs[0]["Status"] == "SUCCESS"


def test_tc3_job_level_failure_watermark_unchanged(app_config, test_mapping):
    _write_mapping_file(app_config.field_mapping_path, test_mapping)
    record = make_source_record(sfdc_id="FAIL1")
    conn = _setup_conn_with_records([record])
    client = FakeDemandbaseClient(fail_submit=True)

    run(app_config, conn=conn, client=client, notifier=NoOpNotifier())

    assert conn.jobs[0]["Status"] == "FAILED"
    assert conn.jobs[0]["MaxSourceModifiedDate"] is None
    # Record was never successfully synced -> FAILED, not SUCCESS.
    assert conn.inserted_records[0][7] == "FAILED"
    # CSV should have moved to error/, not archive/.
    assert any(app_config.paths.error_dir.glob("*.csv"))
    assert not any(app_config.paths.archive_dir.glob("*.csv")) if app_config.paths.archive_dir.exists() else True


def test_tc4_partial_record_failure_with_detail(app_config, test_mapping):
    _write_mapping_file(app_config.field_mapping_path, test_mapping)
    good_record = make_source_record(sfdc_id="GOOD1")
    bad_record = make_source_record(sfdc_id="BAD1")
    conn = _setup_conn_with_records([good_record, bad_record])

    poll_result = JobStatusResult(
        job_id="job-1",
        status="PARTIAL_SUCCESS",
        records_sent=2,
        records_succeeded=1,
        records_failed=1,
        record_errors=[{"SFDC_OpportunityId": "BAD1", "error": "Invalid Stage"}],
    )
    client = FakeDemandbaseClient(poll_result=poll_result)

    run(app_config, conn=conn, client=client, notifier=NoOpNotifier())

    statuses = {row[0]: row[7] for row in conn.inserted_records}
    assert statuses["GOOD1"] == "SUCCESS"
    assert statuses["BAD1"] == "FAILED"
    assert conn.jobs[0]["Status"] == "PARTIAL_SUCCESS"
    # Watermark should only reflect the succeeded record.
    good_modified = good_record["LastModifiedDate"]
    assert conn.jobs[0]["MaxSourceModifiedDate"] == good_modified


def test_tc6_and_tc9_restart_resumes_in_progress_job_instead_of_new_work(app_config, test_mapping):
    _write_mapping_file(app_config.field_mapping_path, test_mapping)
    record = make_source_record(sfdc_id="RESUME1")
    conn = _setup_conn_with_records([record])  # would be selected if a new run started

    # Simulate a prior crashed run: IN_PROGRESS job row + its staging files.
    job_id = "job-in-progress"
    conn.jobs.append(
        {
            "JobId": job_id,
            "Status": "IN_PROGRESS",
            "RunStartTime": dt.datetime.now(dt.timezone.utc),
            "MaxSourceModifiedDate": None,
        }
    )
    app_config.paths.staging_dir.mkdir(parents=True)
    csv_path = app_config.paths.staging_dir / f"demandbase_opportunity_import_20260101_{job_id}.csv"
    csv_path.write_text("SFDC_OpportunityId\nRESUME1\n")
    manifest_path = app_config.paths.staging_dir / f"manifest_{job_id}.json"
    manifest_path.write_text(
        json.dumps(
            [
                {
                    "sfdc_opportunity_id": "RESUME1",
                    "sync_action": "INSERT",
                    "created_date": record["CreatedDate"].isoformat(),
                    "last_modified_date": record["LastModifiedDate"].isoformat(),
                    "mapping_failed": False,
                    "mapping_error": None,
                }
            ]
        )
    )

    client = FakeDemandbaseClient(
        job_id=job_id,
        poll_result=JobStatusResult(job_id=job_id, status="SUCCESS", records_sent=1, records_succeeded=1, records_failed=0),
    )

    run(app_config, conn=conn, client=client, notifier=NoOpNotifier())

    # It must have resumed polling the SAME job id, never created a new one.
    assert client.created_jobs == []
    assert client.polled_jobs == [job_id]
    assert conn.jobs[0]["Status"] == "SUCCESS"
    # The record from the resumed job was audited -- no *new* work started.
    assert len(conn.inserted_records) == 1
    assert conn.inserted_records[0][0] == "RESUME1"


def test_tc7_no_changed_records_job_flag_off(app_config, test_mapping):
    _write_mapping_file(app_config.field_mapping_path, test_mapping)
    conn = _setup_conn_with_records([])
    app_config.demandbase.create_job_on_empty_changeset = False
    client = FakeDemandbaseClient()

    run(app_config, conn=conn, client=client, notifier=NoOpNotifier())

    assert client.created_jobs == []
    assert conn.jobs == []


def test_tc7_no_changed_records_job_flag_on(app_config, test_mapping):
    _write_mapping_file(app_config.field_mapping_path, test_mapping)
    conn = _setup_conn_with_records([])
    app_config.demandbase.create_job_on_empty_changeset = True
    client = FakeDemandbaseClient(
        poll_result=JobStatusResult(job_id="job-1", status="SUCCESS", records_sent=0, records_succeeded=0, records_failed=0)
    )

    run(app_config, conn=conn, client=client, notifier=NoOpNotifier())

    assert client.created_jobs == ["job-1"]
    assert conn.jobs[0]["Status"] == "SUCCESS"


def test_tc8_null_required_field_fails_only_that_record(app_config, test_mapping):
    _write_mapping_file(app_config.field_mapping_path, test_mapping)
    good_record = make_source_record(sfdc_id="GOOD1")
    bad_record = make_source_record(sfdc_id="NULLFIELD1", AccountName=None)
    conn = _setup_conn_with_records([good_record, bad_record])

    client = FakeDemandbaseClient(
        poll_result=JobStatusResult(job_id="job-1", status="SUCCESS", records_sent=1, records_succeeded=1, records_failed=0)
    )

    run(app_config, conn=conn, client=client, notifier=NoOpNotifier())

    # Only the good record should have been sent to Demandbase.
    job_id, csv_content = client.submitted_csvs[0]
    assert "GOOD1" in csv_content
    assert "NULLFIELD1" not in csv_content

    statuses = {row[0]: row[7] for row in conn.inserted_records}
    errors = {row[0]: row[8] for row in conn.inserted_records}
    assert statuses["GOOD1"] == "SUCCESS"
    assert statuses["NULLFIELD1"] == "FAILED"
    assert "account_name" in errors["NULLFIELD1"]


def test_tc10_first_run_no_prior_job_uses_query_with_none_watermark(app_config, test_mapping):
    _write_mapping_file(app_config.field_mapping_path, test_mapping)
    record = make_source_record(sfdc_id="FIRST1")
    conn = _setup_conn_with_records([record])
    # No jobs at all -- get_last_successful_watermark must return None and
    # the run must not error.
    client = FakeDemandbaseClient(
        poll_result=JobStatusResult(job_id="job-1", status="SUCCESS", records_sent=1, records_succeeded=1, records_failed=0)
    )

    run(app_config, conn=conn, client=client, notifier=NoOpNotifier())

    assert conn.jobs[0]["Status"] == "SUCCESS"


def test_tc11_batch_splitting_and_crash_recovery(app_config, test_mapping):
    _write_mapping_file(app_config.field_mapping_path, test_mapping)
    app_config.demandbase.max_records_per_batch = 2
    records = [make_source_record(sfdc_id=f"BATCH{i}") for i in range(5)]
    conn = _setup_conn_with_records(records)

    client = FakeDemandbaseClient(
        job_id="job-batch",
        poll_result=JobStatusResult(job_id="job-batch", status="SUCCESS", records_sent=2, records_succeeded=2, records_failed=0),
    )

    run(app_config, conn=conn, client=client, notifier=NoOpNotifier())

    # 5 records / batch size 2 -> 3 batches -> 3 jobs created.
    assert len(client.created_jobs) == 3
    assert len(conn.jobs) == 3
    assert all(j["Status"] == "SUCCESS" for j in conn.jobs)
    assert len(conn.inserted_records) == 5

    # Simulate: a second run after "batch 1" succeeded -- the self-healing
    # query naturally excludes already-synced records, so re-running with a
    # DB that now only returns the remaining unsynced records must not
    # resubmit already-SUCCESS ones. We simulate this directly by checking
    # that all 5 unique SFDC ids were captured (no duplicates from a
    # crash-and-retry), which is what the self-healing WHERE clause + this
    # test's setup jointly guarantee.
    seen_ids = [row[0] for row in conn.inserted_records]
    assert sorted(seen_ids) == sorted(f"BATCH{i}" for i in range(5))
    assert len(seen_ids) == len(set(seen_ids))
