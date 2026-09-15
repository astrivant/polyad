"""
Verify log configuration precedence and keep payloads out of debug diagnostics.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.operator import runtime
from polyad.operator.api import API


@pytest.mark.parametrize(
    "environment,arguments,level",
    [("INFO", [], logging.INFO), ("debug", [], logging.DEBUG), ("DEBUG", ["--log-level", "WARNING"], logging.WARNING)],
)
def test_log_level_precedence_does_not_enable_transport_logging(monkeypatch, caplog, environment, arguments, level):
    """
    Scope verbose logging to Polyad and let explicit CLI settings override the environment.
    """
    monkeypatch.setenv("POLYAD_LOG_LEVEL", environment)
    monkeypatch.setattr("sys.argv", ["polyad-operator", *arguments])
    monkeypatch.setattr(runtime.signal, "signal", Mock())
    fake = SimpleNamespace(start=Mock(), join=Mock(), stop=Mock(), thread=SimpleNamespace(is_alive=lambda: False))
    monkeypatch.setattr(runtime, "OperatorThread", Mock(return_value=fake))
    transport_level = logging.getLogger("urllib3").getEffectiveLevel()
    with caplog.at_level(logging.INFO, logger="polyad"):
        runtime.main()
        assert logging.getLogger("polyad.operator.api").getEffectiveLevel() == level
        assert logging.getLogger("urllib3").getEffectiveLevel() == transport_level
    fake.start.assert_called_once()


def test_invalid_environment_level_fails_before_starting_operator(monkeypatch):
    """
    Reject misspelled logging settings rather than silently losing diagnostics.
    """
    monkeypatch.setenv("POLYAD_LOG_LEVEL", "DEBIG")
    monkeypatch.setattr("sys.argv", ["polyad-operator"])
    constructor = Mock()
    monkeypatch.setattr(runtime, "OperatorThread", constructor)
    with pytest.raises(SystemExit) as error:
        runtime.main()
    assert error.value.code == 2
    constructor.assert_not_called()


@pytest.mark.parametrize("failure", [None, ApiException(status=409, reason="private-payload"), TimeoutError("private-payload")])
def test_api_debug_logs_do_not_include_payloads_or_exception_bodies(caplog, failure):
    """
    Retain request identity and timing while preserving results, errors and payload confidentiality.
    """
    api = API.__new__(API)
    api.client = Mock()
    api.client.call_api.return_value = {"data": "private-payload"}
    api.client.call_api.side_effect = failure

    async def scenario():
        operation = api.request("PATCH", "ConfigMap", "test", "config", {"data": {"token": "private-payload"}})
        if failure is None:
            assert await operation == {"data": "private-payload"}
        else:
            with pytest.raises(type(failure)) as error:
                await operation
            assert error.value is failure

    with caplog.at_level(logging.DEBUG, logger="polyad"):
        asyncio.run(scenario())
    assert "private-payload" not in caplog.text
    assert "method=PATCH kind=ConfigMap namespace=test name=config" in caplog.text
    assert "elapsed_seconds=" in caplog.text
    assert api.writes.snapshot()["total"] == 0
