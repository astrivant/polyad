"""
Exercise TTL sharing contracts, replica ownership, scoped discovery and SDK helpers.
"""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from attrs import evolve
from cattrs.errors import CattrsError
from lupa.lua54 import LuaRuntime, lua_type
from openapi_spec_validator import validate
from redis.exceptions import ResponseError

from polyad.api.connections.app import build_app
from polyad.api.connections.store import ConnectionSettings, ConnectionStore
from polyad.events.capabilities import AdvertisementStore
from polyad.events.discovery import Directory
from polyad.exceptions.api import Conflict, Forbidden, Unavailable
from polyad_sdk import AdaptiveService, Client, ContainerMetrics, ContainerResources, VPAConstraints, WorkloadContext
from polyad_sdk.capabilities import (
    CapabilityAdvertisement,
    CapabilityContract,
    CapabilityOffer,
    ResourceAvailability,
    resource_availability,
)
from polyad_sdk.exceptions.api import APIError
from polyad_types import APIKey, GraphAccess, KeyDirection, ServiceEndpoint, from_dict, to_dict
from tests.test_atlas import setup_atlas
from tests.test_client import Adapter
from tests.test_temporary_connections import ConnectionAPI, graph_fixture, participant


class LuaRedis:
    """
    Execute the actual packaged Lua script with deterministic Redis and cjson adapters.
    """

    def __init__(self):
        """
        Own isolated hash state and an explicitly advanced server clock.
        """
        self.now, self.hashes, self.expiry = 1000.0, {}, {}
        self.runtime = LuaRuntime(unpack_returned_tuples=True)
        self.null = object()
        self.runtime.globals().redis = self.runtime.table_from({"call": self.call, "error_reply": self.error})
        self.runtime.globals().cjson = self.runtime.table_from({"encode": self.encode, "decode": self.decode})

    def decode(self, encoded):
        """
        Preserve explicit JSON null values while building Lua tables.
        """

        def lower(value):
            if value is None:
                return self.null
            if isinstance(value, dict):
                return self.runtime.table_from({key: lower(item) for key, item in value.items()})
            if isinstance(value, list):
                return self.runtime.table_from([lower(item) for item in value])
            return value

        return lower(json.loads(encoded))

    def encode(self, value):
        """
        Match cjson object/array encoding, including empty tables as objects.
        """

        def lift(item):
            if item is self.null:
                return None
            if lua_type(item) == "table":
                keys = list(item.keys())
                if keys and set(keys) == set(range(1, len(keys) + 1)):
                    return [lift(item[index]) for index in range(1, len(keys) + 1)]
                return {key: lift(entry) for key, entry in item.items()}
            return item

        return json.dumps(lift(value), allow_nan=False)

    def error(self, message):
        """
        Raise the same error class emitted by a Redis error reply.
        """
        raise ResponseError(message)

    def call(self, command, *args):
        """
        Emulate only the Redis commands used by the real atomic registry script.
        """
        if command == "TIME":
            return self.runtime.table_from([str(int(self.now)), str(round((self.now % 1) * 1000000))])
        key = args[0]
        if self.expiry.get(key, float("inf")) <= self.now:
            self.hashes.pop(key, None)
        values = self.hashes.setdefault(key, {})
        if command == "HGETALL":
            return self.runtime.table_from([item for pair in values.items() for item in pair])
        if command == "HVALS":
            return self.runtime.table_from(list(values.values()))
        if command == "HDEL":
            return int(values.pop(args[1], None) is not None)
        if command == "HEXISTS":
            return int(args[1] in values)
        if command == "HLEN":
            return len(values)
        if command == "HSET":
            values[args[1]] = args[2]
            return 1
        if command == "EXPIRE":
            self.expiry[key] = self.now + int(args[1])
            return 1
        raise AssertionError(command)

    async def eval(self, code, count, key, *args):
        """
        Execute one complete script without duplicating the production lease logic.
        """
        assert count == 1
        self.runtime.globals().KEYS = self.runtime.table_from([key])
        self.runtime.globals().ARGV = self.runtime.table_from(list(args))
        return self.runtime.execute(code)


def setup(monkeypatch):
    """
    Create a real graph, Ready Pod identity and a registry backed by the Lua adapter.
    """
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "local")
    graph, definitions = graph_fixture()
    api = ConnectionAPI(graph, *definitions)
    caller = participant(api, graph, "a")
    pod = api.objects["Pod", "test", caller.extra["authentication.kubernetes.io/pod-name"][0]]
    pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
    redis = LuaRedis()
    registry = AdvertisementStore(SimpleNamespace(client=redis, prefix="test:"))
    store = ConnectionStore(api, ConnectionSettings("test"))
    peer = ServiceEndpoint("local", "test", "Graph", "root", graph["metadata"]["uid"], "a")
    advertisement = CapabilityAdvertisement(peer, (CapabilityOffer("resize", "images", 20, 12, 6, 4),), {"team": "media"})
    identity = {"cluster": "local", "kind": "Graph", "namespace": "test", "name": "root", "uid": peer.graphUid}
    return registry, redis, store, api, caller, advertisement, identity


def test_capacity_is_the_minimum_of_ability_and_willingness():
    """
    Keep rate and concurrency caps distinct from utilization and allocated resources.
    """
    offer = CapabilityOffer("resize", "images", 20, 12, 2, 8)
    assert offer.offered_per_second == 12 and offer.offered_concurrency == 2
    assert evolve(offer, availablePerSecond=3).offered_per_second == 3
    assert CapabilityOffer("resize", "images", 0, 10).offered_per_second == 0
    assert CapabilityOffer("resize", "images", 20, 10).offered_concurrency is None


@pytest.mark.parametrize("value", [True, "20", -1, float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["availablePerSecond", "sharePerSecond"])
def test_wire_rates_are_not_coerced(value, field):
    """
    Reject unsafe input in both direct and decoded public models.
    """
    values = {"name": "resize", "unit": "images", "availablePerSecond": 20, "sharePerSecond": 10, field: value}
    with pytest.raises(ValueError):
        CapabilityOffer(**values)
    with pytest.raises(ValueError):
        from_dict(values, CapabilityOffer)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ttlSeconds", True),
        ("ttlSeconds", "30"),
        ("ttlSeconds", 301),
        ("ttlSeconds", 4),
        ("labels", {"team": 1}),
        ("labels", {"": "x"}),
        ("labels", {"x": "a\nb"}),
        ("podUid", "invented"),
        ("expiresAt", 9999999999),
    ],
)
def test_advertisement_wire_contract_is_closed(monkeypatch, field, value):
    """
    Prevent timestamp/identity injection, excessive lifetimes and ambiguous selectors.
    """
    advertisement = setup(monkeypatch)[5]
    with pytest.raises((ValueError, TypeError, CattrsError)):
        from_dict({**to_dict(advertisement), field: value}, CapabilityAdvertisement)


def test_nested_contract_types_reject_coercion_and_duplicates(monkeypatch):
    """
    Validate nested endpoints, resource readings and capability identities strictly.
    """
    advertisement = setup(monkeypatch)[5]
    raw = to_dict(advertisement)
    raw["endpoint"]["graphUid"] = 123
    with pytest.raises(ValueError):
        from_dict(raw, CapabilityAdvertisement)
    with pytest.raises(ValueError):
        evolve(advertisement, capabilities=advertisement.capabilities * 2)
    for value in (True, "10", -1, 1.5):
        with pytest.raises(ValueError):
            from_dict({"memoryLimitBytes": value}, ResourceAvailability)
    with pytest.raises(ValueError):
        CapabilityOffer("resize", "images", 10, 10, shareConcurrency=4)
    assert from_dict(to_dict(advertisement), CapabilityAdvertisement) == advertisement


def test_refresh_replaces_contract_and_server_controls_expiry(monkeypatch):
    """
    Renew a single Pod atomically without accumulating duplicate or removed capabilities.
    """
    registry, redis, store, api, caller, advertisement, identity = setup(monkeypatch)

    async def run():
        first = await registry.publish(advertisement, caller, store)
        assert first["observedAt"] == 1000 and first["expiresAt"] == 1030
        assert first["podUid"] == caller.extra["authentication.kubernetes.io/pod-uid"][0]
        assert "caller" not in json.dumps(first) and "authentication.kubernetes" not in json.dumps(first)
        redis.now = 1020
        replacement = evolve(advertisement, capabilities=(CapabilityOffer("decode", "frames", 30, 15),), ttlSeconds=10)
        second = await registry.publish(replacement, caller, store)
        assert second["expiresAt"] == 1030
        contracts = await registry.contracts(identity, api)
        assert len(contracts) == 1 and contracts[0]["advertisement"]["capabilities"][0]["name"] == "decode"
        assert {item["resourceAttributes"]["verb"] for item in api.reviews} == {"advertise"}
        redis.now = 1030
        assert await registry.contracts(identity, api) == []

    asyncio.run(run())


def test_replica_withdrawal_and_refresh_do_not_extend_siblings(monkeypatch):
    """
    Distinguish replicas of the same logical service and give each an independent lease.
    """
    registry, redis, store, api, caller, advertisement, identity = setup(monkeypatch)
    original = api.objects["Pod", "test", caller.extra["authentication.kubernetes.io/pod-name"][0]]
    sibling = copy.deepcopy(original)
    sibling["metadata"].update(name="sibling", uid="sibling-uid")
    api.objects["Pod", "test", "sibling"] = sibling
    extra = {"authentication.kubernetes.io/pod-name": ["sibling"], "authentication.kubernetes.io/pod-uid": ["sibling-uid"]}
    other = evolve(caller, extra=extra)

    async def run():
        await registry.publish(advertisement, caller, store)
        redis.now = 1020
        await registry.publish(advertisement, other, store)
        assert len(await registry.contracts(identity, api)) == 2
        redis.now = 1030
        assert [item["podUid"] for item in await registry.contracts(identity, api)] == ["sibling-uid"]
        assert (await registry.publish(evolve(advertisement, capabilities=()), other, store))["withdrawn"]
        assert await registry.contracts(identity, api) == []
        assert (await registry.publish(evolve(advertisement, capabilities=()), other, store))["withdrawn"]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["permission", "pod-uid", "pod-ready", "pod-stopped", "pod-deleted", "account", "graph-uid", "node"])
def test_live_fences_hide_unusable_contracts(monkeypatch, change):
    """
    Hide stale offers before TTL expiry when identity, readiness or publication authority changes.
    """
    registry, _, store, api, caller, advertisement, identity = setup(monkeypatch)

    async def run():
        await registry.publish(advertisement, caller, store)
        pod = api.objects["Pod", "test", caller.extra["authentication.kubernetes.io/pod-name"][0]]
        if change == "permission":
            api.allowed = False
        elif change == "pod-uid":
            pod["metadata"]["uid"] = "replaced"
        elif change == "pod-ready":
            pod["status"]["conditions"] = []
        elif change == "pod-stopped":
            pod["status"]["phase"] = "Succeeded"
        elif change == "pod-deleted":
            pod["metadata"]["deletionTimestamp"] = "now"
        elif change == "account":
            api.objects["ServiceAccount", "test", caller.username.rsplit(":", 1)[-1]]["metadata"]["uid"] = "replaced"
        elif change == "graph-uid":
            api.objects["Graph", "test", "root"]["metadata"]["uid"] = "replaced"
        else:
            pod["metadata"]["labels"]["polyad.astrivant.com/node"] = "b"
        assert await registry.contracts(identity, api) == []
        with pytest.raises((Forbidden, Conflict)):
            await registry.publish(advertisement, caller, store)

    asyncio.run(run())


def test_publication_requires_bound_pod_and_own_service(monkeypatch):
    """
    Reject broad account tokens and attempts to advertise another cluster or node.
    """
    registry, _, store, _, caller, advertisement, _ = setup(monkeypatch)

    async def run():
        with pytest.raises(Forbidden):
            await registry.publish(advertisement, evolve(caller, extra={}), store)
        for changes in ({"cluster": "other"}, {"namespace": "other"}, {"node": "b"}):
            with pytest.raises(Forbidden):
                await registry.publish(evolve(advertisement, endpoint=evolve(advertisement.endpoint, **changes)), caller, store)

    asyncio.run(run())


def test_atomic_registry_bounds_and_reclaims_expired_entries(monkeypatch):
    """
    Bound a graph's hash while allowing replacements at capacity and reclaiming expired records.
    """
    registry, redis, _, _, _, advertisement, identity = setup(monkeypatch)

    async def run():
        payload = json.dumps({"contract": {"advertisement": to_dict(advertisement), "podName": "pod", "podUid": "uid"}})
        for index in range(128):
            await registry._execute(identity, "publish", str(index), payload, 5)
        await registry._execute(identity, "publish", "0", payload, 30)
        with pytest.raises(Unavailable):
            await registry._execute(identity, "publish", "overflow", payload, 30)
        redis.now = 1005
        await registry._execute(identity, "publish", "overflow", payload, 30)
        assert len((await registry._execute(identity, "read"))["entries"]) == 2

    asyncio.run(run())


def test_directory_requires_graph_grants_before_reading_contracts(monkeypatch):
    """
    Labels cannot bypass existing graph discovery policy, and denied reads do not hit the registry.
    """
    registry, _, store, api, caller, advertisement, _ = setup(monkeypatch)
    directory = Directory(
        api, "test", store.federation, {"local": SimpleNamespace(cursor=AsyncMock(return_value="1-0"))}, advertisements=registry
    )
    target = GraphAccess("root", "test", cluster="local")
    key = APIKey("reader", KeyDirection.INBOUND, "reader-secret", endpoints=("discovery",), home=target, graphs=(target,))

    async def run():
        await registry.publish(advertisement, caller, store)
        result = await directory.discover(key, target)
        assert len(result["services"][0]["contracts"]) == 1
        assert result["services"][1]["contracts"] == []
        registry.contracts = AsyncMock(wraps=registry.contracts)
        with pytest.raises(Forbidden):
            await directory.discover(evolve(key, graphs=()), target)
        registry.contracts.assert_not_awaited()
        api.objects["Graph", "test", "root"]["spec"]["suspend"] = True
        result = await directory.discover(key, target)
        assert all(not item["contracts"] for item in result["services"])

    asyncio.run(run())


def test_registered_remote_publisher_is_visible_only_at_its_selected_authority(monkeypatch):
    """
    Verify remote Pod ownership without merging independent root and leaf registries.
    """
    store, controller, request, apis, _, callers = setup_atlas(monkeypatch)
    caller = callers["west"]
    pod = apis["west"].objects["Pod", "test", caller.extra["authentication.kubernetes.io/pod-name"][0]]
    pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
    registry = AdvertisementStore(SimpleNamespace(client=LuaRedis(), prefix="root:"))
    streams = {name: SimpleNamespace(cursor=AsyncMock(return_value="1-0")) for name in ("management", "west", "east")}
    directory = Directory(controller.api, "test", store.federation, streams, advertisements=registry)
    target = GraphAccess("west", "test", cluster="west", uid=request.source.graphUid)
    key = APIKey("reader", KeyDirection.INBOUND, "key", endpoints=("discovery",), home=target, graphs=(target,))

    async def run():
        await registry.publish(CapabilityAdvertisement(request.source, (CapabilityOffer("resize", "images", 20, 10),)), caller, store)
        result = await directory.discover(key, target)
        assert result["services"][0]["contracts"][0]["advertisement"]["endpoint"]["cluster"] == "west"
        independent = AdvertisementStore(SimpleNamespace(client=LuaRedis(), prefix="leaf:"))
        assert await independent.contracts(result["graph"], apis["west"]) == []

    asyncio.run(run())


def test_http_client_publication_withdrawal_and_openapi(monkeypatch):
    """
    Round-trip the SDK through authenticated HTTP routes and validate their OpenAPI document.
    """
    registry, _, store, _, caller, advertisement, _ = setup(monkeypatch)
    app = build_app(
        lambda token: caller,
        lambda *args: {},
        lambda *args: {},
        lambda *args: {},
        advertise=lambda value, identity: asyncio.run(registry.publish(value, identity, store)),
    )
    client = Client("http://connections", "token")
    client._opener = Adapter(app)
    contract = client.advertise_capabilities(advertisement)
    assert isinstance(contract, CapabilityContract) and contract.advertisement == advertisement
    assert client.withdraw_capabilities(advertisement.endpoint)["withdrawn"]
    assert app.test_client().post("/v1/capabilities", json=to_dict(advertisement)).status_code == 401
    invalid = {**to_dict(advertisement), "ttlSeconds": "30"}
    assert app.test_client().post("/v1/capabilities", json=invalid, headers={"Authorization": "Bearer token"}).status_code == 400
    client._token = None
    with pytest.raises(APIError) as error:
        client.advertise_capabilities(advertisement)
    assert error.value.status == 401
    schema = app.test_client().get("/openapi.json", headers={"Authorization": "Bearer token"}).json
    validate(schema)
    assert "/v1/capabilities" in schema["paths"]


@pytest.mark.parametrize("change", [{}, {"capabilities": {}}, {"capabilities": ""}, {"capabilities": [1]}, {"resources": []}])
def test_http_rejects_missing_or_wrong_shaped_objects_without_publication(monkeypatch, change):
    """
    Malformed nested JSON must neither withdraw a contract nor cause an HTTP 500.
    """
    _, _, _, _, caller, advertisement, _ = setup(monkeypatch)
    calls = []
    app = build_app(lambda token: caller, lambda *args: {}, lambda *args: {}, lambda *args: {}, advertise=lambda *args: calls.append(args))
    value = {**to_dict(advertisement), **change} if change else {}
    response = app.test_client().post("/v1/capabilities", json=value, headers={"Authorization": "Bearer token"})
    assert response.status_code == 400 and calls == []


def test_registry_preserves_resource_integers_and_fractional_capacity(monkeypatch):
    """
    Keep declared JSON opaque inside Lua so cjson precision cannot change budgets.
    """
    registry, _, store, api, caller, advertisement, identity = setup(monkeypatch)
    precise = 1.2345678901234567
    advertisement = evolve(
        advertisement,
        capabilities=(CapabilityOffer("resize", "images", precise, precise),),
        resources=ResourceAvailability(cpuUsageUsec=2**53 - 1, memoryLimitBytes=2**53 - 1),
    )

    async def run():
        published = await registry.publish(advertisement, caller, store)
        snapshot = await registry._execute(identity, "read")
        lease = json.loads(snapshot["entries"][0])
        assert isinstance(lease["payload"], str)
        assert (await registry.contracts(identity, api))[0]["advertisement"] == published["advertisement"] == to_dict(advertisement)
        assert from_dict(published, CapabilityContract).advertisement.resources.cpuUsageUsec == 2**53 - 1

    asyncio.run(run())


def test_contracts_expiring_during_live_checks_are_not_returned(monkeypatch):
    """
    Account conservatively for authorization latency after Redis checked each expiry.
    """
    registry, _, store, api, caller, advertisement, identity = setup(monkeypatch)

    async def run():
        await registry.publish(advertisement, caller, store)
        readings = iter((0, 31))
        monkeypatch.setattr("polyad.events.capabilities.time", SimpleNamespace(monotonic=lambda: next(readings)))
        assert await registry.contracts(identity, api) == []

    asyncio.run(run())


def test_sdk_matching_expiry_zero_offers_and_shared_resources(monkeypatch):
    """
    Match all selected labels and capabilities, rechecking expiry at each iteration.
    """
    advertisement = setup(monkeypatch)[5]
    second = CapabilityOffer("decode", "frames", 100, 20)
    full = evolve(advertisement, capabilities=(*advertisement.capabilities, second))
    empty = evolve(advertisement, capabilities=(CapabilityOffer("resize", "images", 10, 0),))
    blocked = evolve(advertisement, capabilities=(CapabilityOffer("resize", "images", 10, 5, 0, 4),))
    contracts = [CapabilityContract(item, "pod", str(index), 1000, 1030) for index, item in enumerate((full, empty, blocked))]
    client = Client("http://events", "reader")
    monkeypatch.setattr(client, "services", lambda **kwargs: iter([{"contracts": [to_dict(item) for item in contracts]}]))
    monkeypatch.setattr("polyad_sdk.api.client.time.time", lambda: 1001)
    assert list(client.offers(capabilities=("resize", "decode"), labels={"team": "media"})) == [contracts[0]]
    assert list(client.offers(labels={"team": "other"})) == []
    assert list(client.offers(capabilities=("missing",))) == []
    assert len(list(client.offers(available_only=False))) == 3
    iterator = client.offers(available_only=False)
    assert next(iterator) == contracts[0]
    monkeypatch.setattr("polyad_sdk.api.client.time.time", lambda: 1030)
    assert list(iterator) == []
    with pytest.raises(ValueError):
        list(client.offers(capabilities="resize"))


def test_runtime_resource_projection_tracks_resize_without_fabricating_capacity(monkeypatch):
    """
    Keep actual cgroup readings separate from startup requests and VPA policy bounds.
    """
    peer = setup(monkeypatch)[5].endpoint
    context = WorkloadContext(
        peer,
        resources=ContainerResources(cpu_request_millicores=100, memory_limit_bytes=1024),
        vpa=VPAConstraints(min_cpu_millicores=100, max_cpu_millicores=1000),
    )
    before = resource_availability(
        context, metrics=ContainerMetrics(cpu_limit_millicores=250, memory_limit_bytes=2048, memory_usage_bytes=512)
    )
    after = resource_availability(
        context, metrics=ContainerMetrics(cpu_limit_millicores=500, memory_limit_bytes=4096, memory_usage_bytes=1024)
    )
    assert (before.cpuLimitMillicores, after.cpuLimitMillicores) == (250, 500)
    assert before.memory_available_bytes == 1536 and after.memory_available_bytes == 3072
    assert before.cpuRequestMillicores == after.cpuRequestMillicores == 100
    assert after.vpaMaxCpuMillicores == 1000
    unknown = resource_availability(context, metrics=ContainerMetrics())
    assert unknown.cpuLimitMillicores is None and unknown.memory_available_bytes is None


def test_adaptive_service_publishes_only_when_explicitly_called(monkeypatch):
    """
    Bind application-level convenience methods to the exact service and no background heartbeat.
    """
    advertisement = setup(monkeypatch)[5]

    class Service(AdaptiveService):
        """
        Provide an inert application adaptation hook for this focused SDK test.
        """

        def adapt(self, change):
            """
            Keep all advertisement decisions explicit in the test.
            """

    client = Client("http://connections", "token")
    seen = []
    monkeypatch.setattr(client, "advertise_capabilities", lambda value: seen.append(value))
    monkeypatch.setattr(client, "withdraw_capabilities", lambda value: {"endpoint": value})
    service = Service(advertisement.endpoint, Client("http://events", "reader"), connections=client, require_strategies=False)
    assert seen == []
    service.advertise_capabilities(advertisement.capabilities, labels={"team": "media"}, ttl_seconds=15)
    assert seen[0].endpoint == advertisement.endpoint and seen[0].resources is None and seen[0].ttlSeconds == 15
    assert service.withdraw_capabilities()["endpoint"] == advertisement.endpoint
    assert seen[0].labels == {"team": "media"}
