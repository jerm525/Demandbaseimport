import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from demandbase_sync.config import AppConfig, Environment
from demandbase_sync.demandbase_client import (
    DemandbaseClient,
    DemandbasePermanentError,
    DemandbaseTimeoutError,
    DemandbaseTransientError,
    JobStatusResult,
    MockStatusPoller,
)


@pytest.fixture
def config(tmp_path):
    cfg = AppConfig(environment=Environment.TEST)
    cfg.demandbase.poll_initial_interval_seconds = 0.01
    cfg.demandbase.poll_max_interval_seconds = 0.02
    cfg.demandbase.poll_max_total_seconds = 0.1
    return cfg


@pytest.fixture(autouse=True)
def token_env():
    os.environ["DEMANDBASE_API_TOKEN"] = "token-v1"
    yield
    os.environ.pop("DEMANDBASE_API_TOKEN", None)


def _mock_response(status_code, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


def test_create_import_job_success(config):
    client = DemandbaseClient(config, poller=MockStatusPoller([]))
    with patch("requests.post", return_value=_mock_response(200, {"id": "job-123"})) as mock_post:
        job_id = client.create_import_job("test-import")
    assert job_id == "job-123"
    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer token-v1"
    assert "timeout" in mock_post.call_args.kwargs


def test_create_import_job_reads_token_fresh_each_call(config):
    client = DemandbaseClient(config, poller=MockStatusPoller([]))
    with patch("requests.post", return_value=_mock_response(200, {"id": "job-1"})):
        client.create_import_job("first")
    os.environ["DEMANDBASE_API_TOKEN"] = "token-v2"
    with patch("requests.post", return_value=_mock_response(200, {"id": "job-2"})) as mock_post:
        client.create_import_job("second")
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer token-v2"


def test_create_import_job_4xx_is_not_retried(config):
    client = DemandbaseClient(config, poller=MockStatusPoller([]))
    with patch("requests.post", return_value=_mock_response(400, text="bad request")) as mock_post:
        with pytest.raises(DemandbasePermanentError):
            client.create_import_job("test-import")
    assert mock_post.call_count == 1


def test_create_import_job_5xx_is_retried(config):
    client = DemandbaseClient(config, poller=MockStatusPoller([]))
    with patch("requests.post", return_value=_mock_response(503, text="server error")) as mock_post:
        with pytest.raises(DemandbaseTransientError):
            client.create_import_job("test-import")
    assert mock_post.call_count > 1  # tenacity retried


def test_submit_csv_missing_file_raises_clean_error(config, tmp_path):
    client = DemandbaseClient(config, poller=MockStatusPoller([]))
    missing = tmp_path / "nope.csv"
    with pytest.raises(DemandbasePermanentError):
        client.submit_csv("job-1", missing)


def test_submit_csv_success(config, tmp_path):
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("a,b\n1,2\n")
    client = DemandbaseClient(config, poller=MockStatusPoller([]))
    with patch("requests.put", return_value=_mock_response(202)) as mock_put:
        client.submit_csv("job-1", csv_path)
    assert mock_put.call_args.kwargs["headers"]["Content-Type"] == "application/octet-stream"


def test_headers_are_not_shared_mutable_state(config, tmp_path):
    # Regression test for the mutable-default-argument bug called out in the
    # build prompt: submitting a CSV must not corrupt headers used elsewhere.
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("a,b\n1,2\n")
    client = DemandbaseClient(config, poller=MockStatusPoller([]))
    with patch("requests.put", return_value=_mock_response(202)):
        client.submit_csv("job-1", csv_path)
    with patch("requests.post", return_value=_mock_response(200, {"id": "job-2"})) as mock_post:
        client.create_import_job("second")
    assert "Content-Type" not in mock_post.call_args.kwargs["headers"]


def test_poll_to_completion_returns_on_success(config):
    poller = MockStatusPoller(
        [
            JobStatusResult(job_id="job-1", status="RUNNING"),
            JobStatusResult(job_id="job-1", status="SUCCESS", records_sent=2, records_succeeded=2, records_failed=0),
        ]
    )
    client = DemandbaseClient(config, poller=poller)
    result = client.poll_to_completion("job-1")
    assert result.status == "SUCCESS"


def test_poll_to_completion_times_out(config):
    poller = MockStatusPoller([JobStatusResult(job_id="job-1", status="RUNNING")])
    client = DemandbaseClient(config, poller=poller)
    with pytest.raises(DemandbaseTimeoutError):
        client.poll_to_completion("job-1")
