"""
Verify typed resource compilation preserves Kubernetes wire contracts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from unittest.mock import Mock

import pytest

from polyad.compiler.passes.children import owned_child
from polyad.operator.api import API
from polyad_types import resources as asts
from polyad_types.resources.resources import RESOURCE_CLASSES


def parent():
    """
    Provide a persisted graph boundary for owned resources.
    """
    return asts.Graph(metadata=asts.ObjectMeta(name="graph", namespace="test", uid="12345678-abcd"), spec={})


def execution_spec(kind):
    """
    Provide representative native execution settings including extension fields.
    """
    template = {
        "metadata": {"labels": {"app": "test"}},
        "spec": {"containers": [{"name": "main", "image": "test", "stdin": False}], "restartPolicy": "Never"},
    }
    if kind == "Job":
        return {"template": template, "backoffLimit": 0, "ttlSecondsAfterFinished": 20}
    if kind == "Deployment":
        return {
            "template": template,
            "replicas": 0,
            "selector": {"matchLabels": {"app": "test"}},
            "strategy": {"type": "Recreate"},
        }
    if kind == "StatefulSet":
        return asts.to_document(
            asts.StatefulSetSpec(
                template=asts.converter.structure(template, asts.PodTemplate),
                replicas=2,
                selector=asts.LabelSelector(matchLabels={"app": "test"}),
                serviceName="headless",
                volumeClaimTemplates=(
                    {
                        "metadata": {"name": "data"},
                        "spec": {
                            "accessModes": ["ReadWriteOnce"],
                            "resources": {"requests": {"storage": "10Gi"}},
                            "storageClassName": "durable",
                        },
                    },
                ),
            )
        )
    return {}


@pytest.mark.parametrize("cls", RESOURCE_CLASSES, ids=lambda cls: cls.resource_type.kind)
def test_resource_roundtrip(cls):
    """
    Every supported kind retains native and server fields through cattrs.
    """
    descriptor = cls.resource_type
    document = {
        "apiVersion": descriptor.api_version,
        "kind": descriptor.kind,
        "metadata": {
            "name": "example",
            "namespace": "test",
            "resourceVersion": "42",
            "managedFields": [{"manager": "kopf", "fieldsV1": {}}],
            "ownerReferences": [{"apiVersion": "v1", "kind": "Pod", "name": "p", "uid": "u", "controller": False}],
        },
        "status": {"unknown": None},
        "nativeExtension": {"enabled": False},
    }
    if cls is asts.ConfigMap:
        document.update(data={}, binaryData={"key": "YQ=="}, immutable=False)
    elif cls is asts.PodTemplateResource:
        document["template"] = execution_spec("Job")["template"]
    else:
        document["spec"] = execution_spec(descriptor.kind)
    model = asts.from_document(document)
    assert isinstance(model, cls)
    assert asts.to_document(model) == document
    output = asts.to_document(model)
    output["metadata"]["managedFields"].clear()
    document["metadata"]["managedFields"].clear()
    assert asts.to_document(model)["metadata"]["managedFields"]
    without_identity = asts.to_document(model)
    del without_identity["apiVersion"]
    del without_identity["kind"]
    assert asts.from_document(without_identity, kind=descriptor.kind) == model


@pytest.mark.parametrize(
    "kind", ["Job", "Deployment", "StatefulSet", "ConfigMap", "Service", "PersistentVolumeClaim", "Graph", "PolyGraph"]
)
def test_child_wire_compatibility(kind):
    """
    Keep the original child payload and digest to avoid replacing live workloads.
    """
    spec = execution_spec(kind)
    extra = {"data": {"key": "value"}, "immutable": False} if kind == "ConfigMap" else None
    child = owned_child(parent(), "worker", kind, spec, extra=extra)
    digest = hashlib.sha256(json.dumps([kind, spec, extra], sort_keys=True).encode()).hexdigest()[:12]
    expected = {
        "apiVersion": child.resource_type.api_version,
        "kind": kind,
        "metadata": {
            "name": "graph-worker-12345678-" + hashlib.sha256(b"worker").hexdigest()[:8],
            "namespace": "test",
            "annotations": {f"{asts.GROUP}/desired-hash": digest},
            "labels": {f"{asts.GROUP}/owner": "12345678-abcd", f"{asts.GROUP}/node": "worker"},
            "ownerReferences": [
                {
                    "apiVersion": f"{asts.GROUP}/{asts.VERSION}",
                    "kind": "Graph",
                    "name": "graph",
                    "uid": "12345678-abcd",
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ],
        },
    }
    if kind == "ConfigMap":
        expected.update(extra)
    else:
        expected["spec"] = spec
    assert asts.to_document(child) == expected
    if kind in {"Job", "Deployment", "StatefulSet"}:
        typed_spec = asts.converter.structure(
            spec, {"Job": asts.JobSpec, "Deployment": asts.DeploymentSpec, "StatefulSet": asts.StatefulSetSpec}[kind]
        )
        assert owned_child(parent(), "worker", kind, typed_spec) == child


def test_identity_and_extension_guards():
    """
    Reject unknown identities and extension fields that could replace ownership.
    """
    document = asts.to_document(parent())
    with pytest.raises(ValueError, match="API version"):
        asts.from_document({**document, "apiVersion": "wrong/v1"})
    with pytest.raises(ValueError, match="unsupported"):
        asts.from_document({**document, "kind": "Unknown"})
    with pytest.raises(ValueError, match="API version"):
        asts.from_document(document, kind="Daemon")
    with pytest.raises(ValueError, match="override"):
        owned_child(parent(), "worker", "Graph", {}, extra={"metadata": {}})
    with pytest.raises(ValueError, match="override"):
        asts.to_document(asts.ObjectMeta(name="x", extra={"name": "y"}))
    with pytest.raises(ValueError, match="no spec"):
        owned_child(parent(), "worker", "ConfigMap", {"data": {}})
    with pytest.raises(ValueError, match="not a spec"):
        asts.from_document({"kind": "ConfigMap", "metadata": {"name": "x"}, "spec": {}})


def test_api_serializes_resources_and_fences():
    """
    Exercise the actual API adapter without requiring credentials or a cluster.
    """
    api = API.__new__(API)
    api.client = Mock()
    child = owned_child(parent(), "worker", "Job", execution_spec("Job"))
    asyncio.run(api.request("POST", "Job", "test", body=child))
    assert api.client.call_api.call_args.kwargs["body"] == asts.to_document(child)
    assert api.client.call_api.call_args.args == ("/apis/batch/v1/namespaces/test/jobs", "POST")
    patch = asts.StatusPatch(metadata=asts.ObjectMeta(resourceVersion="42"), status={"ready": False})
    asyncio.run(api.request("PATCH", "Graph", "test", "graph", patch, status=True))
    assert api.client.call_api.call_args.kwargs["body"] == {"metadata": {"resourceVersion": "42"}, "status": {"ready": False}}
    asyncio.run(api.delete(asts.to_document(parent())))
    assert api.client.call_api.call_args.kwargs["body"] == {
        "preconditions": {"uid": "12345678-abcd"},
        "propagationPolicy": "Foreground",
    }
