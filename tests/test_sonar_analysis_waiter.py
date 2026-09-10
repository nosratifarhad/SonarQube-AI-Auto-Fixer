"""T17 tests: waiting for the triggered SonarQube analysis to finish.

The clock and the sleeper are injected, so these tests never wait in real time,
and the compute-engine client is a fake - no real SonarQube server is used.
"""

import dataclasses

import pytest

from sonar_analysis_waiter import (
    AnalysisWaitError,
    SonarAnalysisCompletion,
    SonarAnalysisState,
    SonarAnalysisWaiter,
)
from sonar_client import SonarQubeError, SonarQubeNotFoundError

TASK_ID = "AY1abcDEF-2xyz"


class FakeClock:
    """Deterministic clock whose time only advances when the sleeper runs."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)
        self.sleeps = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeCeClient:
    """Returns canned ``/api/ce/task`` payloads (the last one repeats)."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def get_ce_task(self, task_id):
        self.calls.append(task_id)
        if not self.responses:
            raise AssertionError("no canned CE response left")
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def task(status, **extra):
    return {"task": {"id": TASK_ID, "status": status, **extra}}


def waiter(client, clock, **kwargs):
    kwargs.setdefault("timeout_seconds", 60.0)
    kwargs.setdefault("poll_interval_seconds", 5.0)
    return SonarAnalysisWaiter(
        client, clock=clock, sleep=clock.sleep, **kwargs
    )


def test_immediate_success():
    clock = FakeClock()
    client = FakeCeClient(task("SUCCESS", analysisId="A-1", componentKey="demo"))
    result = waiter(client, clock).wait(TASK_ID)
    assert isinstance(result, SonarAnalysisCompletion)
    assert result.status is SonarAnalysisState.SUCCESS
    assert result.succeeded is True
    assert result.terminal is True
    assert result.poll_count == 1
    assert result.elapsed_seconds == 0.0
    assert result.analysis_id == "A-1"
    assert result.component_key == "demo"
    assert result.failure_reason is None
    assert client.calls == [TASK_ID]
    assert clock.sleeps == []


def test_success_after_several_polls():
    clock = FakeClock()
    client = FakeCeClient(
        task("PENDING"), task("IN_PROGRESS"), task("SUCCESS", analysisId="A-9")
    )
    result = waiter(client, clock).wait(TASK_ID)
    assert result.status is SonarAnalysisState.SUCCESS
    assert result.poll_count == 3
    assert result.elapsed_seconds == 10.0
    assert clock.sleeps == [5.0, 5.0]
    assert len(client.calls) == result.poll_count


def test_analysis_failure_includes_the_engine_message():
    clock = FakeClock()
    client = FakeCeClient(
        task("PENDING"),
        task("FAILED", errorMessage="ce failure: bad report"),
    )
    result = waiter(client, clock).wait(TASK_ID)
    assert result.status is SonarAnalysisState.FAILED
    assert result.terminal is True
    assert result.succeeded is False
    assert "bad report" in result.failure_reason
    assert result.poll_count == 2


@pytest.mark.parametrize("raw_status", ["CANCELED", "CANCELLED", "canceled"])
def test_canceled_analysis(raw_status):
    clock = FakeClock()
    result = waiter(FakeCeClient(task(raw_status)), clock).wait(TASK_ID)
    assert result.status is SonarAnalysisState.CANCELED
    assert result.terminal is True
    assert "cancel" in result.failure_reason.lower()


def test_timeout_after_the_budget_is_exhausted():
    clock = FakeClock()
    client = FakeCeClient(task("IN_PROGRESS"))
    result = waiter(client, clock, timeout_seconds=10.0, poll_interval_seconds=5.0).wait(
        TASK_ID
    )
    assert result.status is SonarAnalysisState.TIMEOUT
    assert result.terminal is False
    assert result.elapsed_seconds == 10.0
    assert result.poll_count == 2
    assert "did not reach a terminal state" in result.reason


def test_timeout_is_deterministic():
    def run():
        clock = FakeClock()
        client = FakeCeClient(task("PENDING"))
        return waiter(client, clock, timeout_seconds=10.0).wait(TASK_ID)

    assert run() == run()


def test_bounded_polls_even_with_a_frozen_clock():
    """No infinite loop: a clock that never advances still terminates."""
    client = FakeCeClient(task("PENDING"))
    result = SonarAnalysisWaiter(
        client,
        timeout_seconds=10.0,
        poll_interval_seconds=5.0,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    ).wait(TASK_ID)
    assert result.status is SonarAnalysisState.TIMEOUT
    assert result.poll_count == 3
    assert len(client.calls) == 3


def test_explicit_max_polls_bound():
    clock = FakeClock()
    client = FakeCeClient(task("PENDING"))
    result = waiter(client, clock, max_polls=2).wait(TASK_ID)
    assert result.status is SonarAnalysisState.TIMEOUT
    assert result.poll_count == 2


def test_task_not_found_is_unknown():
    clock = FakeClock()
    client = FakeCeClient(SonarQubeNotFoundError("task 'X' was not found (HTTP 404)."))
    result = waiter(client, clock).wait(TASK_ID)
    assert result.status is SonarAnalysisState.UNKNOWN
    assert result.succeeded is False
    assert result.poll_count == 1
    assert "not found" in result.failure_reason


def test_task_not_found_is_a_sonar_error_subclass():
    assert issubclass(SonarQubeNotFoundError, SonarQubeError)


class ScriptedClock:
    """Returns pre-scripted clock readings; sleeps are recorded only."""

    def __init__(self, *values) -> None:
        self.values = list(values)
        self.index = 0
        self.sleeps = []

    def __call__(self) -> float:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return float(value)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class TestApiFailures:
    def test_repeated_api_failures_end_as_unknown(self):
        clock = FakeClock()
        client = FakeCeClient(SonarQubeError("connection refused"))
        result = waiter(client, clock).wait(TASK_ID)
        assert result.status is SonarAnalysisState.UNKNOWN
        assert result.poll_count == 3
        assert "connection refused" in result.failure_reason

    def test_transient_api_failure_recovers(self):
        clock = FakeClock()
        client = FakeCeClient(
            SonarQubeError("temporary 500"),
            task("SUCCESS", analysisId="A-2"),
        )
        result = waiter(client, clock).wait(TASK_ID)
        assert result.status is SonarAnalysisState.SUCCESS
        assert result.poll_count == 2
        assert result.analysis_id == "A-2"

    def test_configurable_api_failure_tolerance(self):
        clock = FakeClock()
        client = FakeCeClient(SonarQubeError("nope"))
        result = waiter(client, clock, max_consecutive_api_errors=1).wait(TASK_ID)
        assert result.status is SonarAnalysisState.UNKNOWN
        assert result.poll_count == 1

    def test_api_error_text_is_credential_redacted(self):
        clock = FakeClock()
        client = FakeCeClient(
            SonarQubeError("request to https://bob:hunter2@example.com/api failed")
        )
        result = waiter(client, clock, max_consecutive_api_errors=1).wait(TASK_ID)
        assert "hunter2" not in result.failure_reason
        assert "***@example.com" in result.failure_reason


class TestMalformedResponses:
    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            "hello",
            {},
            {"task": None},
            {"task": "not-a-mapping"},
            {"task": {}},
            {"task": {"status": None}},
            {"task": {"status": "   "}},
            {"task": {"status": "WIBBLE"}},
        ],
    )
    def test_uninterpretable_payload_stops_as_unknown(self, payload):
        clock = FakeClock()
        result = waiter(FakeCeClient(payload), clock).wait(TASK_ID)
        assert result.status is SonarAnalysisState.UNKNOWN
        assert result.poll_count == 1
        assert "could not be interpreted" in result.reason
        assert clock.sleeps == []

    def test_lowercase_status_is_accepted(self):
        clock = FakeClock()
        result = waiter(FakeCeClient(task("success")), clock).wait(TASK_ID)
        assert result.status is SonarAnalysisState.SUCCESS

    def test_analysis_id_like_values_are_redacted_and_capped(self):
        clock = FakeClock()
        payload = task(
            "FAILED",
            errorMessage="boom https://bob:hunter2@example.com/x " + "z" * 900,
        )
        result = waiter(FakeCeClient(payload), clock).wait(TASK_ID)
        assert "hunter2" not in result.failure_reason
        assert len(result.failure_reason) <= 500

    def test_error_field_is_used_when_error_message_is_absent(self):
        clock = FakeClock()
        result = waiter(FakeCeClient(task("FAILED", error="engine exploded")), clock).wait(
            TASK_ID
        )
        assert result.status is SonarAnalysisState.FAILED
        assert "engine exploded" in result.failure_reason


class TestTaskIdentity:
    def test_no_task_id_is_refused_without_guessing(self):
        clock = FakeClock()
        client = FakeCeClient(task("SUCCESS"))
        result = waiter(client, clock).wait(None)
        assert result.status is SonarAnalysisState.UNKNOWN
        assert result.task_id is None
        assert result.poll_count == 0
        assert client.calls == []
        assert "cannot be attributed" in result.failure_reason

    def test_blank_task_id_is_refused(self):
        clock = FakeClock()
        client = FakeCeClient(task("SUCCESS"))
        assert waiter(client, clock).wait("   ").status is SonarAnalysisState.UNKNOWN
        assert client.calls == []

    def test_resolver_is_used_when_no_task_id_is_given(self):
        clock = FakeClock()
        client = FakeCeClient(task("SUCCESS", analysisId="A-3"))
        result = waiter(client, clock).wait(None, task_id_resolver=lambda: TASK_ID)
        assert result.status is SonarAnalysisState.SUCCESS
        assert result.task_id == TASK_ID
        assert client.calls == [TASK_ID]

    def test_resolver_returning_nothing_stays_unknown(self):
        clock = FakeClock()
        result = waiter(FakeCeClient(task("SUCCESS")), clock).wait(
            None, task_id_resolver=lambda: None
        )
        assert result.status is SonarAnalysisState.UNKNOWN

    def test_resolver_failure_is_a_caller_error(self):
        clock = FakeClock()

        def broken_resolver():
            raise SonarQubeError("activity endpoint unavailable")

        with pytest.raises(AnalysisWaitError, match="could not identify"):
            waiter(FakeCeClient(task("SUCCESS")), clock).wait(
                None, task_id_resolver=broken_resolver
            )

    @pytest.mark.parametrize("unsafe", ["../../etc/passwd", "a b", "x" * 300, 5])
    def test_unsafe_task_id_is_rejected(self, unsafe):
        clock = FakeClock()
        with pytest.raises(AnalysisWaitError, match="Unsafe task id"):
            waiter(FakeCeClient(task("SUCCESS")), clock).wait(unsafe)

    def test_unsafe_resolver_result_is_rejected(self):
        clock = FakeClock()
        with pytest.raises(AnalysisWaitError, match="unsafe id"):
            waiter(FakeCeClient(task("SUCCESS")), clock).wait(
                None, task_id_resolver=lambda: "bad id!"
            )

    def test_task_id_is_carried_on_every_outcome(self):
        clock = FakeClock()
        result = waiter(FakeCeClient(task("FAILED")), clock).wait(TASK_ID)
        assert result.task_id == TASK_ID



class TestConfigurationValidation:
    def test_client_without_ce_support_is_rejected(self):
        with pytest.raises(AnalysisWaitError, match="get_ce_task"):
            SonarAnalysisWaiter(object())

    def test_missing_client_is_rejected(self):
        with pytest.raises(AnalysisWaitError, match="get_ce_task"):
            SonarAnalysisWaiter(None)

    @pytest.mark.parametrize("bad", [0, -1, "soon", None])
    def test_invalid_timeout_is_rejected(self, bad):
        with pytest.raises(AnalysisWaitError, match="timeout must be positive"):
            SonarAnalysisWaiter(FakeCeClient(), timeout_seconds=bad)

    @pytest.mark.parametrize("bad", [0, -5, "fast", None])
    def test_invalid_interval_is_rejected(self, bad):
        with pytest.raises(AnalysisWaitError, match="interval must be positive"):
            SonarAnalysisWaiter(FakeCeClient(), poll_interval_seconds=bad)

    @pytest.mark.parametrize("bad", [0, -1, 1.5, None])
    def test_invalid_api_error_tolerance_is_rejected(self, bad):
        with pytest.raises(AnalysisWaitError, match="max_consecutive_api_errors"):
            SonarAnalysisWaiter(FakeCeClient(), max_consecutive_api_errors=bad)

    @pytest.mark.parametrize("bad", [0, -2, "many"])
    def test_invalid_max_polls_is_rejected(self, bad):
        with pytest.raises(AnalysisWaitError, match="max_polls"):
            SonarAnalysisWaiter(FakeCeClient(), max_polls=bad)


class TestPollingBehaviourAndResultModel:
    def test_sleep_uses_the_configured_interval(self):
        clock = FakeClock()
        client = FakeCeClient(task("PENDING"), task("PENDING"), task("SUCCESS"))
        waiter(client, clock, poll_interval_seconds=2.5).wait(TASK_ID)
        assert clock.sleeps == [2.5, 2.5]

    def test_final_sleep_is_clamped_to_the_remaining_budget(self):
        clock = FakeClock()
        client = FakeCeClient(task("PENDING"))
        waiter(client, clock, timeout_seconds=7.0, poll_interval_seconds=5.0).wait(
            TASK_ID
        )
        assert clock.sleeps == [5.0, 2.0]

    def test_client_is_never_asked_to_trigger_anything_again(self):
        clock = FakeClock()
        client = FakeCeClient(task("PENDING"), task("SUCCESS"))
        result = waiter(client, clock).wait(TASK_ID)
        assert len(client.calls) == result.poll_count == 2

    def test_as_dict_is_secret_free_and_complete(self):
        clock = FakeClock()
        result = waiter(
            FakeCeClient(task("FAILED", errorMessage="tok-123 rejected")), clock
        ).wait(TASK_ID)
        payload = result.as_dict()
        assert payload["status"] == "failed"
        assert payload["task_id"] == TASK_ID
        assert payload["poll_count"] == 1
        assert payload["succeeded"] is False
        assert set(payload) >= {
            "status",
            "task_id",
            "elapsed_seconds",
            "poll_count",
            "analysis_id",
            "component_key",
            "succeeded",
            "reason",
            "failure_reason",
        }

    def test_completion_is_frozen(self):
        clock = FakeClock()
        result = waiter(FakeCeClient(task("SUCCESS")), clock).wait(TASK_ID)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.status = SonarAnalysisState.FAILED

    def test_success_reason_states_that_completion_is_not_a_fix(self):
        clock = FakeClock()
        result = waiter(FakeCeClient(task("SUCCESS")), clock).wait(TASK_ID)
        assert "does not mean the original issue is fixed" in result.reason
        assert result.failure_reason is None



class TestBudgetExhaustedMidPoll:
    def test_transient_api_error_with_expired_budget_times_out(self):
        clock = ScriptedClock(0, 0, 99)
        client = FakeCeClient(SonarQubeError("temporary outage"))
        result = SonarAnalysisWaiter(
            client, timeout_seconds=10.0, poll_interval_seconds=5.0,
            clock=clock, sleep=clock.sleep,
        ).wait(TASK_ID)
        assert result.status is SonarAnalysisState.TIMEOUT
        assert result.poll_count == 1
        assert clock.sleeps == []

    def test_pending_payload_with_expired_budget_times_out(self):
        clock = ScriptedClock(0, 0, 99)
        client = FakeCeClient(task("IN_PROGRESS"))
        result = SonarAnalysisWaiter(
            client, timeout_seconds=10.0, poll_interval_seconds=5.0,
            clock=clock, sleep=clock.sleep,
        ).wait(TASK_ID)
        assert result.status is SonarAnalysisState.TIMEOUT
        assert result.poll_count == 1
        assert clock.sleeps == []


class TestExternalTextHandling:
    def test_blank_engine_message_falls_back_to_a_generic_reason(self):
        clock = FakeClock()
        result = waiter(FakeCeClient(task("FAILED", errorMessage="   ")), clock).wait(
            TASK_ID
        )
        assert result.status is SonarAnalysisState.FAILED
        assert result.failure_reason == "The SonarQube compute-engine task failed."

    def test_blank_engine_message_on_cancel_falls_back(self):
        clock = FakeClock()
        result = waiter(FakeCeClient(task("CANCELED", errorMessage="")), clock).wait(
            TASK_ID
        )
        assert result.status is SonarAnalysisState.CANCELED
        assert "canceled" in result.failure_reason

    def test_bytes_engine_message_is_decoded(self):
        clock = FakeClock()
        result = waiter(
            FakeCeClient(task("FAILED", errorMessage=b"engine broke")), clock
        ).wait(TASK_ID)
        assert "engine broke" in result.failure_reason

