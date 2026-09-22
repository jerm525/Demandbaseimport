"""
Demandbase API client.

Fixes applied relative to the starting-point functions given in the build
prompt (see §6):
  - No mutable default-argument dicts for headers. A fresh headers dict is
    built inside every call.
  - The bearer token is read fresh on every call via config.get_demandbase_token,
    never captured once at function-definition/import time.
  - Every request has an explicit timeout=.
  - Transient errors (network errors, 5xx) are retried with backoff via
    tenacity; 4xx errors are never retried.
  - Failures raise DemandbaseJobError (or a subclass) instead of returning
    None, so callers can't silently swallow a failure.
  - Success is checked with response.ok / an explicit set of accepted codes,
    not a single literal `== 200`.
  - File I/O and request calls are wrapped so a missing file or network
    failure produces a clean, catchable exception rather than an unhandled
    stack trace.

Polling: Demandbase has not yet provided a documented status endpoint. Rather
than guess a URL, polling is built against the `StatusPoller` Protocol below.
`HttpStatusPoller` is a best-effort real implementation that assumes a
plausible GET .../job/{id} shape and is clearly marked as unconfirmed; swap
it out (or fix its URL/response parsing) once Demandbase confirms the real
contract. `MockStatusPoller` is provided for tests and for exercising the
orchestrator before the real endpoint exists.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import AppConfig, get_demandbase_token

logger = logging.getLogger(__name__)


class DemandbaseJobError(Exception):
    """Raised for any Demandbase job-creation/submission/polling failure.

    Callers (the orchestrator) catch this, mark the job FAILED, move the CSV
    to error/, and fire an alert -- it is never silently swallowed.
    """


class DemandbaseTransientError(DemandbaseJobError):
    """Retryable: network error or 5xx response."""


class DemandbasePermanentError(DemandbaseJobError):
    """Not retryable: 4xx response or similar client-side failure."""


class DemandbaseTimeoutError(DemandbaseJobError):
    """Polling exceeded the configured max total wait time."""


def _accepted(response: requests.Response) -> bool:
    """Success check via response.ok / an explicit accepted-code set, never
    a single literal `== 200`."""
    return response.ok or response.status_code in (200, 201, 202)


def _raise_for_status_class(response: requests.Response, action: str) -> None:
    if _accepted(response):
        return
    if 500 <= response.status_code < 600:
        raise DemandbaseTransientError(
            f"{action} failed with server error {response.status_code}: {response.text[:500]}"
        )
    raise DemandbasePermanentError(
        f"{action} failed with client error {response.status_code}: {response.text[:500]}"
    )


def _retry_policy():
    return retry(
        retry=retry_if_exception_type((DemandbaseTransientError, requests.RequestException)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )


@dataclass
class JobStatusResult:
    job_id: str
    status: str  # one of: RUNNING, SUCCESS, PARTIAL_SUCCESS, FAILED (poller-facing)
    records_sent: Optional[int] = None
    records_succeeded: Optional[int] = None
    records_failed: Optional[int] = None
    record_errors: List[Dict[str, Any]] = field(default_factory=list)
    raw: Optional[dict] = None


class StatusPoller(Protocol):
    """Injectable so the real (not-yet-confirmed) endpoint can be dropped in
    without changing any orchestrator/polling-loop code."""

    def get_status(self, job_id: str) -> JobStatusResult: ...


class DemandbaseClient:
    def __init__(self, config: AppConfig, poller: Optional[StatusPoller] = None):
        self._config = config
        self._poller = poller or HttpStatusPoller(config)

    def _headers(self) -> Dict[str, str]:
        # Built fresh every call -- never a shared/mutable default dict, and
        # the token is read fresh every call too.
        token = get_demandbase_token(self._config)
        return {"Authorization": f"Bearer {token}"}

    @_retry_policy()
    def create_import_job(self, import_name: str) -> str:
        """POST /import/v1/job -> returns the Demandbase job id.

        This id is used verbatim as JobId in tbl__demandbase_jobs -- we
        never generate a separate internal job identifier.
        """
        url = f"{self._config.demandbase.base_url}/import/v1/job"
        headers = self._headers()
        body = {
            "dataImportName": import_name,
            "entityType": self._config.demandbase.entity_type,
            "source": self._config.demandbase.source,
        }
        try:
            response = requests.post(
                url,
                json=body,
                headers=headers,
                timeout=self._config.demandbase.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise DemandbaseTransientError(f"Network error creating job: {exc}") from exc

        _raise_for_status_class(response, "Job creation")
        try:
            data = response.json()
            job_id = data["id"]
        except (ValueError, KeyError) as exc:
            raise DemandbasePermanentError(
                f"Job creation succeeded but response was not the expected shape: {response.text[:500]}"
            ) from exc
        return str(job_id)

    @_retry_policy()
    def submit_csv(self, job_id: str, csv_path: Path) -> None:
        """PUT /import/v1/job/{job_id}/data with raw CSV bytes.

        A success response means the file was *accepted for processing*,
        not that every record finished importing -- callers must poll.
        """
        url = f"{self._config.demandbase.base_url}/import/v1/job/{job_id}/data"
        headers = self._headers()
        headers["Content-Type"] = "application/octet-stream"

        try:
            with open(csv_path, "rb") as fh:
                payload = fh.read()
        except OSError as exc:
            raise DemandbasePermanentError(f"Could not read CSV file {csv_path}: {exc}") from exc

        try:
            response = requests.put(
                url,
                data=payload,
                headers=headers,
                timeout=self._config.demandbase.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise DemandbaseTransientError(f"Network error submitting CSV: {exc}") from exc

        _raise_for_status_class(response, "CSV submission")

    def poll_to_completion(self, job_id: str) -> JobStatusResult:
        """Interval-based polling with exponential backoff and a configurable
        max total timeout. On timeout, raises DemandbaseTimeoutError so the
        caller marks the job FAILED and alerts."""
        cfg = self._config.demandbase
        interval = cfg.poll_initial_interval_seconds
        elapsed = 0.0

        while True:
            result = self._poller.get_status(job_id)
            if result.status in ("completed", "processing", "new", "failed"):
                return result

            if elapsed >= cfg.poll_max_total_seconds:
                raise DemandbaseTimeoutError(
                    f"Job {job_id} did not complete within "
                    f"{cfg.poll_max_total_seconds}s (last status: {result.status})"
                )

            time.sleep(interval)
            elapsed += interval
            interval = min(interval * cfg.poll_backoff_multiplier, cfg.poll_max_interval_seconds)


class HttpStatusPoller:
    """*** UNCONFIRMED ENDPOINT ***

    Demandbase has not provided a documented status/polling endpoint. This
    implementation assumes a plausible `GET {base_url}/import/v1/job/{id}`
    shape purely so the rest of the pipeline has something concrete to run
    against. DO NOT ship this to production without confirming the real URL
    and response shape with Demandbase -- see README "Flagged Items".
    """

    def __init__(self, config: AppConfig):
        self._config = config

    def _headers(self) -> Dict[str, str]:
        token = get_demandbase_token(self._config)
        return {"Authorization": f"Bearer {token}"}

    @_retry_policy()
    def get_status(self, job_id: str) -> JobStatusResult:
        url = f"{self._config.demandbase.base_url}/import/v1/job/{job_id}"
        try:
            response = requests.get(
                url,
                headers=self._headers(),
                timeout=self._config.demandbase.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise DemandbaseTransientError(f"Network error polling job {job_id}: {exc}") from exc

        _raise_for_status_class(response, f"Polling job {job_id}")
        try:
            data = response.json()
        except ValueError as exc:
            raise DemandbasePermanentError(
                f"Polling response for job {job_id} was not valid JSON: {response.text[:500]}"
            ) from exc

        # ASSUMED shape -- adjust once recordErrors is confirmed.
        return JobStatusResult(
            job_id=job_id,
            status=data.get("state", "RUNNING"),
            records_sent=data.get("totalRowsIngested"),
            records_succeeded=data.get("totalValidRecords"),
            records_failed=data.get("totalInvalidRecords"),
            record_errors=data.get("recordErrors", []),
            raw=data,
        )


class MockStatusPoller:
    """Simple in-memory poller for tests: pass a pre-scripted sequence of
    JobStatusResult objects to return on successive calls."""

    def __init__(self, sequence: List[JobStatusResult]):
        self._sequence = list(sequence)
        self._calls = 0

    def get_status(self, job_id: str) -> JobStatusResult:
        idx = min(self._calls, len(self._sequence) - 1)
        self._calls += 1
        return self._sequence[idx]
