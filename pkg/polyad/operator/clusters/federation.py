"""
Manage remote graph intent while destination operators retain workload execution.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml  # type: ignore[import-untyped]
from attrs import evolve
from kubernetes import client, config

from polyad.operator.adapters.kubernetes import API
from polyad.operator.observability.decisions import decision
from polyad_types import resources as asts

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from polyad.operator.reconciliation.controller import Controller

REMOTE = f"{asts.GROUP}/remote-cluster"
PARENT = f"{asts.GROUP}/remote-parent"
INVENTORY = f"{asts.GROUP}/remote-children"


class Federation:
    """
    Resolve administrator-registered clusters and journal remote ownership before writes.
    """

    def __init__(self, api: API) -> None:
        """
        Retain the local write fence and a bounded registry of remote destinations.

        Args:
            api (API): Local adapter holding the parent family's write fence.
        """
        self.api = api
        self.name = os.environ.get("POLYAD_CLUSTER_NAME", "")
        registrations = json.loads(os.environ.get("POLYAD_FEDERATION_CLUSTERS", "[]"))
        if not isinstance(registrations, list) or len(registrations) > 32:
            raise ValueError("federation supports at most 32 registered clusters")
        self.clusters: dict[str, dict[str, str]] = {}
        self.clients: dict[str, tuple[str, API]] = {}
        self.resolver: Callable[[str], tuple[API, str]] | None = None
        for entry in registrations:
            if set(entry) != {"name", "namespace", "kubeconfigSecret"}:
                raise ValueError("cluster registrations require name, namespace and kubeconfigSecret")
            if any(
                not isinstance(value, str) or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", value) for value in entry.values()
            ):
                raise ValueError("cluster registration fields must be DNS labels")
            if entry["name"] in self.clusters or entry["name"] == self.name or not self.name:
                raise ValueError("federation requires distinct local and remote cluster names")
            self.clusters[entry["name"]] = entry

    def close(self) -> None:
        """
        Release remote HTTP pools after the reconciliation queue has joined outstanding writes.

        Returns:
            None: All retained remote transport clients are closed.
        """
        for _, api in self.clients.values():
            api.client.close()
        self.clients.clear()

    def target(self, name: str) -> tuple[API, str]:
        """
        Load projected credentials without changing the process's local Kubernetes client.

        Args:
            name (str): Administrator-registered remote cluster name.

        Returns:
            tuple[API, str]: Guarded remote adapter and its permitted namespace.
        """
        if self.resolver is not None:
            if name == self.name:
                raise ValueError("local children must omit cluster placement")
            return self.resolver(name)
        if name not in self.clusters:
            raise ValueError(f"remote cluster is not registered: {name}; retain registrations until children are drained")
        entry = self.clusters[name]
        source = Path(f"/var/run/polyad/clusters/{name}/config").read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        if name not in self.clients or self.clients[name][0] != digest:
            document = yaml.safe_load(source)
            # Projected credentials are data, never executable kubeconfig plugins or host file references.
            if not isinstance(document, dict) or not document.get("current-context"):
                raise ValueError("remote kubeconfig requires an explicit current-context")
            for user in document.get("users", []):
                if set(user["user"]) - {"token", "client-certificate-data", "client-key-data"}:
                    raise ValueError("remote kubeconfig supports only embedded tokens or client certificates")
            for cluster in document.get("clusters", []):
                settings = cluster["cluster"]
                if set(settings) - {"server", "certificate-authority-data", "tls-server-name"} or not settings.get("server", "").startswith(
                    "https://"
                ):
                    raise ValueError("remote kubeconfig requires verified HTTPS with embedded CA data")
            configuration = client.Configuration()
            config.load_kube_config_from_dict(
                document, client_configuration=configuration, persist_config=False, temp_file_path="/var/run/polyad/transport"
            )
            remote = API(before_write=getattr(self.api, "before_write", None), configuration=configuration, cluster=name)
            if name in self.clients:
                self.clients[name][1].client.close()
            self.clients[name] = digest, remote
        return self.clients[name][1], entry["namespace"]

    def identity(self, parent: dict[str, Any], node: str) -> str:
        """
        Encode cross-cluster ownership without an invalid Kubernetes owner reference.

        Args:
            parent (dict[str, Any]): Persisted parent boundary.
            node (str): Logical node in that boundary.

        Returns:
            str: Exact ownership token stable across retries and source operator replicas.
        """
        meta = parent["metadata"]
        return json.dumps([self.name, meta["namespace"], parent["kind"], meta["name"], meta["uid"], node], separators=(",", ":"))

    def compile(self, parent: dict[str, Any], node: str, cluster: str, child: asts.Resource) -> asts.Resource:
        """
        Address a compiled boundary in its remote namespace and replace local ownership.

        Args:
            parent (dict[str, Any]): Persisted parent boundary.
            node (str): Logical parent node.
            cluster (str): Registered destination cluster.
            child (asts.Resource): Compiled Graph or PolyGraph instance.

        Returns:
            asts.Resource: Remote intent with explicit parent identity and a location-sensitive revision.
        """
        _, namespace = self.target(cluster)
        if child.resource_type.kind not in {"Graph", "PolyGraph"}:
            raise ValueError("remote children must be Graphs or PolyGraphs")
        annotations = dict(child.metadata.annotations or {})
        annotations.update({REMOTE: cluster, PARENT: self.identity(parent, node)})
        annotations[f"{asts.GROUP}/desired-hash"] = hashlib.sha256(
            json.dumps([annotations[f"{asts.GROUP}/desired-hash"], cluster, namespace]).encode()
        ).hexdigest()[:12]
        return evolve(child, metadata=evolve(child.metadata, namespace=namespace, ownerReferences=None, annotations=annotations))

    async def journal(self, controller: Controller, parent: dict[str, Any], desired: dict[str, asts.Resource]) -> None:
        """
        Persist every remote address before a create can succeed or lose acknowledgement.

        Args:
            controller (Controller): Parent's controller for guarded journal writes.
            parent (dict[str, Any]): Fresh parent document.
            desired (dict[str, asts.Resource]): Compiled local and remote children.

        Returns:
            None: All required addresses were already durably recorded.
        """
        from polyad.operator.reconciliation.controller import Pending

        meta = parent["metadata"]
        inventory = json.loads(meta.get("annotations", {}).get(INVENTORY, "[]"))
        updated = copy.deepcopy(inventory)
        for node, child in desired.items():
            cluster = (child.metadata.annotations or {}).get(REMOTE)
            if cluster:
                entry = {
                    "cluster": cluster,
                    "namespace": child.metadata.namespace,
                    "kind": child.resource_type.kind,
                    "name": child.metadata.name,
                    "node": node,
                }
                if entry not in updated:
                    updated.append(entry)
        wanted = {
            ((child.metadata.annotations or {}).get(REMOTE), child.resource_type.kind, child.metadata.name) for child in desired.values()
        }
        for entry in inventory:
            if (entry["cluster"], entry["kind"], entry["name"]) not in wanted:
                remote, namespace = self.target(entry["cluster"])
                if entry["namespace"] != namespace:
                    raise ValueError("registered namespace changed while remote ownership remains")
                if await remote.get(entry["kind"], namespace, entry["name"]) is None:
                    updated.remove(entry)
        if len(updated) > 256:
            raise ValueError("remote child inventory exceeds 256 live or pending entries")
        if updated != inventory:
            await controller.api.request(
                "PATCH",
                parent["kind"],
                meta["namespace"],
                meta["name"],
                {
                    "metadata": {
                        "resourceVersion": meta["resourceVersion"],
                        "annotations": {**meta.get("annotations", {}), INVENTORY: json.dumps(updated)},
                    }
                },
            )
            raise Pending("remote ownership inventory persisted; refresh before remote admission")

    async def children(self, parent: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Refresh journaled remote children, including removed nodes and terminating instances.

        Args:
            parent (dict[str, Any]): Parent whose durable inventory defines the read scope.

        Returns:
            list[dict[str, Any]]: Owned remote children; unreachable destinations raise and block progress.
        """
        result = []
        inventory = json.loads(parent["metadata"].get("annotations", {}).get(INVENTORY, "[]"))
        if not isinstance(inventory, list) or len(inventory) > 256:
            raise ValueError("invalid remote child inventory")
        for entry in inventory:
            if entry["kind"] not in {"Graph", "PolyGraph"}:
                raise ValueError("remote inventory contains a non-graph resource")
            remote, namespace = self.target(entry["cluster"])
            if entry["namespace"] != namespace:
                raise ValueError("registered namespace changed while remote children remain")
            child = await remote.get(entry["kind"], namespace, entry["name"])
            if child is not None:
                annotations = child["metadata"].get("annotations", {})
                if (
                    annotations.get(PARENT) != self.identity(parent, entry["node"])
                    or annotations.get(REMOTE) != entry["cluster"]
                    or child["metadata"].get("ownerReferences")
                ):
                    raise ValueError("refusing to adopt or delete a remote resource with different ownership")
                result.append(child)
        return result

    async def delete(self, child: dict[str, Any]) -> None:
        """
        Route a previously observed child deletion through the original parent's write fence.

        Args:
            child (dict[str, Any]): Fresh child with verified ownership.

        Returns:
            None: Deletion is requested with UID and resource-version preconditions.
        """
        cluster = child["metadata"].get("annotations", {}).get(REMOTE)
        api = self.target(cluster)[0] if cluster else self.api
        await api.delete(child)
        decision(
            "polyad.resource.deleting",
            "Requested deletion of an owned resource; waiting for its finalizers and disappearance.",
            obj=child,
            outcome="applied",
            reason="owned_resource_retired",
            attributes={"polyad.target.cluster": cluster} if cluster else None,
        )
