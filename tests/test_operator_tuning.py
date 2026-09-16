"""
Verify runtime polling controls retain freshness bounds and reach the worker loops.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from polyad.operator.tuning import OperatorTuning


def test_environment_settings_and_defaults(monkeypatch):
    """
    Apply independently configurable periods and reject invalid local settings.
    """
    for name in ("RESCAN", "CONSUME", "METRICS", "BACKLOG"):
        monkeypatch.delenv(f"POLYAD_{name}_INTERVAL_SECONDS", raising=False)
    assert OperatorTuning.from_environment() == OperatorTuning()
    monkeypatch.setenv("POLYAD_CONSUME_INTERVAL_SECONDS", "0.25")
    monkeypatch.setenv("POLYAD_RESCAN_INTERVAL_SECONDS", "12")
    assert OperatorTuning.from_environment() == OperatorTuning(consume=0.25, rescan=12)
    for invalid in ("nan", "inf", "-1", "0", "6", "invalid"):
        monkeypatch.setenv("POLYAD_CONSUME_INTERVAL_SECONDS", invalid)
        with pytest.raises(ValueError):
            OperatorTuning.from_environment()


@pytest.mark.parametrize("settings", [{"rescan": 16}, {"metrics": 6}, {"backlog": 0}, {"consume": True}])
def test_tuning_rejects_invalid_intervals(settings):
    """
    Enforce the same bounds for direct Python callers as Helm configuration.
    """
    with pytest.raises(ValueError):
        OperatorTuning(**settings)


@pytest.mark.parametrize("name", ["rescan", "consume", "metrics", "backlog"])
@pytest.mark.parametrize("database_outage", [False, True])
def test_worker_uses_configured_pause(monkeypatch, name, database_outage):
    """
    Complete one worker pass and observe its actual scheduled pause.
    """
    from polyad.operator import handlers

    async def scenario():
        tuning = OperatorTuning(rescan=12, consume=0.25, metrics=2, backlog=3)
        monkeypatch.setattr(handlers, "tuning", tuning)
        monkeypatch.setattr(
            handlers,
            "coordinator",
            SimpleNamespace(
                api=SimpleNamespace(request=AsyncMock(return_value={"items": []})),
                namespace="test",
                owned=set(),
                identity="replica",
                leader=False,
            ),
        )
        monkeypatch.setattr(handlers, "queue", SimpleNamespace(queue=asyncio.Queue()))
        monkeypatch.setattr(handlers, "metrics_http", None)
        monkeypatch.setattr(handlers, "metrics_store", SimpleNamespace(publish=Mock()))
        monkeypatch.setattr(handlers, "inventory_sample", None)
        monkeypatch.setattr(
            handlers,
            "state",
            SimpleNamespace(begin=AsyncMock(side_effect=OSError("database unavailable"))) if database_outage and name == "rescan" else None,
        )
        monkeypatch.setattr(handlers, "write_backlog", Mock(return_value={}))
        monkeypatch.setattr(handlers, "collect", AsyncMock(return_value={"fresh": False, "roles": {}}))
        monkeypatch.setattr(
            handlers,
            "shared",
            SimpleNamespace(ping=AsyncMock(), sample_backlog=AsyncMock(), backlog=Mock(return_value={}), backlog_sample=None),
        )
        sleep = AsyncMock(side_effect=asyncio.CancelledError)
        monkeypatch.setattr(handlers.asyncio, "sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await getattr(handlers, f"{name}_loop")()
        sleep.assert_awaited_once_with(getattr(tuning, name))
        if name == "rescan":
            assert handlers.inventory_sample_ok
            assert handlers.inventory_sample[1]["total"] == 0

    asyncio.run(scenario())
