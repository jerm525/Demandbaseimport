"""
SQL Server access layer.

Everything here talks only to SQL -- never to the Salesforce API (the source
view is populated by an external ETL this application does not touch).

All timestamps are handled as UTC per the build prompt.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, List, Optional, Protocol

from .config import AppConfig, get_sql_connection_string


class DBConnection(Protocol):
    """Minimal protocol so tests can inject a fake connection/cursor without
    needing a real pyodbc/SQL Server instance."""

    def cursor(self) -> Any: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


def get_connection(config: AppConfig) -> DBConnection:
    """Create a live pyodbc connection. Imported lazily so unit tests that
    inject a fake connection never need pyodbc installed/available."""
    import pyodbc  # local import: keeps pyodbc optional for pure unit tests

    conn_str = get_sql_connection_string(config)
    return pyodbc.connect(
        conn_str, timeout=config.database.connect_timeout_seconds, autocommit=False
    )


def fetch_view_schema(conn: DBConnection, view_fqname: str) -> List[str]:
    """SELECT TOP 0 * FROM <view> to discover real column names.

    Never invent column names -- this is the only source of truth for what
    `source_field` values are valid in field_mapping.yaml.
    """
    cur = conn.cursor()
    cur.execute(f"SELECT TOP 0 * FROM {view_fqname}")
    columns = [desc[0] for desc in cur.description]
    cur.close()
    return columns


def _rows_to_dicts(cursor: Any) -> List[dict]:
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def check_in_progress_job(conn: DBConnection, jobs_table_fqname: str) -> Optional[dict]:
    """Return the IN_PROGRESS job row, if any (for restart/resume, TC6/TC9)."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT * FROM {jobs_table_fqname} WHERE Status = 'IN_PROGRESS'"
    )
    rows = _rows_to_dicts(cur)
    cur.close()
    if not rows:
        return None
    if len(rows) > 1:
        # Shouldn't happen given the orchestrator's sequencing, but surface
        # it loudly rather than silently picking one.
        raise RuntimeError(
            f"Found {len(rows)} IN_PROGRESS jobs in {jobs_table_fqname}; expected at most 1."
        )
    return rows[0]


def get_last_successful_watermark(
    conn: DBConnection, jobs_table_fqname: str
) -> Optional[dt.datetime]:
    """MaxSourceModifiedDate from the most recent SUCCESS or PARTIAL_SUCCESS
    job, used only as an optional pre-filter -- never the sole inclusion
    gate (see §4 of the build prompt)."""
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT TOP 1 MaxSourceModifiedDate
        FROM {jobs_table_fqname}
        WHERE Status IN ('SUCCESS', 'PARTIAL_SUCCESS')
          AND MaxSourceModifiedDate IS NOT NULL
        ORDER BY RunEndTime DESC
        """
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def fetch_changed_records(
    conn: DBConnection,
    view_fqname: str,
    records_table_fqname: str,
    watermark_prefilter: Optional[dt.datetime],
) -> List[dict]:
    """
    `watermark_prefilter` is applied as an *optional* AND'd pre-filter to
    keep the query cheap on a large view -- it is never the sole gate for
    inclusion. Pass None on the very first run (no prior successful job);
    the caller is responsible for deciding the initial watermark value if
    it wants to pre-filter at all on first run (the prompt allows using the
    configured initial watermark here too, since it's just a performance
    optimization, not a correctness gate).
    """
    prefilter_clause = ""
    params: list = []
    if watermark_prefilter is not None:
        prefilter_clause = "AND src.LastModifiedDate > ?"
        params.append(watermark_prefilter)

    query = f"""
        SELECT src.*,
               CASE WHEN rec.SFDC_OpportunityId IS NULL THEN 'INSERT' ELSE 'UPDATE' END AS SyncActionHint
        FROM {view_fqname} AS src
        LEFT JOIN (
            SELECT SFDC_OpportunityId, SyncTimestamp,
                   ROW_NUMBER() OVER (
                       PARTITION BY SFDC_OpportunityId ORDER BY SyncTimestamp DESC
                   ) AS rn
            FROM {records_table_fqname}
            WHERE RecordStatus = 'SUCCESS'
        ) AS rec
            ON rec.SFDC_OpportunityId = src.SFDC_OpportunityId
            AND rec.rn = 1
        WHERE (
            rec.SFDC_OpportunityId IS NULL
            OR src.LastModifiedDate > rec.SyncTimestamp
            OR src.CreatedDate > rec.SyncTimestamp
        )
        {prefilter_clause}
    """
    cur = conn.cursor()
    cur.execute(query, params) if params else cur.execute(query)
    records = _rows_to_dicts(cur)
    cur.close()
    return records
