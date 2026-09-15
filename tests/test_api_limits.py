"""
Verify shard intake quotas across API replicas, failures and independent namespaces.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from polyad.api import APIBuilder, RateLimitPolicy
from polyad.compiler.composition import request_name
from polyad.operator.coordination import Coordinator, root_shard
from tests.test_composition_api import document
from tests.test_operator import FakeAPI

HEADERS = {"Authorization": "Bearer secret"}


def build(policy, submitted):
    """
    Create an independent app with a real shared-storage limiter and observable writes.
    """

    def submit(value):
        submitted.append(value.requestId)
        return {"requestId": value.requestId}

    return APIBuilder().with_handlers(submit, lambda *_: None).with_bearer_token("secret").with_rate_limits(policy).build()


def test_shard_identity_matches_scheduler():
    """
    Charge the same stable logical shard that will own the Composition family.
    """
    namespace, name = "test", request_name("request-one")
    assert root_shard("Composition", namespace, name) == asyncio.run(
        Coordinator(FakeAPI(), namespace).shard_for(("Composition", namespace, name))
    )


@pytest.mark.parametrize("failure", ["incr", "get_window_stats"])
def test_cache_failure_blocks_intake_and_authentication_runs_first(monkeypatch, failure):
    """
    Return 503 without submitting work or falling back to an unshared counter.
    """
    submitted = []
    app = build(RateLimitPolicy(namespace="test", storage_uri="redis://127.0.0.1:1"), submitted)

    def fail(*args, **kwargs):
        raise RedisConnectionError("secret URL must not leak")

    limiter = app.extensions["polyad.limiter"]
    monkeypatch.setattr(limiter.storage, "incr", fail if failure == "incr" else lambda *args, **kwargs: 1)
    if failure == "get_window_stats":
        monkeypatch.setattr(limiter.limiter, "get_window_stats", fail)
    client = app.test_client()
    assert client.post("/v1/compositions", json=document()).status_code == 401
    response = client.post("/v1/compositions", json=document(), headers=HEADERS)
    assert response.status_code == 503 and response.json["error"] == "rate-limit storage unavailable"
    assert "secret URL" not in response.text and not submitted
    assert client.get("/openapi.json", headers=HEADERS).status_code == 503
    app.extensions["polyad.limiter"].storage.storage.close()


def test_disabled_limits_and_policy_validation(monkeypatch):
    """
    Resolve environment settings and keep explicitly disabled APIs independent of Redis.
    """
    monkeypatch.setenv("POLYAD_CACHE_URL", "redis://127.0.0.1:1")
    monkeypatch.setenv("POLYAD_API_REQUESTS_PER_MINUTE", "2")
    monkeypatch.setenv("POLYAD_API_RATE_LIMIT_ENABLED", "false")
    policy = RateLimitPolicy.from_environment("test")
    assert policy.requests_per_minute == 2 and not policy.enabled
    app = build(policy, [])
    assert app.test_client().post("/v1/compositions", json=document(), headers=HEADERS).status_code == 202
    with pytest.raises(ValueError, match="shared"):
        RateLimitPolicy(namespace="test", storage_uri="memory://")
    with pytest.raises(ValueError, match="positive"):
        RateLimitPolicy(namespace="test", storage_uri="redis://localhost", requests_per_minute=0)


@pytest.mark.skipif(not os.environ.get("POLYAD_TEST_DRAGONFLY_URL"), reason="requires an isolated Dragonfly test endpoint")
def test_shared_atomic_quotas_and_independent_shards():
    """
    Enforce one quota across replicas and routes while isolating other shards and namespaces.
    """
    namespace = "rate-test-" + uuid.uuid4().hex
    url = os.environ["POLYAD_TEST_DRAGONFLY_URL"]
    policy = RateLimitPolicy(namespace=namespace, storage_uri=url, requests_per_minute=5)
    submitted = []
    apps = [build(policy, submitted), build(policy, submitted), build(RateLimitPolicy(namespace=namespace + "-other", storage_uri=url), [])]
    try:

        def send(index):
            with apps[index % 2].test_client() as client:
                return client.post("/v1/compositions", json=document(), headers=HEADERS)

        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(send, range(20)))
        assert sum(response.status_code == 202 for response in responses) == len(submitted) == 5
        rejected = [response for response in responses if response.status_code == 429]
        assert len(rejected) == 15
        assert all(int(response.headers["Retry-After"]) >= 0 for response in rejected)
        assert all(response.headers["X-RateLimit-Limit"] == "5" for response in responses)
        shard = root_shard("Composition", namespace, request_name("request-one"))
        same = next(f"same-{i}" for i in range(1000) if root_shard("Composition", namespace, request_name(f"same-{i}")) == shard)
        other = next(f"other-{i}" for i in range(1000) if root_shard("Composition", namespace, request_name(f"other-{i}")) != shard)
        client = apps[1].test_client()
        assert client.get(f"/v1/compositions/{same}", headers=HEADERS).status_code == 429
        assert client.get("/v1/compositions/request-one/resources", headers=HEADERS).status_code == 429
        assert client.get(f"/v1/compositions/{other}", headers=HEADERS).status_code == 404
        assert client.get("/v1/compositions/request-one").status_code == 401
        assert client.get("/openapi.json", headers=HEADERS).status_code == 200
        assert apps[2].test_client().get("/v1/compositions/request-one", headers=HEADERS).status_code == 404
    finally:
        client = apps[0].extensions["polyad.limiter"].storage.storage
        for key in client.scan_iter(match=f"*polyad:{namespace}*"):
            client.delete(key)
        for app in apps:
            app.extensions["polyad.limiter"].storage.storage.close()
