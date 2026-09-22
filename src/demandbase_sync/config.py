"""
Configuration loading for the Demandbase sync application.

Design notes
------------
- Supports dev/test/prod via an explicit `--env` flag (or DEMANDBASE_SYNC_ENV
  env var). Each environment has its own YAML file under config/ so a broken
  dev run cannot touch prod tables or folders.
- No credentials ever live in the YAML files or in source control. The SQL
  connection string and the Demandbase bearer token are read from environment
  variables (or, if DEMANDBASE_SECRETS_BACKEND=env_vars is swapped out later,
  from whatever secrets store is plugged in -- see `get_demandbase_token`).
- The Demandbase token is deliberately NOT cached anywhere on the config
  object. It is fetched fresh every time `get_demandbase_token()` is called,
  so a rotated token is picked up without restarting the process (per the
  build prompt's explicit requirement in the "fix these bugs" section).
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator


class Environment(str, Enum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class DatabaseConfig(BaseModel):
    """SQL Server connection details.

    The actual connection string is assembled from environment variables at
    connect time (see db.get_connection). What lives in YAML here is only
    non-secret shape: driver name, DSN alias, timeouts.
    """

    driver: str = "{ODBC Driver 18 for SQL Server}"
    dsn_env_var: str = "DEMANDBASE_SYNC_SQL_CONN_STR"
    connect_timeout_seconds: int = 30
    view_fqname: str = "[marketing_bi].[EDA-268].[vw__demandbase_Opportunity_import]"
    jobs_table_fqname: str = "[marketing_bi].[EDA-268].[tbl__demandbase_jobs]"
    records_table_fqname: str = "[marketing_bi].[EDA-268].[tbl__demandbase_records]"


class DemandbaseConfig(BaseModel):
    base_url: str = "https://uapi.demandbase.com"
    token_env_var: str = "DEMANDBASE_API_TOKEN"
    entity_type: str = "Opportunity"
    source: str = "CSV"
    request_timeout_seconds: float = 30.0

    # Polling config. The actual status endpoint is NOT YET CONFIRMED -- see
    # demandbase_client.py. These knobs govern the injected poller regardless
    # of what URL eventually gets wired in.
    poll_initial_interval_seconds: float = 5.0
    poll_backoff_multiplier: float = 2.0
    poll_max_interval_seconds: float = 60.0
    poll_max_total_seconds: float = 1800.0  # 30 min overall timeout

    # UNCONFIRMED (see README "Flagged Items"): real per-job limits are not
    # documented anywhere we were given. This is a conservative placeholder.
    max_records_per_batch: int = 5000
    # UNCONFIRMED: whether job creation is rate-limited. 0 = no delay.
    inter_batch_delay_seconds: float = 0.0

    # TC7: whether a Demandbase job should still be created when there are
    # zero changed records. Explicitly a config flag, not hardcoded.
    create_job_on_empty_changeset: bool = False


class PathsConfig(BaseModel):
    staging_dir: Path = Path("./staging")
    archive_dir: Path = Path("./archive")
    error_dir: Path = Path("./error")
    run_log_dir: Path = Path("./logs/runs")
    all_runs_error_log: Path = Path("./logs/all_runs_errors.log")
    retention_days: int = 90


class WatermarkConfig(BaseModel):
    # UNCONFIRMED business value: the go-live cutover date used only on the
    # very first run, when no prior successful job exists. Must be set per
    # environment before production go-live.
    initial_watermark_utc: str = "2000-01-01T00:00:00Z"


class NotifierConfig(BaseModel):
    # UNCONFIRMED (see README): actual channel/recipient not yet decided.
    # "noop" logs only; "email" / "webhook" are stubbed and ready to wire in.
    channel: str = "noop"
    webhook_url_env_var: Optional[str] = "DEMANDBASE_SYNC_ALERT_WEBHOOK_URL"
    email_to_env_var: Optional[str] = "DEMANDBASE_SYNC_ALERT_EMAIL_TO"


class AppConfig(BaseModel):
    environment: Environment
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    demandbase: DemandbaseConfig = Field(default_factory=DemandbaseConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    watermark: WatermarkConfig = Field(default_factory=WatermarkConfig)
    notifier: NotifierConfig = Field(default_factory=NotifierConfig)
    field_mapping_path: Path = Path("./field_mapping.yaml")
    # Filename collision handling (§10): we always append the JobId, so this
    # is here mostly to make the "one run per day" alternative easy to flip
    # to if that's ever preferred instead.
    one_run_per_calendar_day: bool = False

    @field_validator("field_mapping_path", "paths", mode="before")
    @classmethod
    def _passthrough(cls, v):
        return v


def get_demandbase_token(config: AppConfig) -> str:
    """Read the bearer token fresh on every call -- never cache this value.

    Raises RuntimeError if the environment variable isn't set, so a missing
    secret fails loudly instead of silently sending unauthenticated requests.
    """
    var_name = config.demandbase.token_env_var
    token = os.environ.get(var_name)
    if not token:
        raise RuntimeError(
            f"Demandbase token not found in environment variable '{var_name}'. "
            "Set it before running (never commit it to source control)."
        )
    return token


def get_sql_connection_string(config: AppConfig) -> str:
    var_name = config.database.dsn_env_var
    conn_str = os.environ.get(var_name)
    if not conn_str:
        raise RuntimeError(
            f"SQL connection string not found in environment variable '{var_name}'."
        )
    return conn_str


def _config_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "config"


def load_config(env: Optional[str] = None) -> AppConfig:
    """Load config for the given environment (dev/test/prod).

    Resolution order for the environment name:
      1. explicit `env` argument
      2. DEMANDBASE_SYNC_ENV environment variable
      3. defaults to "dev" (never defaults to prod, by design)
    """
    env_name = (env or os.environ.get("DEMANDBASE_SYNC_ENV") or "dev").lower()
    try:
        environment = Environment(env_name)
    except ValueError as exc:
        raise ValueError(
            f"Unknown environment '{env_name}'. Must be one of: "
            f"{[e.value for e in Environment]}"
        ) from exc

    config_path = _config_dir() / f"{environment.value}.yaml"
    raw: dict = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    raw["environment"] = environment.value
    return AppConfig(**raw)
