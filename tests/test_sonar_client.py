"""Tests for the SonarQube client addition used by T16/T17.

Covers ``SonarClient.get_ce_task`` (the compute-engine task lookup that lets
T17 wait for *this* analysis) plus a small regression check that the
pre-existing T01/T02 behaviour is preserved. HTTP is fully faked - no real
SonarQube server is contacted.
"""

import pytest
import requests

import sonar_client
from sonar_client import SonarClient, SonarQubeError, SonarQubeNotFoundError


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error
        self.raise_for_status_calls = 0

    def raise_for_status(self):
        self.raise_for_status_calls += 1
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeSession:
    """Records requests and replays canned responses or errors."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.auth = None
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        if not self.responses:
            raise AssertionError("no canned HTTP response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def session(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(sonar_client.requests, "Session", lambda: fake)
    return fake


def make_client(session, *responses, timeout=None):
    session.responses = list(responses)
    return SonarClient(
        "https://sonar.example.com/", "tok-123", "demo", timeout=timeout
    )


class TestGetCeTask:
    def test_returns_the_decoded_payload(self, session):
        payload = {"task": {"id": "AY1", "status": "SUCCESS"}}
        client = make_client(session, FakeResponse(200, payload))
        assert client.get_ce_task("AY1") == payload

    def test_requests_the_ce_task_endpoint_with_the_id(self, session):
        client = make_client(session, FakeResponse(200, {"task": {}}))
        client.get_ce_task("AY1")
        url, params, timeout = session.calls[0]
        assert url == "https://sonar.example.com/api/ce/task"
        assert params == {"id": "AY1"}
        assert timeout == SonarClient.DEFAULT_TIMEOUT_SECONDS

    def test_custom_timeout_is_used(self, session):
        client = make_client(session, FakeResponse(200, {"task": {}}), timeout=3.5)
        client.get_ce_task("AY1")
        assert session.calls[0][2] == 3.5

    def test_token_is_sent_through_session_auth_only(self, session):
        client = make_client(session, FakeResponse(200, {"task": {}}))
        client.get_ce_task("AY1")
        assert session.auth == ("tok-123", "")
        url, params, _timeout = session.calls[0]
        assert "tok-123" not in url
        assert "tok-123" not in str(params)

    def test_404_raises_not_found_error(self, session):
        client = make_client(session, FakeResponse(404, {"errors": []}))
        with pytest.raises(SonarQubeNotFoundError, match="was not found"):
            client.get_ce_task("AY1")

    def test_not_found_error_is_a_sonar_error(self, session):
        client = make_client(session, FakeResponse(404))
        with pytest.raises(SonarQubeError):
            client.get_ce_task("AY1")

    def test_http_error_raises_sonar_error(self, session):
        client = make_client(session, FakeResponse(500))
        with pytest.raises(SonarQubeError, match="failed"):
            client.get_ce_task("AY1")

    def test_non_json_response_raises_sonar_error(self, session):
        client = make_client(session, FakeResponse(200, json_error=ValueError("nope")))
        with pytest.raises(SonarQubeError, match="non-JSON"):
            client.get_ce_task("AY1")

    def test_json_decode_error_raises_sonar_error(self, session):
        client = make_client(
            session,
            FakeResponse(
                200,
                json_error=requests.exceptions.JSONDecodeError(
                    "Expecting value", "<html>", 0
                ),
            ),
        )
        with pytest.raises(SonarQubeError):
            client.get_ce_task("AY1")

    def test_non_object_json_raises_sonar_error(self, session):
        client = make_client(session, FakeResponse(200, ["not", "a", "dict"]))
        with pytest.raises(SonarQubeError, match="unexpected response"):
            client.get_ce_task("AY1")

    def test_connection_error_raises_sonar_error(self, session):
        client = make_client(session, requests.ConnectionError("connection refused"))
        with pytest.raises(SonarQubeError, match="failed"):
            client.get_ce_task("AY1")

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_blank_task_id_is_rejected_without_http(self, session, bad):
        client = make_client(session)
        with pytest.raises(SonarQubeError, match="task id must be provided"):
            client.get_ce_task(bad)
        assert session.calls == []



class TestExistingBehaviourIsPreserved:
    def test_verify_project_finds_the_component(self, session):
        client = make_client(
            session, FakeResponse(200, {"components": [{"key": "demo", "name": "Demo"}]})
        )
        assert client.verify_project()["key"] == "demo"
        url, params, _timeout = session.calls[0]
        assert url == "https://sonar.example.com/api/projects/search"
        assert params == {"projects": "demo"}

    def test_verify_project_raises_when_missing(self, session):
        client = make_client(session, FakeResponse(200, {"components": []}))
        with pytest.raises(SonarQubeError, match="was not found"):
            client.verify_project()

    def test_get_open_issues_normalizes_the_response(self, session):
        payload = {
            "paging": {"total": 7, "pageIndex": 1, "pageSize": 100},
            "issues": [{"key": "AX1"}],
        }
        client = make_client(session, FakeResponse(200, payload))
        normalized = client.get_open_issues()
        assert normalized == {
            "total": 7,
            "page_index": 1,
            "page_size": 100,
            "issues": [{"key": "AX1"}],
        }
        url, params, _timeout = session.calls[0]
        assert url == "https://sonar.example.com/api/issues/search"
        assert params == {"componentKeys": "demo", "resolved": "false", "ps": "100"}

    def test_get_open_issues_tolerates_flat_responses(self, session):
        client = make_client(
            session, FakeResponse(200, {"total": 1, "issues": [{"key": "AX1"}]})
        )
        normalized = client.get_open_issues(page_size=5)
        assert normalized["total"] == 1
        assert normalized["page_size"] == 5

    def test_get_open_issues_wraps_transport_errors(self, session):
        client = make_client(session, requests.ConnectionError("connection refused"))
        with pytest.raises(SonarQubeError, match="failed"):
            client.get_open_issues()

