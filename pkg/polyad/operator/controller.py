"""Translate refreshed graph intent into owned Kubernetes execution resources."""

import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from attrs import evolve

from polyad.graph.gates import Gate
from polyad.graph.topology import converter, topology
from polyad.operator.api import API, GROUP
from polyad.operator.compiler import asts
from polyad.operator.compiler.children import child_name as compile_child_name
from polyad.operator.compiler.children import owned_child
from polyad.operator.placement import merge_placement, place_pod
from polyad.operator.queue import Key

BOUNDARIES = asts.BOUNDARY_KINDS
FINALIZER = f"{GROUP}/drain"


class Pending(Exception):
    """Require a later refreshed observation before proceeding."""


def observed(obj: dict[str, Any]) -> dict[str, bool]:
    """Distinguish creation, readiness, completion and failure without stale rollout readiness."""
    status, spec = obj.get("status", {}), obj.get("spec", {})
    conditions = {c["type"]: c["status"] == "True" for c in status.get("conditions", [])}
    ready = completed = failed = False
    if obj["kind"] == "Job":
        completed, failed = conditions.get("Complete", False), conditions.get("Failed", False)
        ready = bool(status.get("active", 0)) or completed
    elif obj["kind"] == "Deployment":
        ready = (
            status.get("observedGeneration", 0) >= obj["metadata"].get("generation", 1)
            and status.get("updatedReplicas", 0) == spec.get("replicas", 1)
            and status.get("readyReplicas", 0) >= spec.get("replicas", 1)
            and status.get("availableReplicas", 0) >= spec.get("replicas", 1)
        )
    elif obj["kind"] in BOUNDARIES:
        current = status.get("observedGeneration") == obj["metadata"].get("generation", 1)
        ready, completed, failed = (current and status.get(k, False) for k in ("ready", "completed", "failed"))
    elif obj["kind"] == "PersistentVolumeClaim":
        ready = status.get("phase") == "Bound"
    else:
        ready = True
    if obj["metadata"].get("deletionTimestamp"):
        ready = completed = False
    return {"started": not bool(obj["metadata"].get("deletionTimestamp")), "ready": ready, "completed": completed, "failed": failed}


def child_name(parent: dict[str, Any], node: str) -> str:
    """Keep addresses stable across replacements while names remain unique within a boundary."""
    return compile_child_name(asts.converter.structure(parent["metadata"], asts.ObjectMeta), node)


def references(value: Any, names: dict[str, str]) -> Any:
    """Resolve explicit ${nodes.NAME.name} references in container and resource definitions."""
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
    """Reconcile one namespace-scoped boundary per ordered queue turn."""

    def __init__(self, api: API) -> None:
        """Bind the API adapter for production or deterministic tests."""
        self.api = api

    async def status(self, obj: dict[str, Any], values: dict[str, Any]) -> None:
        """Commit observations only against the resource version that produced them."""
        meta = obj["metadata"]
        if all(obj.get("status", {}).get(key) == value for key, value in values.items()):
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
        """Refresh a referenced definition; absent or deleting definitions block admission."""
        obj = await self.api.get(kind, namespace, name)
        if obj is None or obj["metadata"].get("deletionTimestamp"):
            raise Pending(f"waiting for {kind}/{name}")
        return obj

    async def drain(self, obj: dict[str, Any]) -> bool:
        """Release children before parent finalizers, preserving custom child cleanup gates."""
        meta = obj["metadata"]
        children = await self.api.owned(meta["namespace"], meta["uid"])
        for child in children:
            if not child["metadata"].get("deletionTimestamp"):
                await self.api.delete(child)
        return not children

    async def reconcile(self, key: Key) -> None:
        """Read current intent, fence deletion, then execute a single idempotent pass."""
        kind, namespace, name = key
        if kind == "Health":
            await self.api.request("GET", "Graph", namespace)
            return
        obj = await self.api.get(kind, namespace, name)
        if obj is None:
            return
        if obj["metadata"].get("deletionTimestamp"):
            if not await self.drain(obj):
                raise Pending("waiting for owned resources and their finalizers")
            if FINALIZER in obj["metadata"].get("finalizers", []):
                await self.finalizers(obj, remove=True)
            return
        if kind in BOUNDARIES | {"Rewrite"} and FINALIZER not in obj["metadata"].get("finalizers", []):
            await self.finalizers(obj)
            raise Pending("drain finalizer persisted; refresh before admission")
        if obj.get("spec", {}).get("templateOnly", False):
            return
        if kind == "Rewrite":
            await self.rewrite(obj)
        elif kind == "Feedback":
            await self.feedback(obj)
        elif kind in {"Graph", "EphemeralGraph"}:
            await self.graph(obj)

    async def finalizers(self, obj: dict[str, Any], *, remove: bool = False) -> None:
        """Change only our finalizer through the same guarded queue as graph writes."""
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
        """Apply a full declarative topology replacement once at an expected generation."""
        if obj.get("status", {}).get("applied"):
            return
        spec, meta = obj["spec"], obj["metadata"]
        topology({key: value for key, value in spec["topology"].items() if key != "placement"})
        target = await self.definition(spec.get("kind", "Graph"), meta["namespace"], spec["graph"])
        # This annotation is committed atomically with the spec and survives a status-write timeout.
        token = meta["uid"]
        if target["metadata"].get("annotations", {}).get(f"{GROUP}/rewrite") != token:
            if target["metadata"]["generation"] != spec["expectedGeneration"]:
                raise ValueError("rewrite target generation changed")
            target_ast = asts.from_document(target)
            if not isinstance(target_ast, (asts.Graph, asts.EphemeralGraph)):
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
        """Compile a resource AST with stable revision hashes and typed controller ownership."""
        return owned_child(asts.from_document(parent), node_name, kind, spec, extra=extra)

    async def ensure(self, desired: asts.Resource) -> dict[str, Any] | None:
        """Create once; never adopt a same-name object owned by somebody else."""
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
                raise Pending("waiting for resource replacement")
            return current
        await self.api.request("POST", kind, meta.namespace, body=desired)
        return None  # Creation acknowledgement is not readiness; observe it on a fresh pass.

    async def graph(self, obj: dict[str, Any]) -> None:
        """Admit nodes by observed edge predicates and reserved slots; drain structural changes first."""
        raw = dict(obj["spec"])
        placement = raw.pop("placement", None)
        graph = topology(raw)
        meta, namespace = obj["metadata"], obj["metadata"]["namespace"]
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
                    "observedGeneration": meta["generation"],
                },
            )
            return
        desired: dict[str, asts.Resource] = {}
        names = {node.name: child_name(obj, node.name) for node in graph.nodes}
        for node in graph.nodes:
            definition = await self.definition(node.kind, namespace, node.ref)
            spec = (
                references(copy.deepcopy(definition["spec"]), names) if node.kind not in BOUNDARIES else copy.deepcopy(definition["spec"])
            )
            if node.kind in {"Workload", "Ephemeral", "Daemon"}:
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
                if placement:
                    if node.kind == "Feedback":
                        spec["graph"]["placement"] = merge_placement(placement, spec["graph"].get("placement"))
                        if obj["kind"] == "EphemeralGraph":
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
        children = await self.api.owned(namespace, meta["uid"])
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
            raise Pending("draining removed or replaced nodes before admitting the new topology")
        current = {child["metadata"]["name"]: child for child in children}
        states = {
            node.name: observed(current[desired[node.name].metadata.name])
            for node in graph.nodes
            if desired[node.name].metadata.name in current
        }
        facts = {f"{name}.{condition}": value for name, state in states.items() for condition, value in state.items()}
        used = sum(node.slots for node in graph.nodes if node.name in states and not states[node.name]["completed"])
        for node in graph.nodes:
            if node.name in states or used + node.slots > graph.slots:
                continue
            if not all(states.get(edge.node, {}).get(edge.condition, False) for edge in node.requires):
                continue
            if node.gate:
                gate_spec = (await self.definition("Gate", namespace, node.gate))["spec"]
                gate = converter.structure(gate_spec["expression"], Gate)
                if gate.evaluate(facts) is not True:
                    continue
            await self.ensure(desired[node.name])
            used += node.slots
        failed = any(state["failed"] for state in states.values())
        complete = (
            graph.mode == "finite"
            and len(states) == len(graph.nodes)
            and all(
                states[node.name]["completed"]
                if node.kind in {"Workload", "Ephemeral", "Graph", "EphemeralGraph", "Feedback"}
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
                "observedGeneration": meta["generation"],
            },
        )

    async def feedback(self, obj: dict[str, Any]) -> None:
        """Run durable graph epochs; omitted rounds means recurrence until deletion or suspension."""
        spec, meta = obj["spec"], obj["metadata"]
        if spec.get("suspend", False):
            if not await self.drain(obj):
                raise Pending("draining feedback")
            await self.status(obj, {"phase": "Suspended", "ready": False, "completed": False, "observedGeneration": meta["generation"]})
            return
        body = dict(spec["graph"])
        body.pop("placement", None)
        if topology(body).mode != "finite" or body.get("templateOnly", False):
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
            raise Pending("waiting for previous epoch cleanup")
        if spec.get("rounds") is not None and epoch >= spec["rounds"]:
            if not await self.drain(obj):
                raise Pending("waiting for final epoch cleanup")
            await self.status(obj, {"phase": "Completed", "completed": True, "ready": True, "observedGeneration": meta["generation"]})
            return
        previous = obj.get("status", {}).get("lastEpochTime")
        if previous and (datetime.now(UTC) - datetime.fromisoformat(previous)).total_seconds() < spec.get("intervalSeconds", 1):
            return
        instance = await self.ensure(desired)
        if instance:
            state = observed(instance)
            values: dict[str, Any] = {
                "ready": state["ready"],
                "failed": state["failed"],
                "completed": False,
                "phase": "Failed" if state["failed"] else "Running",
                "observedGeneration": meta["generation"],
            }
            if state["completed"]:
                values.update(epoch=epoch + 1, lastEpochTime=datetime.now(UTC).isoformat())
            await self.status(obj, values)
