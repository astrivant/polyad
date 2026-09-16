"""
Elect a planner and lease graph shards to replicas using Kubernetes CAS updates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kubernetes.client.exceptions import ApiException

from polyad.operator.api import GROUP
from polyad.operator.decisions import decision, decision_context
from polyad.operator.roles import role
from polyad_types.resources import Lease, LeaseSpec, ObjectMeta

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Any

    from polyad.operator.api import API
    from polyad.operator.queue import Key

SHARDS = 32
DURATION = 90
WRITE_BUDGET = 35
active_shard: ContextVar[int | None] = ContextVar("polyad_shard", default=None)


def root_shard(kind: str, namespace: str, name: str) -> int:
    """
    Map a root identity to its stable logical shard independently of replica ownership.

    Args:
        kind (str): Root resource kind.
        namespace (str): Namespace containing the root.
        name (str): Root resource name.

    Returns:
        int: Fixed shard shared by intake and reconciliation.
    """
    return int.from_bytes(hashlib.sha256(f"{namespace}/{kind}/{name}".encode()).digest()[:8]) % SHARDS


class NotOwner(Exception):
    """
    Stop a pass when this replica cannot prove shard ownership.
    """


def assignment(members: list[str], shards: int = SHARDS) -> dict[str, str]:
    """
    Use rendezvous hashing to minimize movement when replicas join or leave.

    Args:
        members (list[str]): Live replica identities eligible for shard assignment.
        shards (int): Total number of fixed shards to distribute.

    Returns:
        dict[str, str]: String shard IDs mapped to their selected replica identities.
    """
    return (
        {str(shard): max(members, key=lambda member: hashlib.sha256(f"{shard}/{member}".encode()).digest()) for shard in range(shards)}
        if members
        else {}
    )


class Coordinator:
    """
    Keep leader planning separate from exclusive, renewable worker ownership.
    """

    def __init__(self, api: API, namespace: str, identity: str | None = None, *, planner: bool = True) -> None:
        """
        Use a unique process identity, even when a pod restarts under the same name.

        Args:
            api (API): Kubernetes adapter used for refreshed reads and guarded writes.
            namespace (str): Namespace containing the operator resources.
            identity (str | None): Optional process identity; generated uniquely when omitted.
            planner (bool): Whether this process belongs to the root deployment and may plan assignments.
        """
        self.api, self.namespace = api, namespace
        self.identity = identity or str(uuid.uuid4())
        self.planner = planner
        self.worker_pool = os.environ.get("POLYAD_WORKER_POOL", "")
        self.worker_deployment = os.environ.get("POLYAD_WORKER_DEPLOYMENT", "")
        self.worker_cluster = os.environ.get("POLYAD_WORKER_CLUSTER", "")
        if self.worker_pool and (planner or not self.worker_deployment or not self.worker_cluster):
            raise ValueError("Helm worker registration requires a non-planner, Deployment name and hosting cluster")
        self.attachment_ready = not bool(self.worker_pool)
        self.component = role()
        root_mode = os.environ.get("POLYAD_ROOT_ENABLED", "false").lower() == "true"
        root_deployment = os.environ.get("POLYAD_ROOT_DEPLOYMENT", "")
        self.self_graph = os.environ.get("POLYAD_SELF_GRAPH") or (f"{root_deployment}-operators" if root_mode and root_deployment else "")
        self.self_graph_kind = os.environ.get("POLYAD_SELF_GRAPH_KIND", "PolyGraph" if root_mode else "Graph")
        if self.self_graph_kind not in {"Graph", "PolyGraph"}:
            raise ValueError("POLYAD_SELF_GRAPH_KIND must be Graph or PolyGraph")
        self.image = os.environ.get("POLYAD_OPERATOR_IMAGE", "") if os.environ.get("POLYAD_ROOT_ENABLED", "false").lower() == "true" else ""
        self.observed: dict[str, tuple[str, float]] = {}
        self.deadlines: dict[str, float] = {}
        self.owned: set[int] = set()
        self.busy: set[int] = set()
        self.leader = False
        self.last_success = 0.0
        self.members: list[str] = []
        self.lock = asyncio.Lock()
        self.duties = [asyncio.Lock() for _ in range(SHARDS)]

    async def registered(self) -> bool:
        """
        Require the root to register a Helm-installed worker before it may execute.

        Returns:
            bool: Whether the attachment still matches a live, provisioned root pool.
        """
        if not self.worker_pool:
            return True
        pool = await self.api.get("OperatorPool", self.namespace, self.worker_pool)
        ready = bool(
            pool
            and not pool["metadata"].get("deletionTimestamp")
            and pool["spec"].get("existingDeployment") == self.worker_deployment
            and pool["spec"].get("cluster") == self.worker_cluster
            and pool["metadata"].get("annotations", {}).get(f"{GROUP}/operator-graph-registered") == pool["metadata"]["uid"]
        )
        if ready != self.attachment_ready:
            decision(
                "polyad.worker.attachment",
                "Root attachment is active; the worker may acquire assigned shards."
                if ready
                else "Root attachment was removed or changed; the worker pauses mutations and retains existing workloads.",
                key=("OperatorPool", self.namespace, self.worker_pool),
                outcome="allowed" if ready else "blocked",
                reason="attachment_active" if ready else "attachment_unavailable",
                level=logging.INFO if ready else logging.WARNING,
                attributes={"polyad.replica.id": self.identity, "polyad.target.cluster": self.worker_cluster},
            )
        self.attachment_ready = ready
        return ready

    def expired(self, lease: dict[str, Any]) -> bool:
        """
        Measure unchanged lease versions locally; do not trust remote wall clocks.

        Args:
            lease (dict[str, Any]): Latest observed Lease document.

        Returns:
            bool: Whether the unchanged lease version has exceeded its observed duration.
        """
        meta = lease["metadata"]
        name, version = meta["name"], meta["resourceVersion"]
        previous = self.observed.get(name)
        if previous is None or previous[0] != version:
            self.observed[name] = (version, time.monotonic())
        duration = max(DURATION, lease.get("spec", {}).get("leaseDurationSeconds", DURATION))
        return bool(time.monotonic() - self.observed[name][1] > duration)

    async def claim(self, name: str, *, annotations: dict[str, str] | None = None) -> bool:
        """
        Acquire or renew with resourceVersion compare-and-swap, never blind patches.

        Args:
            name (str): Resource name within its namespace.
            annotations (dict[str, str] | None): Resource annotations carrying coordination or revision metadata.

        Returns:
            bool: Whether this replica holds the claim with sufficient write headroom.
        """
        started = time.monotonic()
        current = await self.api.get("Lease", self.namespace, name)
        if current and current.get("spec", {}).get("holderIdentity") != self.identity and not self.expired(current):
            return False
        body = Lease(
            metadata=ObjectMeta(
                name=name,
                namespace=self.namespace,
                labels={f"{GROUP}/coordination": "true"},
                resourceVersion=current["metadata"]["resourceVersion"] if current else None,
                annotations=annotations if annotations is not None else (current or {}).get("metadata", {}).get("annotations", {}),
            ),
            spec=LeaseSpec(
                holderIdentity=self.identity,
                leaseDurationSeconds=DURATION,
                renewTime=datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )
        try:
            await self.api.request("PUT" if current else "POST", "Lease", self.namespace, name if current else "", body)
        except ApiException as error:
            if error.status == 409:
                return False
            raise
        self.deadlines[name] = started + DURATION
        return time.monotonic() < self.deadlines[name] - WRITE_BUDGET

    async def tick(self) -> None:
        """
        Heartbeat membership, elect the planner, and acquire assigned shards.

        Returns:
            None: No return value.
        """
        async with self.lock:
            previous = self.leader, frozenset(self.owned)
            if not await self.registered():
                self.owned.clear()
                self.leader = False
                return
            await self.claim(
                f"polyad-member-{self.identity}",
                annotations={
                    f"{GROUP}/operator-image": self.image,
                    f"{GROUP}/component": self.component,
                    f"{GROUP}/planner": str(self.planner).lower(),
                },
            )
            listing = await self.api.request("GET", "Lease", self.namespace, query=[("labelSelector", f"{GROUP}/coordination=true")])
            for lease in listing.get("items", []):
                self.expired(lease)
            self.members = [
                lease["spec"]["holderIdentity"]
                for lease in listing.get("items", [])
                if lease["metadata"]["name"].startswith("polyad-member-") and not self.expired(lease)
            ]
            self.leader = self.planner and await self.claim("polyad-leader")
            if self.leader:
                members = []
                bootstrap = []
                planners = []
                for lease in listing.get("items", []):
                    if lease["metadata"]["name"].startswith("polyad-member-"):
                        if self.expired(lease):
                            try:
                                await self.api.delete({**lease, "kind": "Lease"})
                            except ApiException as error:
                                if error.status != 409:
                                    raise
                        elif not self.image or lease["metadata"].get("annotations", {}).get(f"{GROUP}/operator-image") == self.image:
                            if lease["metadata"].get("annotations", {}).get(f"{GROUP}/planner") == "true":
                                planners.append(lease["spec"]["holderIdentity"])
                            component = lease["metadata"].get("annotations", {}).get(f"{GROUP}/component", "dense")
                            if component == "bootstrap":
                                bootstrap.append(lease["spec"]["holderIdentity"])
                            elif component in {"dense", "executor"}:
                                members.append(lease["spec"]["holderIdentity"])
                planned = assignment(members or bootstrap)
                if self.self_graph and planners:
                    reserved = str(root_shard(self.self_graph_kind, self.namespace, self.self_graph))
                    planned[reserved] = assignment(planners)[reserved]
                await self.claim("polyad-leader", annotations={f"{GROUP}/assignments": json.dumps(planned, sort_keys=True)})
            leader = await self.api.get("Lease", self.namespace, "polyad-leader")
            if not leader or self.expired(leader):
                if self.owned:
                    decision(
                        "polyad.coordination.paused",
                        "Root planner heartbeat is unavailable; all workload mutations are paused.",
                        outcome="deferred",
                        reason="root_heartbeat_unavailable",
                        level=logging.WARNING,
                        attributes={"polyad.replica.id": self.identity, "polyad.shards": sorted(self.owned)},
                    )
                self.owned.clear()
                return
            planned = json.loads((leader or {}).get("metadata", {}).get("annotations", {}).get(f"{GROUP}/assignments", "{}"))
            for shard in range(SHARDS):
                name = f"polyad-shard-{shard}"
                if planned.get(str(shard)) == self.identity or shard in self.busy:
                    if await self.claim(name):
                        self.owned.add(shard)
                    else:
                        self.owned.discard(shard)
                else:
                    # Stop renewing. A successor waits for the full lease expiry;
                    # no handoff can bypass an outstanding transport request.
                    self.owned.discard(shard)
            self.last_success = time.monotonic()
            if previous != (self.leader, frozenset(self.owned)):
                decision(
                    "polyad.coordination.assignment",
                    "Refreshed this operator replica's leadership and permitted graph shards.",
                    outcome="assigned",
                    reason="assignment_changed",
                    attributes={"polyad.replica.id": self.identity, "polyad.leader": self.leader, "polyad.shards": sorted(self.owned)},
                )
            logger.debug(
                "Coordination refreshed namespace=%s replica=%s leader=%s owned_shards=%s busy_shards=%s",
                self.namespace,
                self.identity,
                self.leader,
                sorted(self.owned),
                sorted(self.busy),
            )

    async def shard_for(self, key: Key, *, api: API | None = None, cluster: str = "") -> int:
        """
        Co-locate nested boundaries and rewrites with their owning root graph.

        Args:
            key (Key): Resource kind, namespace and name to reconcile from fresh API state.
            api (API | None): Workload cluster adapter; coordination always uses the root adapter.
            cluster (str): Cluster identity isolating otherwise identical graph addresses.

        Returns:
            int: Shard assigned to the root graph family.
        """
        kind, namespace, name = key
        seen: set[Key] = set()
        for _ in range(64):
            current_key = kind, namespace, name
            if current_key in seen:
                raise ValueError("cyclic graph ownership")
            seen.add(current_key)
            obj = await (api or self.api).get(kind, namespace, name)
            if obj is None:
                break
            if kind == "Rewrite":
                kind, name = obj["spec"].get("kind", "Graph"), obj["spec"]["graph"]
                continue
            owners = [
                owner
                for owner in obj["metadata"].get("ownerReferences", [])
                if owner.get("controller")
                and owner.get("apiVersion", "").startswith(f"{GROUP}/")
                and owner["kind"] in {"Graph", "PolyGraph", "ReplicaGroup", "Composition"}
            ]
            if not owners:
                break
            kind, name = owners[0]["kind"], owners[0]["name"]
        else:
            raise ValueError("graph ownership exceeds 64 levels")
        return root_shard(kind, f"{cluster}/{namespace}" if cluster else namespace, name)

    async def guard(self) -> None:
        """
        Recheck the lease before each workload mutation and leave transport headroom.

        Returns:
            None: No return value.
        """
        shard = active_shard.get()
        if not await self.registered():
            raise NotOwner("Helm worker attachment is unavailable")
        if shard is None or shard not in self.owned:
            decision(
                "polyad.coordination.fenced",
                "This replica does not own the active shard; the write is blocked.",
                outcome="blocked",
                reason="shard_not_owned",
                level=logging.DEBUG,
                attributes={"polyad.replica.id": self.identity, "polyad.shard": shard},
            )
            logger.debug("Write guard rejected replica=%s shard=%s reason=unowned", self.identity, shard)
            raise NotOwner("no active shard ownership")
        name = f"polyad-shard-{shard}"
        if not self.planner:
            leader = await self.api.get("Lease", self.namespace, "polyad-leader")
            if not leader or self.expired(leader):
                decision(
                    "polyad.coordination.fenced",
                    "The worker cannot confirm root authority; the write is blocked.",
                    outcome="blocked",
                    reason="root_heartbeat_unavailable",
                    level=logging.WARNING,
                    attributes={"polyad.replica.id": self.identity, "polyad.shard": shard},
                )
                raise NotOwner("root planner heartbeat is unavailable")
            observed = self.observed["polyad-leader"][1]
            if time.monotonic() >= observed + DURATION - WRITE_BUDGET:
                decision(
                    "polyad.coordination.fenced",
                    "The root heartbeat is overdue; the worker pauses mutations until authority is refreshed.",
                    outcome="blocked",
                    reason="root_heartbeat_overdue",
                    level=logging.WARNING,
                    attributes={"polyad.replica.id": self.identity, "polyad.shard": shard},
                )
                raise NotOwner("root planner heartbeat is overdue")
        lease = await self.api.get("Lease", self.namespace, name)
        if (
            not lease
            or lease["spec"].get("holderIdentity") != self.identity
            or time.monotonic() >= self.deadlines.get(name, 0) - WRITE_BUDGET
        ):
            logger.debug("Write guard rejected replica=%s shard=%s reason=lease-expired-or-lost", self.identity, shard)
            decision(
                "polyad.coordination.fenced",
                "The shard lease expired or changed owner; this replica cannot continue writing.",
                outcome="blocked",
                reason="lease_lost",
                level=logging.WARNING,
                attributes={
                    "polyad.replica.id": self.identity,
                    "polyad.shard": shard,
                    "polyad.lease.holder": (lease or {}).get("spec", {}).get("holderIdentity", ""),
                },
            )
            raise NotOwner("shard lease lost or renewal overdue")

    @asynccontextmanager
    async def duty(self, key: Key, *, api: API | None = None, cluster: str = "") -> AsyncIterator[None]:
        """
        Hold a shard through one ordered, freshly read reconciliation attempt.

        Args:
            key (Key): Resource kind, namespace and name to reconcile from fresh API state.
            api (API | None): Adapter for resolving ownership in the workload cluster.
            cluster (str): Workload cluster identity included in shard routing.

        Yields:
            None: Control while the guarded mutation or reconciliation slot is held.
        """
        shard = await self.shard_for(key, api=api, cluster=cluster)
        if shard not in self.owned:
            raise NotOwner("graph assigned to another replica")
        await self.duties[shard].acquire()
        self.busy.add(shard)
        token = active_shard.set(shard)
        log_token = decision_context.set(
            {
                "polyad.target.cluster": cluster or os.environ.get("POLYAD_CLUSTER_NAME", ""),
                "polyad.replica.id": self.identity,
                "polyad.shard": shard,
            }
        )
        try:
            await self.guard()
            logger.debug("Duty acquired replica=%s shard=%s kind=%s namespace=%s name=%s", self.identity, shard, *key)
            yield
        finally:
            decision_context.reset(log_token)
            active_shard.reset(token)
            self.busy.discard(shard)
            self.duties[shard].release()
            logger.debug("Duty released replica=%s shard=%s kind=%s namespace=%s name=%s", self.identity, shard, *key)
