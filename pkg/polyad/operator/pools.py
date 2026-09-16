"""
Reconcile root-owned remote worker Deployments and KEDA scale intents.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml  # type: ignore[import-untyped]

from polyad.events.visibility import INTERNAL, public_observation
from polyad.metrics.workloads import current_observation
from polyad.operator.controller import Pending
from polyad.operator.coordination import NotOwner
from polyad.operator.decisions import decision, status_decisions
from polyad.operator.remote_scaling import INTENT, remote_revision
from polyad.operator.reserved import DEPLOYMENT
from polyad.operator.rule_state import check_live_rules
from polyad.operator.tracing import traced
from polyad_types.resources import GROUP

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.api import API
    from polyad.operator.root import RootControlPlane

logger = logging.getLogger(__name__)
OWNER = f"{GROUP}/root-owner"
FINALIZER = f"{GROUP}/remote-operator"
REGISTERED = f"{GROUP}/operator-graph-registered"


def contains(actual: Any, desired: Any) -> bool:
    """
    Compare declared fields while allowing API-defaulted object fields.

    Args:
        actual (Any): Persisted Kubernetes value.
        desired (Any): Fields managed by the root.

    Returns:
        bool: Whether every managed value matches current state.
    """
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(key in actual and contains(actual[key], value) for key, value in desired.items())
    if isinstance(desired, list):
        return (
            isinstance(actual, list) and len(actual) == len(desired) and all(contains(a, b) for a, b in zip(actual, desired, strict=True))
        )
    return bool(actual == desired)


class PoolManager:
    """
    Translate root scale requests into UID-fenced remote changes.
    """

    def __init__(self, root: RootControlPlane) -> None:
        """
        Reuse root authority for bootstrap, upgrades and scale handoff.

        Args:
            root (RootControlPlane): Shared root controller and registered transports.
        """
        self.root = root
        self.api = root.controller.api
        self.namespace = root.coordinator.namespace

    @property
    def topology_name(self) -> str:
        """
        Select the single root-local reserved PolyGraph shared by all operator groups.

        Returns:
            str: Release-scoped reserved topology name.
        """
        return os.environ.get("POLYAD_SELF_GRAPH") or f"{os.environ['POLYAD_ROOT_DEPLOYMENT']}-operators"

    @traced("polyad.operator_topology.refresh")
    async def topology(self) -> dict[str, Any]:
        """
        Link the root and each provisioned operator group into one live PolyGraph.

        Returns:
            dict[str, Any]: Reserved PolyGraph after a version-fenced membership refresh.
        """
        source = await self.api.get("Deployment", self.namespace, os.environ["POLYAD_ROOT_DEPLOYMENT"])
        if source is None:
            raise Pending("waiting for the root operator Deployment")
        name = self.topology_name
        previous = await self.api.get("PolyGraph", self.namespace, name)
        owner = json.dumps([self.root.federation.name, self.namespace, "Deployment", source["metadata"]["name"]])
        labels = {INTERNAL: "true"}
        await self.apply(
            self.api,
            {
                "apiVersion": f"{GROUP}/v1alpha1",
                "kind": "Daemon",
                "metadata": {"name": name + "-root", "namespace": self.namespace, "labels": labels},
                "spec": {"replicas": source["spec"].get("replicas", 1), "template": copy.deepcopy(source["spec"]["template"])},
            },
            owner,
        )
        await self.apply(
            self.api,
            {
                "apiVersion": f"{GROUP}/v1alpha1",
                "kind": "Graph",
                "metadata": {
                    "name": name + "-root",
                    "namespace": self.namespace,
                    "labels": labels,
                    "annotations": {DEPLOYMENT: source["metadata"]["name"]},
                },
                "spec": {
                    "templateOnly": True,
                    "mode": "persistent",
                    "nodes": [{"name": "operator", "kind": "Daemon", "ref": name + "-root"}],
                },
            },
            owner,
        )
        nodes = [{"name": "root", "kind": "Graph", "ref": name + "-root"}]
        if component_graph := os.environ.get("POLYAD_COMPONENT_GRAPH"):
            nodes.append({"name": "components", "kind": "Graph", "ref": component_graph})
        listing = await self.api.request("GET", "OperatorPool", self.namespace)
        for pool in sorted((listing or {}).get("items", []), key=lambda item: item["metadata"]["name"]):
            meta = pool["metadata"]
            if meta.get("deletionTimestamp") or meta.get("annotations", {}).get(REGISTERED) != meta["uid"]:
                continue
            nodes.append(
                {
                    "name": f"pool-{meta['uid'][:12]}",
                    "kind": "Graph",
                    "ref": f"polyad-worker-{meta['uid'][:12]}-graph",
                    "cluster": pool["spec"]["cluster"],
                }
            )
        boundary = await self.apply(
            self.api,
            {
                "apiVersion": f"{GROUP}/v1alpha1",
                "kind": "PolyGraph",
                "metadata": {"name": name, "namespace": self.namespace, "labels": labels},
                "spec": {
                    "mode": "persistent",
                    "nodes": nodes,
                    "connections": [
                        edge
                        for node in nodes[1:]
                        for edge in ({"source": "root", "target": node["name"]}, {"source": node["name"], "target": "root"})
                    ],
                },
            },
            owner,
        )
        previous_nodes = {node["name"]: node for node in (previous or {}).get("spec", {}).get("nodes", [])}
        current_nodes = {node["name"]: node for node in nodes}
        for node_name in sorted(previous_nodes.keys() | current_nodes.keys()):
            if previous_nodes.get(node_name) == current_nodes.get(node_name):
                continue
            linked = node_name in current_nodes
            node = current_nodes[node_name] if linked else previous_nodes[node_name]
            decision(
                "polyad.operator_topology.membership",
                f"{'Linked' if linked else 'Unlinked'} operator group {node_name} "
                f"{'in' if linked else 'from'} the reserved root PolyGraph.",
                obj=boundary,
                outcome="applied",
                reason="group_linked" if linked else "group_unlinked",
                attributes={
                    "polyad.node.name": node_name,
                    "polyad.target.graph": node["ref"],
                    "polyad.target.cluster": node.get("cluster", self.root.federation.name),
                },
            )
        return boundary

    async def apply(self, api: API, body: dict[str, Any], owner: str) -> dict[str, Any]:
        """
        Create or update managed objects while refusing to adopt unrelated resources.

        Args:
            api (API): Destination adapter carrying the root write fence.
            body (dict[str, Any]): Desired namespaced object.
            owner (str): Exact root resource incarnation responsible for the object.

        Returns:
            dict[str, Any]: Persisted object after an acknowledged mutation or unchanged read.
        """
        kind, meta = body["kind"], body["metadata"]
        namespace, name = meta.get("namespace", ""), meta["name"]
        meta.setdefault("annotations", {})[OWNER] = owner
        current = await api.get(kind, namespace, name)
        if current:
            if current["metadata"].get("annotations", {}).get(OWNER) != owner:
                decision(
                    "polyad.operator_pool.conflict",
                    "The root cannot adopt this resource because another owner controls it.",
                    obj=current,
                    outcome="blocked",
                    reason="ownership_conflict",
                    level=logging.WARNING,
                    attributes={"polyad.target.cluster": getattr(api, "cluster", self.root.federation.name)},
                )
                raise ValueError(f"refusing to adopt unmanaged {kind}/{name}")
            # API defaulting is retained by merge-patching only declared desired fields.
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if current["metadata"].get("annotations", {}).get(f"{GROUP}/root-revision") == digest and contains(current, body):
                return current
            meta["resourceVersion"] = current["metadata"]["resourceVersion"]
            meta["annotations"][f"{GROUP}/root-revision"] = digest
            if kind == "Secret":
                body["data"].update({key: None for key in current.get("data", {}) if key not in body["data"]})
            result = cast("dict[str, Any]", await api.request("PATCH", kind, namespace, name, body))
            decision(
                "polyad.operator_pool.resource",
                "Updated the root-managed resource to match its declared configuration.",
                obj=result,
                outcome="applied",
                reason="resource_updated",
                attributes={"polyad.target.cluster": getattr(api, "cluster", self.root.federation.name)},
            )
            return result
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        meta["annotations"][f"{GROUP}/root-revision"] = digest
        result = cast("dict[str, Any]", await api.request("POST", kind, namespace, body=body))
        decision(
            "polyad.operator_pool.resource",
            "Created a resource required by the root-managed operator hierarchy.",
            obj=result,
            outcome="applied",
            reason="resource_created",
            attributes={"polyad.target.cluster": getattr(api, "cluster", self.root.federation.name)},
        )
        return result

    async def status(self, obj: dict[str, Any], **values: Any) -> None:
        """
        Publish root-visible capacity and progress without claiming a remote transaction.

        Args:
            obj (dict[str, Any]): Fresh root resource.
            **values (Any): Observed status fields.

        Returns:
            None: Conflicts require a complete reread on the next pass.
        """
        original = obj["metadata"]
        latest = await self.api.get(obj["kind"], self.namespace, original["name"])
        if latest is None or any(latest["metadata"].get(field) != original.get(field) for field in ("uid", "generation")):
            decision(
                "polyad.operator_pool.observation",
                "Root intent changed during reconciliation; the old observation is not published.",
                obj=obj,
                outcome="deferred",
                reason="intent_changed",
                level=logging.DEBUG,
            )
            return
        meta = latest["metadata"]
        # Registration/finalizer writes can advance resourceVersion within this pass.
        # HPA validates a nonempty selector even for external AverageValue metrics.
        # This root-local identity intentionally selects no remote workload Pods.
        values["labelSelector"] = f"{GROUP}/root-scale={meta['uid']}"
        await self.api.request(
            "PATCH",
            obj["kind"],
            self.namespace,
            meta["name"],
            {
                "metadata": {"resourceVersion": meta["resourceVersion"]},
                "status": {"observedGeneration": meta.get("generation", 1), "observedAt": datetime.now(UTC).isoformat(), **values},
            },
            status=True,
        )
        status_decisions(latest, values)

    @traced("polyad.remote_scale.reconcile")
    async def scale(self, obj: dict[str, Any]) -> None:
        """
        Forward a root-local request to a remote ReplicaGroup's existing admission path.

        Args:
            obj (dict[str, Any]): Root RemoteScale with an immutable target UID.

        Returns:
            None: Observed counts describe execution, independently of requested replicas.
        """
        spec = obj["spec"]
        if spec["cluster"] == self.root.federation.name:
            raise ValueError("RemoteScale cannot target the local operator cluster")
        remote, namespace = self.root.resolve(spec["cluster"])
        target = await remote.get("ReplicaGroup", namespace, spec["target"]["name"])
        if not target or target["metadata"]["uid"] != spec["target"]["uid"] or target["metadata"].get("deletionTimestamp"):
            raise ValueError("remote scale target is absent, replaced or terminating")
        if target["spec"].get("replicaSource") and target["spec"].get("inheritReplicas", True):
            raise ValueError("remote scale targets must have inheritReplicas: false")
        owner = {
            "root": self.root.federation.name,
            "namespace": obj["metadata"]["namespace"],
            "name": obj["metadata"]["name"],
            "uid": obj["metadata"]["uid"],
        }
        if target["spec"].get("remoteScaling") != owner:
            decision(
                "polyad.remote_scale.conflict",
                "The destination has not granted this request authority to scale the ReplicaGroup.",
                obj=obj,
                outcome="blocked",
                reason="local_approval_missing",
                level=logging.WARNING,
                attributes={"polyad.target.name": target["metadata"]["name"], "polyad.target.uid": target["metadata"]["uid"]},
            )
            raise ValueError(f"destination has not approved RemoteScale {owner}")
        if target["metadata"].get("generation", 1) != spec["target"].get("generation"):
            decision(
                "polyad.remote_scale.conflict",
                "A local edit superseded this remote scaling request; local intent takes precedence.",
                obj=obj,
                outcome="blocked",
                reason="local_edit_wins",
                level=logging.WARNING,
                attributes={
                    "polyad.target.name": target["metadata"]["name"],
                    "polyad.target.uid": target["metadata"]["uid"],
                    "polyad.generation.expected": spec["target"].get("generation"),
                    "polyad.generation.observed": target["metadata"].get("generation", 1),
                },
            )
            raise ValueError("local ReplicaGroup edits superseded this request; new local approval is required")
        if not await public_observation(remote, target):
            raise ValueError("remote scaling cannot control reserved operator graphs or unresolved ancestry")
        replicas = spec["replicas"]
        if not target["spec"].get("minReplicas", 0) <= replicas <= target["spec"].get("maxReplicas", 32):
            raise ValueError("requested replicas violate the target ReplicaGroup bounds")
        intent = json.dumps(
            {
                "owner": owner,
                "targetUid": target["metadata"]["uid"],
                "targetGeneration": spec["target"]["generation"],
                "requestGeneration": obj["metadata"].get("generation", 1),
                "replicas": replicas,
            },
            sort_keys=True,
        )
        if target["metadata"].get("annotations", {}).get(INTENT) != intent:
            await remote.request(
                "PATCH",
                "ReplicaGroup",
                namespace,
                target["metadata"]["name"],
                {
                    "metadata": {
                        "resourceVersion": target["metadata"]["resourceVersion"],
                        "annotations": {INTENT: intent},
                    },
                },
            )
            observed = target.get("status", {})
            decision(
                "polyad.remote_scale.submitted",
                "Submitted locally authorized replica intent; the destination must admit it before scaling.",
                obj=obj,
                outcome="submitted",
                reason="local_approval_matches",
                attributes={"polyad.target.name": target["metadata"]["name"], "polyad.replicas.requested": replicas},
            )
            await self.status(
                obj,
                replicas=observed.get("replicas", 0),
                readyReplicas=observed.get("readyReplicas", 0),
                phase="Pending",
                message="remote scale intent submitted; awaiting fresh rule admission and execution",
            )
            return
        status = target.get("status", {})
        current = (
            status.get("observedGeneration") == target["metadata"].get("generation", 1)
            and status.get("remoteScaleRevision") == remote_revision(target)
            and status.get("scaleCurrent", False)
            and current_observation(status.get("scaleObservedAt"))
        )
        await self.status(
            obj,
            replicas=status.get("replicas", 0),
            readyReplicas=status.get("readyReplicas", 0),
            phase="Ready" if current else "Pending",
            message=status.get("message", ""),
        )

    @traced("polyad.operator_pool.reconcile")
    async def pool(self, obj: dict[str, Any]) -> None:
        """
        Install or upgrade a remote worker from the root Deployment and projected credentials.

        Args:
            obj (dict[str, Any]): Root OperatorPool capacity intent.

        Returns:
            None: Root ownership is recorded before any remote object is created.
        """
        meta, spec = obj["metadata"], obj["spec"]
        controller = spec.get("controller", "Deployment")
        if controller not in {"Deployment", "DaemonSet"} or (controller == "DaemonSet" and spec["replicas"] != 1):
            raise ValueError(
                "OperatorPool controller must be Deployment, or DaemonSet with replicas: 1; node eligibility controls DaemonSets"
            )
        remote, namespace = self.root.resolve(spec["cluster"])
        recorded_namespace = meta.get("annotations", {}).get(f"{GROUP}/worker-namespace")
        if recorded_namespace is not None and recorded_namespace != namespace:
            raise ValueError("retain the pool's registered namespace until its remote workers are drained")
        if spec["cluster"] == self.root.federation.name:
            raise ValueError("OperatorPool must select a remote registered cluster")
        owner = json.dumps([self.root.federation.name, self.namespace, meta["uid"]], separators=(",", ":"))
        name = f"polyad-worker-{meta['uid'][:12]}"
        labels = {f"{GROUP}/operator-pool": meta["uid"], INTERNAL: "true"}
        finalizers = meta.get("finalizers", [])
        if meta.get("deletionTimestamp"):
            if meta.get("annotations", {}).get(REGISTERED) == meta["uid"]:
                boundary = await self.topology()
                children = await self.root.federation.children(boundary)
                if any(child["metadata"].get("labels", {}).get(f"{GROUP}/node") == f"pool-{meta['uid'][:12]}" for child in children):
                    raise Pending("waiting for the unlinked operator Graph and remote workloads to drain")
            # Keep workloads, CRDs and storage. Only remove the pool's own execution machinery.
            for kind in ("Deployment", "Graph", "Daemon", "ConfigMap", "Secret"):
                listing = await remote.request("GET", kind, namespace, query=[("labelSelector", f"{GROUP}/operator-pool={meta['uid']}")])
                for child in (listing or {}).get("items", []):
                    if child["metadata"].get("annotations", {}).get(OWNER) != owner:
                        decision(
                            "polyad.operator_pool.conflict",
                            "Worker cleanup found a resource with different ownership; deletion is blocked.",
                            obj=child,
                            outcome="blocked",
                            reason="cleanup_ownership_conflict",
                            level=logging.WARNING,
                            attributes={"polyad.target.cluster": spec["cluster"]},
                        )
                        raise ValueError("remote pool ownership changed during cleanup")
                    await remote.delete({**child, "kind": kind})
                    decision(
                        "polyad.operator_pool.deleting",
                        "Requested removal of this pool's execution machinery after unlinking its Graph.",
                        obj=child,
                        outcome="applied",
                        reason="pool_deleted",
                        attributes={"polyad.target.cluster": spec["cluster"]},
                    )
                if (listing or {}).get("items"):
                    raise Pending("waiting for remote worker cleanup")
            if FINALIZER in finalizers:
                await self.api.request(
                    "PATCH",
                    "OperatorPool",
                    self.namespace,
                    meta["name"],
                    {
                        "metadata": {
                            "resourceVersion": meta["resourceVersion"],
                            "finalizers": [item for item in finalizers if item != FINALIZER],
                        },
                    },
                )
            return
        if FINALIZER not in finalizers or recorded_namespace is None:
            await self.api.request(
                "PATCH",
                "OperatorPool",
                self.namespace,
                meta["name"],
                {
                    "metadata": {
                        "resourceVersion": meta["resourceVersion"],
                        "finalizers": list(dict.fromkeys([*finalizers, FINALIZER])),
                        "annotations": {f"{GROUP}/worker-namespace": namespace},
                    },
                },
            )
            raise Pending("root pool ownership recorded")
        source = await self.api.get("Deployment", self.namespace, os.environ["POLYAD_ROOT_DEPLOYMENT"])
        if source is None:
            raise ValueError("root Deployment is unavailable")
        desired_image = next(item["image"] for item in source["spec"]["template"]["spec"]["containers"] if item["name"] == "operator")
        if desired_image != os.environ.get("POLYAD_OPERATOR_IMAGE"):
            raise Pending("waiting for a worker running the root's desired image before upgrading remote schemas")
        # Shared CRDs are installed/upgraded by this root, never removed with a pool.
        schemas = sorted(Path(os.environ.get("POLYAD_CRD_DIRECTORY", "/opt/polyad/crds")).glob("*.yaml"))
        if not schemas:
            raise ValueError("operator image does not contain the Polyad CRD bundle")
        for path in schemas:
            body = yaml.safe_load(path.read_text())
            current = await remote.get("CustomResourceDefinition", "", body["metadata"]["name"])
            if current:
                if not contains(current["spec"], body["spec"]):
                    await remote.request(
                        "PATCH",
                        "CustomResourceDefinition",
                        "",
                        body["metadata"]["name"],
                        {
                            "metadata": {"resourceVersion": current["metadata"]["resourceVersion"]},
                            "spec": body["spec"],
                        },
                    )
            else:
                await remote.request("POST", "CustomResourceDefinition", "", body=body)
        pod = copy.deepcopy(source["spec"]["template"])
        pod["metadata"] = {"labels": {**labels, "sidecar.istio.io/inject": "false"}}
        pod["spec"].pop("serviceAccountName", None)
        pod["spec"]["automountServiceAccountToken"] = False
        for field in ("resources", "nodeSelector", "tolerations"):
            if field in spec and field != "resources":
                pod["spec"][field] = spec[field]
        container = next(item for item in pod["spec"]["containers"] if item["name"] == "operator")
        if "resources" in spec:
            container["resources"] = spec["resources"]
        env = {item["name"]: item for item in container.get("env", [])}
        for variable in ("POLYAD_AUTH_CONFIG_FILE", "POLYAD_AUTH_DATABASE_DSN_FILE"):
            env.pop(variable, None)
        excluded = {"authentication", "authentication-database"}
        pod["spec"]["volumes"] = [volume for volume in pod["spec"].get("volumes", []) if volume["name"] not in excluded]
        container["volumeMounts"] = [mount for mount in container.get("volumeMounts", []) if mount["name"] not in excluded]
        for feature in ("API", "EVENTS", "CONNECTIONS", "OPERATOR_MESH"):
            env[f"POLYAD_{feature}_ENABLED"] = {"name": f"POLYAD_{feature}_ENABLED", "value": "false"}
        env["POLYAD_ROOT_WORKER"] = {"name": "POLYAD_ROOT_WORKER", "value": "true"}
        env["POLYAD_POD_CLUSTER"] = {"name": "POLYAD_POD_CLUSTER", "value": spec["cluster"]}
        env["POLYAD_COMPONENT"] = {"name": "POLYAD_COMPONENT", "value": "executor"}
        env["KUBECONFIG"] = {"name": "KUBECONFIG", "value": "/var/run/polyad/root/config"}
        # Every Kubernetes request from the replica uses the root credentials or a registered remote adapter.
        revisions = []
        for volume in pod["spec"].get("volumes", []):
            if "configMap" in volume:
                original = volume["configMap"]["name"]
                configuration = await self.api.get("ConfigMap", self.namespace, original)
                if configuration is None:
                    raise ValueError(f"root workload credential configuration is absent: {original}")
                copied = name + "-" + hashlib.sha256(original.encode()).hexdigest()[:8]
                await self.apply(
                    remote,
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": copied, "namespace": namespace, "labels": labels},
                        "data": configuration.get("data", {}),
                    },
                    owner,
                )
                volume["configMap"]["name"] = copied
        secret_names = {volume["secret"]["secretName"] for volume in pod["spec"].get("volumes", []) if "secret" in volume}
        secret_names.update(
            item["valueFrom"]["secretKeyRef"]["name"] for item in env.values() if "secretKeyRef" in item.get("valueFrom", {})
        )
        for original in sorted(secret_names):
            credential = await self.api.get("Secret", self.namespace, original)
            if credential is None:
                raise ValueError(f"root credential Secret is absent: {original}")
            copied = name + "-" + hashlib.sha256(original.encode()).hexdigest()[:8]
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": copied, "namespace": namespace, "labels": labels},
                "type": "Opaque",
                "data": credential.get("data", {}),
            }
            await self.apply(remote, secret, owner)
            for volume in pod["spec"].get("volumes", []):
                if volume.get("secret", {}).get("secretName") == original:
                    volume["secret"]["secretName"] = copied
            revisions.append(credential["metadata"]["resourceVersion"])
            for item in env.values():
                reference = item.get("valueFrom", {}).get("secretKeyRef", {})
                if reference.get("name") == original:
                    reference["name"] = copied
        container["env"] = list(env.values())
        pod["metadata"]["annotations"] = {f"{GROUP}/credential-revision": hashlib.sha256(json.dumps(revisions).encode()).hexdigest()}
        if controller == "DaemonSet":
            await self.graph_pool(obj, pod, owner)
            return
        body = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "replicas": spec["replicas"],
                "selector": {"matchLabels": labels},
                "template": pod,
                "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
            },
        }
        boundary = await self.graph_pool(obj, pod, owner)
        await check_live_rules(self.api, boundary)
        group = await remote.get("Graph", namespace, name + "-graph")
        if group is None:
            raise Pending("waiting for the operator group's graph definition")
        await check_live_rules(remote, group)
        result = await self.apply(remote, body, owner)
        status = result.get("status", {})
        await self.status(
            obj,
            replicas=status.get("replicas", 0),
            readyReplicas=status.get("readyReplicas", 0),
            phase="Ready"
            if (
                status.get("observedGeneration") == result["metadata"].get("generation", 1)
                and status.get("replicas", 0) == status.get("readyReplicas", 0) == spec["replicas"]
            )
            else "Pending",
            message="",
        )

    async def graph_pool(self, obj: dict[str, Any], pod: dict[str, Any], owner: str) -> dict[str, Any]:
        """
        Register one remote group Graph beneath the shared reserved PolyGraph.

        Args:
            obj (dict[str, Any]): Root pool with Deployment or DaemonSet scheduling.
            pod (dict[str, Any]): Bootstrapped worker template with copied root credentials.
            owner (str): Exact root pool incarnation for definition ownership.

        Returns:
            dict[str, Any]: Shared reserved topology for fresh structural admission checks.
        """
        meta, spec = obj["metadata"], obj["spec"]
        remote, namespace = self.root.resolve(spec["cluster"])
        name = f"polyad-worker-{meta['uid'][:12]}"
        labels = {f"{GROUP}/operator-pool": meta["uid"], INTERNAL: "true"}
        controller = spec.get("controller", "Deployment")
        await self.apply(
            remote,
            {
                "apiVersion": f"{GROUP}/v1alpha1",
                "kind": "Daemon",
                "metadata": {"name": name + "-daemon", "namespace": namespace, "labels": labels},
                "spec": {"controller": controller, "replicas": spec["replicas"], "template": pod},
            },
            owner,
        )
        await self.apply(
            remote,
            {
                "apiVersion": f"{GROUP}/v1alpha1",
                "kind": "Graph",
                "metadata": {
                    "name": name + "-graph",
                    "namespace": namespace,
                    "labels": labels,
                    "annotations": {DEPLOYMENT: name} if controller == "Deployment" else {},
                },
                "spec": {
                    "templateOnly": True,
                    "mode": "persistent",
                    "nodes": [
                        {"name": "workers", "kind": "Daemon", "ref": name + "-daemon"},
                    ],
                },
            },
            owner,
        )
        if meta.get("annotations", {}).get(REGISTERED) != meta["uid"]:
            obj = await self.api.request(
                "PATCH",
                "OperatorPool",
                self.namespace,
                meta["name"],
                {
                    "metadata": {
                        "resourceVersion": meta["resourceVersion"],
                        "annotations": {**meta.get("annotations", {}), REGISTERED: meta["uid"]},
                    }
                },
            )
        boundary = await self.topology()
        if controller != "DaemonSet":
            return boundary
        children = await self.root.federation.children(boundary)
        children = [child for child in children if child["metadata"].get("labels", {}).get(f"{GROUP}/node") == f"pool-{meta['uid'][:12]}"]
        counts: dict[str, Any] = next(
            (child.get("status", {}).get("workloads", {}).get("workers", {}).get("values", {}) for child in children), {}
        )
        await self.status(
            obj,
            replicas=counts.get("replicas", 0),
            readyReplicas=counts.get("readyReplicas", 0),
            phase="Ready" if children and children[0].get("status", {}).get("ready") else "Pending",
            message="DaemonSet capacity follows eligible nodes; KEDA must not target this pool's scale subresource",
        )
        return boundary

    async def run(self) -> None:
        """
        Rediscover root scale resources and serialize each request through root leases.

        Returns:
            None: Runs until cancellation, leaving remote workloads intact on failure.
        """
        while True:
            topology_key = ("PolyGraph", self.namespace, self.topology_name)
            try:
                async with self.root.coordinator.duty(topology_key):
                    await self.topology()
            except NotOwner:
                pass
            except Exception:
                logger.exception("Reserved operator topology could not refresh; retaining its existing groups")
            for kind in ("OperatorPool", "RemoteScale"):
                try:
                    listing = await self.api.request("GET", kind, self.namespace)
                    for obj in (listing or {}).get("items", []):
                        obj.setdefault("kind", kind)
                        try:
                            key = topology_key if kind == "OperatorPool" else (kind, self.namespace, obj["metadata"]["name"])
                            async with self.root.coordinator.duty(key):
                                try:
                                    await (self.pool(obj) if kind == "OperatorPool" else self.scale(obj))
                                except Pending as error:
                                    await self.status(obj, phase="Pending", message=str(error))
                                except ValueError as error:
                                    await self.status(obj, phase="Blocked", message=str(error))
                                except Exception:
                                    logger.exception("Root %s/%s could not refresh its destination", kind, obj["metadata"]["name"])
                                    await self.status(obj, phase="Pending", message="destination unavailable; retaining existing execution")
                        except NotOwner:
                            pass
                        except Exception:
                            logger.exception("Root %s/%s failed; other targets remain eligible", kind, obj["metadata"]["name"])
                except Exception:
                    logger.exception("Root %s reconciliation failed; retrying fresh state", kind)
            await asyncio.sleep(5)
