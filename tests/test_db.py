import datetime as dt

from demandbase_sync.db import (
    check_in_progress_job,
    fetch_changed_records,
    fetch_view_schema,
    get_last_successful_watermark,
)

from .conftest import VIEW_COLUMNS, FakeConnection, make_source_record


def test_fetch_view_schema_returns_columns():
    conn = FakeConnection(VIEW_COLUMNS)
    columns = fetch_view_schema(conn, "[dbo].[some_view]")
    assert columns == VIEW_COLUMNS


def test_check_in_progress_job_none_when_no_jobs():
    conn = FakeConnection(VIEW_COLUMNS)
    assert check_in_progress_job(conn, "[dbo].[tbl__demandbase_jobs]") is None


def test_check_in_progress_job_found():
    conn = FakeConnection(VIEW_COLUMNS)
    conn.jobs.append({"JobId": "job-1", "Status": "IN_PROGRESS"})
    result = check_in_progress_job(conn, "[dbo].[tbl__demandbase_jobs]")
    assert result["JobId"] == "job-1"


def test_get_last_successful_watermark_none_on_first_run():
    # TC10: no prior successful job -> None (caller falls back to config's
    # initial_watermark_utc for pre-filtering, if it chooses to).
    conn = FakeConnection(VIEW_COLUMNS)
    assert get_last_successful_watermark(conn, "[dbo].[tbl__demandbase_jobs]") is None


def test_get_last_successful_watermark_picks_max_of_successful_jobs():
    conn = FakeConnection(VIEW_COLUMNS)
    conn.jobs.append({"Status": "SUCCESS", "MaxSourceModifiedDate": dt.datetime(2026, 1, 1)})
    conn.jobs.append({"Status": "SUCCESS", "MaxSourceModifiedDate": dt.datetime(2026, 2, 1)})
    conn.jobs.append({"Status": "FAILED", "MaxSourceModifiedDate": dt.datetime(2026, 3, 1)})
    watermark = get_last_successful_watermark(conn, "[dbo].[tbl__demandbase_jobs]")
    assert watermark == dt.datetime(2026, 2, 1)


def test_fetch_changed_records_returns_rows():
    record = make_source_record()
    result_columns = list(record.keys())
    result_row = tuple(record.values())
    conn = FakeConnection(VIEW_COLUMNS, result_columns=result_columns, query_results=[result_row])

    rows = fetch_changed_records(conn, "[dbo].[view]", "[dbo].[records]", None)
    assert len(rows) == 1
    assert rows[0]["SFDC_ID"] == record["SFDC_ID"]
