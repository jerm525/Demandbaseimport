import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from demandbase_sync.config import AppConfig, Environment
from demandbase_sync.mapping import FieldMapping


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self._conn = conn
        self.description = None
        self._results = []
        self.last_query = None
        self.last_params = None

    def execute(self, query, params=None):
        self.last_query = query
        self.last_params = params
        query_upper = query.strip().upper()

        if query_upper.startswith("SELECT TOP 0"):
            self.description = [(c,) for c in self._conn.view_columns]
            self._results = []
        elif "FROM " in query_upper and "TBL__DEMANDBASE_JOBS" in query_upper and "WHERE STATUS = 'IN_PROGRESS'" in query_upper:
            self.description = self._conn.job_table_description()
            rows = [r for r in self._conn.jobs if r.get("Status") == "IN_PROGRESS"]
            self._results = [self._conn.job_row_tuple(r) for r in rows]
        elif "MAXSOURCEMODIFIEDDATE" in query_upper and "TOP 1" in query_upper:
            self.description = [("MaxSourceModifiedDate",)]
            candidates = [
                r["MaxSourceModifiedDate"]
                for r in self._conn.jobs
                if r.get("Status") in ("SUCCESS", "PARTIAL_SUCCESS") and r.get("MaxSourceModifiedDate")
            ]
            self._results = [(max(candidates),)] if candidates else []
        elif "FROM " in query_upper and query_upper.strip().startswith("SELECT SRC.*"):
            self.description = [(c,) for c in self._conn.result_columns]
            self._results = list(self._conn.query_results)
        elif query_upper.startswith("INSERT INTO") and "TBL__DEMANDBASE_JOBS" in query_upper:
            job_id, entity_type, run_start_time, created_ts = params
            self._conn.jobs.append(
                {
                    "JobId": job_id,
                    "EntityType": entity_type,
                    "Status": "IN_PROGRESS",
                    "RunStartTime": run_start_time,
                    "RunEndTime": None,
                    "RecordsSent": None,
                    "RecordsSucceeded": None,
                    "RecordsFailed": None,
                    "MaxSourceModifiedDate": None,
                    "CreatedTimestamp": created_ts,
                }
            )
        elif query_upper.startswith("UPDATE") and "TBL__DEMANDBASE_JOBS" in query_upper:
            status, run_end_time, sent, succeeded, failed, watermark, job_id = params
            for row in self._conn.jobs:
                if row["JobId"] == job_id:
                    row.update(
                        Status=status,
                        RunEndTime=run_end_time,
                        RecordsSent=sent,
                        RecordsSucceeded=succeeded,
                        RecordsFailed=failed,
                        MaxSourceModifiedDate=watermark,
                    )
        elif query_upper.startswith("INSERT INTO") and "TBL__DEMANDBASE_RECORDS" in query_upper:
            self._conn.inserted_records.append(params)
        else:
            self.description = []
            self._results = []

    def executemany(self, query, rows):
        for row in rows:
            self.execute(query, row)

    def fetchall(self):
        return self._results

    def fetchone(self):
        return self._results[0] if self._results else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self, view_columns, result_columns=None, query_results=None):
        self.view_columns = view_columns
        self.result_columns = result_columns or view_columns
        self.query_results = query_results or []
        self.jobs = []
        self.inserted_records = []  # list of param tuples
        self.committed = 0

    def job_table_description(self):
        return [
            (c,)
            for c in [
                "JobId",
                "EntityType",
                "Status",
                "RunStartTime",
                "RunEndTime",
                "RecordsSent",
                "RecordsSucceeded",
                "RecordsFailed",
                "MaxSourceModifiedDate",
                "CreatedTimestamp",
            ]
        ]

    def job_row_tuple(self, row):
        cols = [c[0] for c in self.job_table_description()]
        return tuple(row.get(c) for c in cols)

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def close(self):
        pass


VIEW_COLUMNS = [
    "SFDC_ID",
    "AccountName",
    "AccountDomain",
    "CustomerType",
    "OppType",
    "ServiceType",
    "ServiceType2",
    "OppName",
    "CreatedDate",
    "ExpectedCloseDate",
    "CloseDate",
    "IsClosed",
    "IsWon",
    "StageName",
    "AmountMrr",
    "AmountConsumption",
    "AmountProserv",
    "PipelineMrr",
    "PipelineConsumption",
    "PipelineProserve",
    "AeName",
    "OwnerName",
    "Probability",
    "Territory",
    "TargetAccount",
    "LastModifiedDate",
]


@pytest.fixture
def test_mapping() -> list[FieldMapping]:
    simple = [
        ("SFDC_ID", "SFDC_OpportunityId", "none", True, None),
        ("AccountName", "account_name", "none", True, None),
        ("AccountDomain", "account_domain", "none", False, ""),
        ("CustomerType", "customer_type", "none", False, ""),
        ("OppType", "Type", "none", False, ""),
        ("ServiceType", "service_type", "none", False, ""),
        ("ServiceType2", "service_type2", "none", False, ""),
        ("OppName", "Opportunity Name", "none", True, None),
        ("CreatedDate", "Created Date", "date_format:%Y-%m-%d", True, None),
        ("ExpectedCloseDate", "expected_close", "date_format:%Y-%m-%d", False, None),
        ("CloseDate", "Close Date", "date_format:%Y-%m-%d", False, None),
        ("IsClosed", "Is Closed?", "bool_to_yn", False, "N"),
        ("IsWon", "Is Won?", "bool_to_yn", False, "N"),
        ("StageName", "Stage", "none", True, None),
        ("AmountMrr", "amount_mrr", "decimal_round_2", False, 0.0),
        ("AmountConsumption", "amount_consumption", "decimal_round_2", False, 0.0),
        ("AmountProserv", "amount_proserv", "decimal_round_2", False, 0.0),
        ("PipelineMrr", "pipeline_mrr", "decimal_round_2", False, 0.0),
        ("PipelineConsumption", "pipeline_consumption", "decimal_round_2", False, 0.0),
        ("PipelineProserve", "pipeline_proserve", "decimal_round_2", False, 0.0),
        ("AeName", "AE Name", "none", False, ""),
        ("OwnerName", "Owner", "none", False, ""),
        ("Probability", "Probability", "decimal_round_2", False, 0.0),
        ("Territory", "Territory", "none", False, ""),
        ("TargetAccount", "Target Account", "bool_to_yn", False, "N"),
    ]
    return [
        FieldMapping(source_field=s, target_field=t, transform=tr, required=r, default=d)
        for s, t, tr, r, d in simple
    ]


def make_source_record(sfdc_id="006ABC000000001", **overrides):
    base = dict(
        SFDC_ID=sfdc_id,
        AccountName="Acme Corp",
        AccountDomain="acme.com",
        CustomerType="Enterprise",
        OppType="New Business",
        ServiceType="Cloud",
        ServiceType2="",
        OppName="Acme Q3 Deal",
        CreatedDate=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        ExpectedCloseDate=dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc),
        CloseDate=None,
        IsClosed=False,
        IsWon=False,
        StageName="Negotiation",
        AmountMrr=1000.555,
        AmountConsumption=0,
        AmountProserv=0,
        PipelineMrr=0,
        PipelineConsumption=0,
        PipelineProserve=0,
        AeName="Jane AE",
        OwnerName="Jane AE",
        Probability=50,
        Territory="NA",
        TargetAccount=True,
        LastModifiedDate=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc),
        SyncActionHint="INSERT",
    )
    base.update(overrides)
    return base


@pytest.fixture
def app_config(tmp_path) -> AppConfig:
    cfg = AppConfig(environment=Environment.TEST)
    cfg.paths.staging_dir = tmp_path / "staging"
    cfg.paths.archive_dir = tmp_path / "archive"
    cfg.paths.error_dir = tmp_path / "error"
    cfg.paths.run_log_dir = tmp_path / "logs" / "runs"
    cfg.paths.all_runs_error_log = tmp_path / "logs" / "errors.log"
    cfg.demandbase.max_records_per_batch = 5000
    cfg.demandbase.poll_initial_interval_seconds = 0.01
    cfg.demandbase.poll_max_interval_seconds = 0.02
    cfg.demandbase.poll_max_total_seconds = 0.2
    cfg.field_mapping_path = tmp_path / "field_mapping.yaml"
    return cfg
