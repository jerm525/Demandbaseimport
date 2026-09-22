"""
Structured (JSON lines) logging.

- One log file per run, named with a run identifier and (once known) tagged
  with the batch's JobId in every record, so every line can be correlated
  back to the Demandbase job it belongs to.
- ERROR-level entries are mirrored into a separate rotating all-runs log for
  quick scanning across days.
- Uses only the standard `logging` module -- no icecream/ic() anywhere.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path
from typing import Optional

from .config import PathsConfig


class JsonLinesFormatter(logging.Formatter):
    def __init__(self, run_id: str):
        super().__init__()
        self._run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "run_id": self._run_id,
            "job_id": getattr(record, "job_id", None),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class JobIdAdapter(logging.LoggerAdapter):
    """Attaches the current batch's JobId to every log line, once known."""

    def process(self, msg, kwargs):
        kwargs.setdefault("extra", {})
        kwargs["extra"]["job_id"] = self.extra.get("job_id")
        return msg, kwargs

    def set_job_id(self, job_id: Optional[str]) -> None:
        self.extra["job_id"] = job_id


def configure_run_logger(paths: PathsConfig, run_id: str) -> JobIdAdapter:
    """Set up the per-run JSON-lines log file plus the rotating all-runs
    error-only log. Returns a LoggerAdapter that tags every line with the
    current job_id (settable as it becomes known during the run)."""
    paths.run_log_dir.mkdir(parents=True, exist_ok=True)
    paths.all_runs_error_log.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"demandbase_sync.run.{run_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    run_log_path = paths.run_log_dir / f"run_{run_id}.jsonl"
    file_handler = logging.FileHandler(run_log_path, encoding="utf-8")
    file_handler.setFormatter(JsonLinesFormatter(run_id))
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        paths.all_runs_error_log, maxBytes=10_000_000, backupCount=10, encoding="utf-8"
    )
    error_handler.setFormatter(JsonLinesFormatter(run_id))
    error_handler.setLevel(logging.ERROR)
    logger.addHandler(error_handler)

    # Also echo to console for interactive/manual runs.
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    return JobIdAdapter(logger, {"job_id": None})
