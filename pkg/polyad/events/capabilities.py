"""
Share bounded, expiring replica contracts without granting labels discovery authority.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, cast

from attrs import evolve
from redis.exceptions import RedisError

from polyad.api.connections.consent import endpoint
from polyad.api.connections.paths import path
from polyad.api.connections.store import Caller, ConnectionSettings, ConnectionStore
from polyad.exceptions.api import Conflict, Forbidden, Unavailable
from polyad.lua import script
from polyad_types.api.discovery import ServiceEndpoint
from polyad_types.serialization import converter, to_dict

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from typing import Any

    from polyad.cache import Cache
    from polyad.operator.adapters.kubernetes import API
    from polyad_types.api.capabilities import CapabilityAdvertisement

__all__ = ("AdvertisementStore",)

_CONTRACTS = script("events/capabilities.lua")


def _receipt(peer: ServiceEndpoint) -> dict[str, Any]:
    # Reuse the existing Pod-to-graph ownership verifier, with one exact node
    # on both sides. This does not create a temporary connection or any consent.
    return {
        "spec": {
            "namespace": peer.namespace,
            "kind": peer.kind,
            "graph": peer.graph,
            "graphUid": peer.graphUid,
            "source": peer.node,
            "target": peer.node,
        }
    }


async def _ready(api: API, caller: Caller) -> bool:
    pod = await api.get("Pod", caller.namespace, caller.extra["authentication.kubernetes.io/pod-name"][0])
    return bool(
        pod
        and pod["metadata"]["uid"] == caller.extra["authentication.kubernetes.io/pod-uid"][0]
        and not pod["metadata"].get("deletionTimestamp")
        and pod.get("status", {}).get("phase") == "Running"
        and any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod.get("status", {}).get("conditions", []))
    )


class AdvertisementStore:
    """
    Store at most 128 contracts per graph, with one replaceable record per Pod UID.
    """

    def __init__(self, cache: Cache) -> None:
        """
        Reuse the discovery authority's shared, namespaced cache.

        Args:
            cache (Cache): Redis-compatible cache owned and closed by the HTTP server.
        """
        self.cache = cache

    async def _execute(self, identity: dict[str, Any], command: str, uid: str = "", payload: str = "", ttl: int = 30) -> Any:
        digest = hashlib.sha256(
            json.dumps([identity.get("cluster", ""), identity["kind"], identity["namespace"], identity["uid"]]).encode()
        ).hexdigest()
        try:
            raw = await cast(
                "Awaitable[str]",
                self.cache.client.eval(_CONTRACTS, 1, self.cache.prefix + "capabilities:v1:" + digest, command, uid, payload, str(ttl)),
            )
            return json.loads(raw)
        except RedisError as error:
            raise Unavailable("capability registry is unavailable or this graph has reached its 128-contract limit") from error

    async def publish(self, advertisement: CapabilityAdvertisement, caller: Caller, store: ConnectionStore) -> dict[str, Any]:
        """
        Verify publication authority and replace or withdraw this Pod's complete contract.

        Args:
            advertisement (CapabilityAdvertisement): Bounded sharing policy with no caller-controlled timestamps.
            caller (Caller): TokenReview-authenticated Pod and service-account identity.
            store (ConnectionStore): Existing cluster resolver, namespace limits and Kubernetes authorization.

        Returns:
            dict[str, Any]: Server-timed contract, or a withdrawal acknowledgement.
        """
        peer = evolve(advertisement.endpoint, cluster=advertisement.endpoint.cluster or store.federation.name)
        if peer.cluster != (caller.cluster or store.federation.name) or peer.namespace != caller.namespace:
            raise Forbidden("a provider may advertise only its own cluster and namespace")
        chain = await path(peer, store.resolve, store.federation.name)
        api, _, _, _ = chain[0]
        authority = ConnectionStore(api, store.settings)
        await authority.authorize(caller, peer.namespace, peer.kind, peer.graph, verb="advertise")
        await endpoint(api, caller, _receipt(peer))

        # Kubernetes supplies the replica identity. Never accept a Pod UID from
        # the payload, or sibling replicas could overwrite each other's budgets.
        pod_name = caller.extra["authentication.kubernetes.io/pod-name"][0]
        pod_uid = caller.extra["authentication.kubernetes.io/pod-uid"][0]
        identity = {"cluster": peer.cluster, "namespace": peer.namespace, "kind": peer.kind, "uid": peer.graphUid}
        if not advertisement.capabilities:
            await self._execute(identity, "withdraw", pod_uid)
            return {"withdrawn": True, "podUid": pod_uid}
        if not await _ready(api, caller):
            raise Conflict("only Running, Ready Pods can offer capacity")
        contract = {"advertisement": to_dict(evolve(advertisement, endpoint=peer)), "podName": pod_name, "podUid": pod_uid}
        value = {
            "contract": contract,
            "caller": {
                "username": caller.username,
                "uid": caller.uid,
                "namespace": caller.namespace,
                "groups": list(caller.groups),
                "extra": {
                    name: caller.extra[name] for name in ("authentication.kubernetes.io/pod-name", "authentication.kubernetes.io/pod-uid")
                },
                "cluster": caller.cluster,
            },
        }
        encoded = json.dumps(value, allow_nan=False)
        if len(encoded.encode()) > 16384:
            raise ValueError("capability contract and verified identity exceed 16 KiB")
        result = await self._execute(identity, "publish", pod_uid, encoded, advertisement.ttlSeconds)
        return {**contract, "observedAt": result["observedAt"], "expiresAt": result["expiresAt"]}

    async def contracts(self, identity: dict[str, Any], api: API) -> list[dict[str, Any]]:
        """
        Read fresh contracts only after the directory has authorized this graph.

        Args:
            identity (dict[str, Any]): Current, authorized graph identity including cluster and UID.
            api (API): Reader in that graph's registered cluster.

        Returns:
            list[dict[str, Any]]: Public contracts for still-live, ready, authorized replica owners.
        """
        started = time.monotonic()
        snapshot = await self._execute(identity, "read")
        result = []
        authority = ConnectionStore(api, ConnectionSettings(identity["namespace"], scope="OperatorNamespace"))
        for raw in snapshot["entries"]:
            lease = json.loads(raw)
            value = json.loads(lease["payload"])
            contract = {**value["contract"], "observedAt": lease["observedAt"], "expiresAt": lease["expiresAt"]}
            peer = converter.structure(contract["advertisement"]["endpoint"], ServiceEndpoint)
            caller = Caller(**value["caller"])
            try:
                await authority.authorize(caller, peer.namespace, peer.kind, peer.graph, verb="advertise")
                await endpoint(api, caller, _receipt(peer))
                if await _ready(api, caller):
                    result.append(contract)
            except Forbidden:
                continue

        # Live authorization can take time. Conservatively include all elapsed
        # read time so a contract that expires during these checks is not returned.
        now = snapshot["observedAt"] + time.monotonic() - started
        return sorted((item for item in result if item["expiresAt"] > now), key=lambda item: item["podUid"])
