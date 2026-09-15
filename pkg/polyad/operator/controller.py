"""
Translate refreshed graph intent into owned Kubernetes execution resources.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from attrs import evolve
from cattrs.errors import BaseValidationError

from polyad.compiler import asts
from polyad.compiler.audit import trace_child
from polyad.compiler.children import child_name as compile_child_name
from polyad.compiler.children import owned_child
from polyad.compiler.storage import configure_storage, storage_fields
from polyad.graph.gates import DelayGate, Gate
from polyad.graph.topology import converter, topology
from polyad.operator.api import GROUP
from polyad.operator.compositions import drain_composition, reconcile_composition
from polyad.operator.graph_status import instance_metrics
from polyad.operator.graph_status import observed as observed
from polyad.operator.placement import merge_placement, place_pod
from polyad.operator.rules import check_rules

if TYPE_CHECKING:
    from typing import Any

    from polyad.graph.storage import Persistence
    from polyad.operator.api import API
    from polyad.operator.queue import Key

BOUNDARIES = asts.BOUNDARY_KINDS
FINALIZER = f"{GROUP}/drain"


def _status_value(value: Any) -> Any:
    # JSON merge-patch removes null object fields; compare with the stored form.
    if isinstance(value, dict):
        return {key: _status_value(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_status_value(item) for item in value]
    return value


class Pending(Exception):
    """
    Require a later refreshed observation before proceeding.
    """

    def __init__(self, message: str, *, phase: str = "Reconciling") -> None:
        """
        Carry the lifecycle phase while waiting for another observation.

        Args:
            message (str): Explanation of the blocking observation.
            phase (str): Graph phase to publish while the attempt remains pending.
        """
        super().__init__(message)
        self.phase = phase


def child_name(parent: dict[str, Any], node: str) -> str:
    """
    Keep addresses stable across replacements while names remain unique within a boundary.

    Args:
        parent (dict[str, Any]): Persisted parent identity and ownership boundary.
        node (str): Name of the node within its graph boundary.

    Returns:
        str: Stable child name derived from the parent UID and node name.
    """
    return compile_child_name(asts.converter.structure(parent["metadata"], asts.ObjectMeta), node)


def references(value: Any, names: dict[str, str]) -> Any:
    """
    Resolve explicit ${nodes.NAME.name} references in container and resource definitions.

    Args:
        value (Any): Value to serialize or resolve.
        names (dict[str, str]): Graph node names mapped to compiled Kubernetes resource names.

    Returns:
        Any: Independent structure with explicit node references resolved.
    """
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            if match[1] not in names:
                raise ValueError(f"unknown resource reference: {match[1]}")
            return names[match[1]]

        return re.sub(r"\$\{nodes\.([a-z0-9-]+)\.name\}", replace, value)
    if isinstance(value, dict):
        return {key: references(item, names) for key, item in value.items()}
    if isinstance(value, list):
        return [references(item, names) for item in value]
    return value


class Controller:
    """
    Reconcile one namespace-scoped boundary per ordered queue turn.
    """

    def __init__(self, api: API) -> None:
        """
        Bind the API adapter for production or deterministic tests.

        Args:
            api (API): Kubernetes adapter used for refreshed reads and guarded writes.
        """
        self.api = api

    async def status(self, obj: dict[str, Any], values: dict[str, Any]) -> None:
        """
        Commit observations only against the resource version that produced them.

        Args:
            obj (dict[str, Any]): Resource document from the latest API observation.
            values (dict[str, Any]): Observed status fields to merge at the current resource version.

        Returns:
            None: No return value.
        """
        meta = obj["metadata"]
        if all(_status_value(obj.get("status", {}).get(key)) == _status_value(value) for key, value in values.items()):
            return
        values = copy.deepcopy(values)
        if "nodes" in values:
            for removed in obj.get("status", {}).get("nodes", {}).keys() - values["nodes"].keys():
                values["nodes"][removed] = None
        await self.api.request(
            "PATCH",
            obj["kind"],
            meta["namespace"],
            meta["name"],
            asts.StatusPatch(metadata=asts.ObjectMeta(resourceVersion=meta["resourceVersion"]), status=values),
            status=True,
        )

    async def definition(self, kind: str, namespace: str, name: str) -> dict[str, Any]:
        """
        Refresh a referenced definition; absent or deleting definitions block admission.

        Args:
            kind (str): Kubernetes resource kind.
            namespace (str): Namespace containing the operator resources.
            name (str): Resource name within its namespace.

        Returns:
            dict[str, Any]: Live definition document suitable for compilation.
        """
        obj = await self.api.get(kind, namespace, name)
        if obj is None or obj["metadata"].get("deletionTimestamp"):
            raise Pending(f"waiting for {kind}/{name}")
        return obj

    async def drain(self, obj: dict[str, Any]) -> bool:
        """
        Release children before parent finalizers, preserving custom child cleanup gates.

        Args:
            obj (dict[str, Any]): Resource document from the latest API observation.

        Returns:
            bool: Whether a fresh listing contains no remaining owned children.
        """
        meta = obj["metadata"]
        if obj["kind"] == "Composition":
            return await drain_composition(self, obj)
        children = await self.api.owned(meta["namespace"], meta["uid"])
        for child in children:
            if not child["metadata"].get("deletionTimestamp"):
                await self.api.delete(child)
        return not children

    async def storage_claim(self, namespace: str, storage: Persistence) -> None:
        """
        Refresh the declared claim and validate its class before treating work as eligible.

        Args:
            namespace (str): Workload namespace containing the persistent claim.
            storage (Persistence): Enabled workload storage contract.

        Returns:
            None: No return value.
        """
        claim = await self.definition("PersistentVolumeClaim", namespace, storage.claimName or "")
        if claim.get("spec", {}).get("storageClassName") != storage.storageClass:
            raise ValueError("persistent claim storageClassName does not match workload persistence.storageClass")

    async def reconcile(self, key: Key) -> None:
        """
        Read current intent, fence deletion, then execute a single idempotent pass.

        Args:
            key (Key): Resource kind, namespace and name to reconcile from fresh API state.

        Returns:
            None: No return value.
        """
        try:
            await self._reconcile(key)
        except Pending as error:
            await self.report_metrics(key, pending=error)
            raise
        except (ValueError, TypeError, BaseValidationError):
            await self.report_metrics(key)
            raise
        else:
            await self.report_metrics(key)

    async def report_metrics(self, key: Key, *, pending: Pending | None = None) -> None:
        """
        Refresh graph inventory and publish metrics through the ordered API adapter.

        Args:
            key (Key): Boundary identity to reread after the reconciliation attempt.
            pending (Pending | None): Optional explanation of a blocked lifecycle transition.

        Returns:
            None: No return value.
        """
        if key[0] == "Composition" and pending:
            receipt = await self.api.get(*key)
            if receipt is not None:
                await self.status(receipt, {"phase": pending.phase, "message": str(pending), "ready": False, "completed": False})
            return
        if key[0] not in BOUNDARIES:
            return
        obj = await self.api.get(*key)
        if obj is None or obj.get("spec", {}).get("templateOnly", False):
            return
        children = await self.api.owned(key[1], obj["metadata"]["uid"])
        values: dict[str, Any] = {}
        if pending or obj.get("status", {}).get("phase") != "Invalid":
            values["message"] = str(pending) if pending else ""
        if pending or obj["metadata"].get("deletionTimestamp"):
            values.update(
                phase=pending.phase if pending else "Draining",
                ready=False,
                completed=False,
                failed=False,
                observedGeneration=obj["metadata"]["generation"],
            )
        snapshot = {**obj, "status": {**obj.get("status", {}), **values}}
        values["metrics"] = instance_metrics(snapshot, children)
        await self.status(obj, values)

    async def _reconcile(self, key: Key) -> None:
        kind, namespace, name = key
        if kind == "Health":
            await self.api.request("GET", "Graph", namespace)
            return
        obj = await self.api.get(kind, namespace, name)
        if obj is None:
            return
        if obj["metadata"].get("deletionTimestamp"):
            if not await self.drain(obj):
                raise Pending("waiting for owned resources and their finalizers", phase="Draining")
            if FINALIZER in obj["metadata"].get("finalizers", []):
                await self.finalizers(obj, remove=True)
            return
        if kind in BOUNDARIES | {"Rewrite", "Composition"} and FINALIZER not in obj["metadata"].get("finalizers", []):
            await self.finalizers(obj)
            raise Pending("drain finalizer persisted; refresh before admission")
        if obj.get("spec", {}).get("templateOnly", False):
            return
        if kind == "Composition":
            await reconcile_composition(self, obj)
        elif kind == "Rewrite":
            await self.rewrite(obj)
        elif kind == "Feedback":
            await self.feedback(obj)
        elif kind in {"Graph", "EphemeralGraph", "PolyGraph"}:
            await self.graph(obj)

    async def finalizers(self, obj: dict[str, Any], *, remove: bool = False) -> None:
        """
        Change only our finalizer through the same guarded queue as graph writes.

        Args:
            obj (dict[str, Any]): Resource document from the latest API observation.
            remove (bool): Whether to release the drain finalizer instead of adding it.

        Returns:
            None: No return value.
        """
        meta = obj["metadata"]
        values = [item for item in meta.get("finalizers", []) if item != FINALIZER]
        if not remove:
            values.append(FINALIZER)
        await self.api.request(
            "PATCH",
            obj["kind"],
            meta["namespace"],
            meta["name"],
            {"metadata": {"resourceVersion": meta["resourceVersion"], "finalizers": values}},
        )

    async def rewrite(self, obj: dict[str, Any]) -> None:
        """
        Apply a full declarative topology replacement once at an expected generation.

        Args:
            obj (dict[str, Any]): Resource document from the latest API observation.

        Returns:
            None: No return value.
        """
        if obj.get("status", {}).get("applied"):
            return
        spec, meta = obj["spec"], obj["metadata"]
        topology({key: value for key, value in spec["topology"].items() if key != "placement"}, spec.get("kind", "Graph"))
        target = await self.definition(spec.get("kind", "Graph"), meta["namespace"], spec["graph"])
        await check_rules(self.api, meta["namespace"], target["kind"], spec["topology"])
        # This annotation is committed atomically with the spec and survives a status-write timeout.
        token = meta["uid"]
        if target["metadata"].get("annotations", {}).get(f"{GROUP}/rewrite") != token:
            if target["metadata"]["generation"] != spec["expectedGeneration"]:
                raise ValueError("rewrite target generation changed")
            target_ast = asts.from_document(target)
            if not isinstance(target_ast, (asts.Graph, asts.EphemeralGraph, asts.PolyGraph)):
                raise ValueError("rewrites require a graph target")
            replacement = evolve(
                target_ast,
                spec=copy.deepcopy(spec["topology"]),
                metadata=evolve(target_ast.metadata, annotations={**(target_ast.metadata.annotations or {}), f"{GROUP}/rewrite": token}),
            )
            await self.api.request("PUT", target["kind"], meta["namespace"], target["metadata"]["name"], replacement)
        await self.status(obj, {"applied": True, "observedGeneration": meta["generation"]})

    def child(
        self,
        parent: dict[str, Any],
        node_name: str,
        kind: str,
        spec: dict[str, Any] | asts.JobSpec | asts.DeploymentSpec,
        *,
        extra: dict[str, Any] | None = None,
    ) -> asts.Resource:
        """
        Compile a resource AST with stable revision hashes and typed controller ownership.

        Args:
            parent (dict[str, Any]): Persisted parent identity and ownership boundary.
            node_name (str): Node name used for child identity and ownership labels.
            kind (str): Kubernetes resource kind.
            spec (dict[str, Any] | asts.JobSpec | asts.DeploymentSpec): Desired resource configuration.
            extra (dict[str, Any] | None): Unmodeled native fields preserved during serialization.

        Returns:
            asts.Resource: Owned resource AST ready for serialization.
        """
        return owned_child(asts.from_document(parent), node_name, kind, spec, extra=extra)

    async def ensure(self, desired: asts.Resource) -> dict[str, Any] | None:
        """
        Create once; never adopt a same-name object owned by somebody else.

        Args:
            desired (asts.Resource): Compiled resource with the required ownership and revision.

        Returns:
            dict[str, Any] | None: Existing matching resource, or None after a new creation request.
        """
        meta = desired.metadata
        if not meta.namespace or not meta.name:
            raise ValueError("admission requires a namespaced resource name")
        kind = desired.resource_type.kind
        current = await self.api.get(kind, meta.namespace, meta.name)
        if current is not None:
            current_meta = asts.converter.structure(current["metadata"], asts.ObjectMeta)
            if current_meta.ownerReferences != meta.ownerReferences:
                raise ValueError("refusing to adopt a resource with different ownership")
            if current_meta.deletionTimestamp or (current_meta.annotations or {}).get(f"{GROUP}/desired-hash") != (
                meta.annotations or {}
            ).get(f"{GROUP}/desired-hash"):
                raise Pending("waiting for resource replacement", phase="Draining")
            return current
        await self.api.request("POST", kind, meta.namespace, body=desired)
        return None  # Creation acknowledgement is not readiness; observe it on a fresh pass.

    async def graph(self, obj: dict[str, Any]) -> None:
        """
        Admit nodes by observed edge predicates and reserved slots; drain structural changes first.

        Args:
            obj (dict[str, Any]): Resource document from the latest API observation.

        Returns:
            None: No return value.
        """
        raw = dict(obj["spec"])
        placement = raw.pop("placement", None)
        graph = topology(raw, obj["kind"])
        meta, namespace = obj["metadata"], obj["metadata"]["namespace"]
        ephemeral = obj["kind"] == "EphemeralGraph" or meta.get("annotations", {}).get(f"{GROUP}/ephemeral") == "true"
        if obj["kind"] == "EphemeralGraph" and not placement:
            raise ValueError("EphemeralGraph requires explicit spot placement")
        policy = {}
        if graph.shutdownPolicy:
            policy = (await self.definition("ShutdownPolicy", namespace, graph.shutdownPolicy))["spec"]
        stopped = obj.get("status", {}).get("phase") == "Stopped"
        limit = policy.get("afterSeconds")
        if limit is not None:
            age = (datetime.now(UTC) - datetime.fromisoformat(meta["creationTimestamp"].replace("Z", "+00:00"))).total_seconds()
            stopped |= age >= limit
        if graph.suspend or stopped:
            drained = await self.drain(obj)
            await self.status(
                obj,
                {
                    "phase": "Stopped" if stopped and drained else "Suspended" if drained else "Draining",
                    "ready": False,
                    "completed": False,
                    "failed": False,
                    "observedGeneration": meta["generation"],
                },
            )
            return
        desired: dict[str, asts.Resource] = {}
        rule_reports = await check_rules(self.api, namespace, obj["kind"], obj["spec"])
        persistence = {}
        names = {node.name: child_name(obj, node.name) for node in graph.nodes}
        for node in graph.nodes:
            definition = await self.definition(node.kind, namespace, node.ref)
            spec = (
                references(copy.deepcopy(definition["spec"]), names) if node.kind not in BOUNDARIES else copy.deepcopy(definition["spec"])
            )
            if node.kind in {"Workload", "Ephemeral", "Daemon"}:
                persistence[node.name] = configure_storage(spec, ephemeral=ephemeral or node.kind == "Ephemeral")
                pod = spec["template"]
                pod_spec = pod["spec"]
                effective = merge_placement(placement, spec.get("placement"))
                if node.kind == "Ephemeral" and not effective:
                    raise ValueError("Ephemeral requires explicit spot placement")
                place_pod(pod_spec, effective)
                pod_spec["terminationGracePeriodSeconds"] = policy.get("graceSeconds", pod_spec.get("terminationGracePeriodSeconds", 30))
                if node.kind == "Daemon":
                    for container in pod_spec["containers"]:
                        if any(probe not in container for probe in ("startupProbe", "readinessProbe", "livenessProbe")):
                            raise ValueError("daemon containers require startup, readiness and liveness probes")
                    pod_spec["restartPolicy"] = "Always"
                    label = {f"{GROUP}/instance": hashlib.sha256(f"{meta['uid']}/{node.name}".encode()).hexdigest()[:32]}
                    pod.setdefault("metadata", {}).setdefault("labels", {}).update(label)
                    runtime: asts.JobSpec | asts.DeploymentSpec = asts.DeploymentSpec(
                        replicas=spec.get("replicas", 1),
                        selector=asts.LabelSelector(matchLabels=label),
                        template=asts.converter.structure(pod, asts.PodTemplate),
                        strategy=asts.DeploymentStrategy(type="Recreate"),
                    )
                    kind = "Deployment"
                else:
                    pod_spec["restartPolicy"] = "Never"
                    runtime = asts.JobSpec(
                        template=asts.converter.structure(pod, asts.PodTemplate), backoffLimit=spec.get("backoffLimit", 6)
                    )
                    kind = "Job"
                desired[node.name] = self.child(obj, node.name, kind, runtime)
            elif node.kind == "Resource":
                manifest = spec["manifest"]
                if ephemeral and (
                    manifest["kind"] in {"PersistentVolumeClaim", "StorageClass"} or storage_fields(manifest.get("spec", {}))
                ):
                    raise ValueError("persistent storage and storage classes are invalid under Ephemeral graphs")
                if manifest["kind"] not in {"Service", "ConfigMap", "PersistentVolumeClaim"}:
                    raise ValueError("resource kind is outside the operator's namespaced allowlist")
                extra = {key: value for key, value in manifest.items() if key not in {"apiVersion", "kind", "metadata", "spec"}}
                desired[node.name] = self.child(obj, node.name, manifest["kind"], manifest.get("spec", {}), extra=extra)
            else:
                if not spec.get("templateOnly", False):
                    raise ValueError("nested boundaries must reference templateOnly definitions")
                finite_child = spec.get("rounds") is not None if node.kind == "Feedback" else spec.get("mode", "finite") == "finite"
                if not finite_child and (
                    graph.mode == "finite"
                    or any(edge.node == node.name and edge.condition == "completed" for other in graph.nodes for edge in other.requires)
                ):
                    raise ValueError("persistent nested boundaries cannot satisfy finite completion")
                spec["templateOnly"] = False
                if graph.rules:
                    target_spec = spec["graph"] if node.kind == "Feedback" else spec
                    target_spec["rules"] = sorted(set(target_spec.get("rules", [])) | set(graph.rules))
                if placement:
                    if node.kind == "Feedback":
                        spec["graph"]["placement"] = merge_placement(placement, spec["graph"].get("placement"))
                        if obj["kind"] == "EphemeralGraph" and spec.get("kind", "Graph") == "Graph":
                            spec["kind"] = "EphemeralGraph"
                    else:
                        spec["placement"] = merge_placement(placement, spec.get("placement"))
                kind = "EphemeralGraph" if obj["kind"] == "EphemeralGraph" and node.kind == "Graph" else node.kind
                lineage = json.loads(meta.get("annotations", {}).get(f"{GROUP}/lineage", "[]"))
                reference = f"{node.kind}/{node.ref}"
                if reference in lineage or len(lineage) >= 32:
                    raise ValueError("recursive boundary reference or nesting exceeds 32")
                desired[node.name] = self.child(obj, node.name, kind, spec)
                compiled_child = desired[node.name]
                desired[node.name] = evolve(
                    compiled_child,
                    metadata=evolve(
                        compiled_child.metadata,
                        annotations={**(compiled_child.metadata.annotations or {}), f"{GROUP}/lineage": json.dumps([*lineage, reference])},
                    ),
                )
                if ephemeral:
                    compiled_child = desired[node.name]
                    desired[node.name] = evolve(
                        compiled_child,
                        metadata=evolve(
                            compiled_child.metadata,
                            annotations={**(compiled_child.metadata.annotations or {}), f"{GROUP}/ephemeral": "true"},
                        ),
                    )
            desired[node.name] = trace_child(desired[node.name], obj, node, definition)
        children = await self.api.owned(namespace, meta["uid"])
        present_names = {child["metadata"]["name"] for child in children}
        for name, present_storage in persistence.items():
            if present_storage.enabled and desired[name].metadata.name in present_names:
                await self.storage_claim(namespace, present_storage)
        wanted = {item.metadata.name: (item.metadata.annotations or {})[f"{GROUP}/desired-hash"] for item in desired.values()}
        obsolete = [
            child
            for child in children
            if wanted.get(child["metadata"]["name"]) != child["metadata"].get("annotations", {}).get(f"{GROUP}/desired-hash")
        ]
        if obsolete:
            await self.status(
                obj, {"phase": "Draining", "ready": False, "completed": False, "failed": False, "observedGeneration": meta["generation"]}
            )
            for child in obsolete:
                if not child["metadata"].get("deletionTimestamp"):
                    await self.api.delete(child)
            raise Pending("draining removed or replaced nodes before admitting the new topology", phase="Draining")
        current = {child["metadata"]["name"]: child for child in children}
        states = {
            node.name: observed(current[desired[node.name].metadata.name])
            for node in graph.nodes
            if desired[node.name].metadata.name in current
        }
        facts = {f"{name}.{condition}": value for name, state in states.items() for condition, value in state.items()}
        used = sum(node.slots for node in graph.nodes if node.name in states and not states[node.name]["completed"])
        delays: dict[str, Any] = {name: None for name in obj.get("status", {}).get("delays", {})}
        for node in graph.nodes:
            if node.name in states:
                continue
            if not all(states.get(edge.node, {}).get(edge.condition, False) for edge in node.requires):
                continue
            if node.gate:
                definition = await self.definition("Gate", namespace, node.gate)
                gate_spec = definition["spec"]
                if ("expression" in gate_spec) == ("delaySeconds" in gate_spec):
                    raise ValueError("Gate requires exactly one of expression or delaySeconds")
                if "delaySeconds" in gate_spec:
                    delay = DelayGate(gate_spec["delaySeconds"])
                    token = {
                        "gate": [definition["metadata"]["uid"], definition["metadata"].get("generation", 1)],
                        "generation": meta["generation"],
                        "revision": (desired[node.name].metadata.annotations or {}).get(f"{GROUP}/desired-hash"),
                        "dependencies": [current[desired[edge.node].metadata.name]["metadata"]["uid"] for edge in node.requires],
                    }
                    record = obj.get("status", {}).get("delays", {}).get(node.name)
                    now = datetime.now(UTC)
                    if not record or record.get("token") != token:
                        delays[node.name] = {"token": token, "notBefore": (now + timedelta(seconds=delay.seconds)).isoformat()}
                        continue  # Persist the deadline, then require a refreshed observation.
                    delays[node.name] = record
                    if now < datetime.fromisoformat(record["notBefore"]):
                        continue
                else:
                    gate = converter.structure(gate_spec["expression"], Gate)
                    if gate.evaluate(facts) is not True:
                        continue
            if used + node.slots > graph.slots:
                continue
            storage = persistence.get(node.name)
            if storage and storage.enabled:
                await self.storage_claim(namespace, storage)
            await self.ensure(desired[node.name])
            used += node.slots
        failed = any(state["failed"] for state in states.values())
        complete = (
            graph.mode == "finite"
            and len(states) == len(graph.nodes)
            and all(
                states[node.name]["completed"]
                if node.kind in {"Workload", "Ephemeral", "Graph", "EphemeralGraph", "Feedback", "PolyGraph"}
                else states[node.name]["ready"]
                for node in graph.nodes
            )
        )
        ready = len(states) == len(graph.nodes) and all(state["ready"] or state["completed"] for state in states.values())
        await self.status(
            obj,
            {
                "phase": "Failed" if failed else "Completed" if complete else "Ready" if ready else "Reconciling",
                "ready": ready and not failed,
                "completed": complete,
                "failed": failed,
                "nodes": states,
                "delays": delays,
                "structuralRules": rule_reports,
                "observedGeneration": meta["generation"],
            },
        )

    async def feedback(self, obj: dict[str, Any]) -> None:
        """
        Run durable graph epochs; omitted rounds means recurrence until deletion or suspension.

        Args:
            obj (dict[str, Any]): Resource document from the latest API observation.

        Returns:
            None: No return value.
        """
        spec, meta = obj["spec"], obj["metadata"]
        if spec.get("suspend", False):
            if not await self.drain(obj):
                raise Pending("draining feedback", phase="Draining")
            await self.status(
                obj, {"phase": "Suspended", "ready": False, "completed": False, "failed": False, "observedGeneration": meta["generation"]}
            )
            return
        body = dict(spec["graph"])
        rule_reports = await check_rules(self.api, meta["namespace"], "Feedback", spec)
        body.pop("placement", None)
        if topology(body, spec.get("kind", "Graph")).mode != "finite" or body.get("templateOnly", False):
            raise ValueError("feedback epochs must be executable finite graphs")
        epoch = obj.get("status", {}).get("epoch", 0)
        children = await self.api.owned(meta["namespace"], meta["uid"])
        desired = self.child(obj, f"epoch-{epoch}", spec.get("kind", "Graph"), spec["graph"])
        obsolete = [
            child
            for child in children
            if child["metadata"]["name"] != desired.metadata.name
            or child["metadata"].get("annotations", {}).get(f"{GROUP}/desired-hash")
            != (desired.metadata.annotations or {})[f"{GROUP}/desired-hash"]
        ]
        if obsolete:
            for child in obsolete:
                await self.api.delete(child)
            raise Pending("waiting for previous epoch cleanup", phase="Draining")
        if spec.get("rounds") is not None and epoch >= spec["rounds"]:
            if not await self.drain(obj):
                raise Pending("waiting for final epoch cleanup", phase="Draining")
            await self.status(
                obj, {"phase": "Completed", "completed": True, "ready": True, "failed": False, "observedGeneration": meta["generation"]}
            )
            return
        previous = obj.get("status", {}).get("lastEpochTime")
        if previous and (datetime.now(UTC) - datetime.fromisoformat(previous)).total_seconds() < spec.get("intervalSeconds", 1):
            await self.status(
                obj, {"phase": "Waiting", "ready": False, "completed": False, "failed": False, "observedGeneration": meta["generation"]}
            )
            return
        instance = await self.ensure(desired)
        if instance:
            state = observed(instance)
            values: dict[str, Any] = {
                "structuralRules": rule_reports,
                "ready": state["ready"],
                "failed": state["failed"],
                "completed": False,
                "phase": "Failed" if state["failed"] else "Running",
                "observedGeneration": meta["generation"],
            }
            if state["completed"]:
                values.update(epoch=epoch + 1, lastEpochTime=datetime.now(UTC).isoformat())
            await self.status(obj, values)
        else:
            await self.status(
                obj, {"phase": "Reconciling", "ready": False, "completed": False, "failed": False, "observedGeneration": meta["generation"]}
            )
