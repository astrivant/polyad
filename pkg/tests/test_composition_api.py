"""
Exercise ID compilation, HTTP intake and queued manifest provenance.
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.api import create_app
from polyad.api.composition.store import CompositionStore
from polyad.compiler.passes.composition import compile_composition, read_receipt, receipt_spec, request_name
from polyad.exceptions.api import Conflict
from polyad.exceptions.policies import PolicyViolation
from polyad.exceptions.reconciliation import Pending
from polyad.operator.coordination.leases import Coordinator
from polyad.operator.reconciliation.controller import Controller
from polyad_types import resources as asts
from polyad_types.api.requests import CompositionRequest
from polyad_types.serialization import converter
from tests.test_operator import FakeAPI, resource, template


def document():
    """
    Reuse a graph definition at two nodes of a higher-order root.
    """
    return {
        "requestId": "request-one",
        "rootId": "root",
        "objects": [
            {"id": "work", "kind": "Workload", "spec": {"template": template()}},
            {"id": "subgraph", "kind": "Graph", "spec": {"nodes": [{"id": "execute", "refId": "work"}]}},
            {
                "id": "root",
                "kind": "PolyGraph",
                "spec": {"nodes": [{"id": "left", "refId": "subgraph"}, {"id": "right", "refId": "subgraph"}]},
            },
        ],
    }


def request_value(data=None):
    """
    Structure an HTTP-like request into the public attrs model.
    """
    return converter.structure(data or document(), CompositionRequest)


def test_compiler_resolves_ids_and_reuses_templates():
    """
    Keep definition and node instance identities separate across repeated subgraphs.
    """
    request = request_value()
    manifests = compile_composition(request, "test", owner_uid="receipt-uid")
    root = asts.to_document(manifests["root"])
    assert root["spec"]["nodes"][0]["ref"] == root["spec"]["nodes"][1]["ref"] == manifests["subgraph"].metadata.name
    assert root["spec"]["nodes"][0]["id"] == "left"
    assert manifests["subgraph"].spec["templateOnly"]
    assert not manifests["root"].spec["templateOnly"]
    assert list(manifests)[-1] == "root"
    assert root["metadata"]["ownerReferences"][0]["uid"] == "receipt-uid"
    assert read_receipt(receipt_spec(request)).digest() == request.digest()
    reordered = document()
    reordered["objects"].reverse()
    assert request.digest() == request_value(reordered).digest()
    other = document()
    other["requestId"] = "request-two"
    assert compile_composition(request_value(other), "test")["root"].metadata.name != manifests["root"].metadata.name


@pytest.mark.parametrize("change", ["missing", "duplicate", "recursive", "unreachable", "native", "bypass"])
def test_compiler_rejects_ambiguous_or_unsafe_composition(change):
    """
    Reject dangling, recursive and out-of-surface references before receipt creation.
    """
    data = document()
    if change == "missing":
        data["objects"][1]["spec"]["nodes"][0]["refId"] = "missing"
    elif change == "duplicate":
        data["objects"].append(copy.deepcopy(data["objects"][0]))
    elif change == "recursive":
        data["objects"][1]["spec"]["nodes"][0]["refId"] = "root"
    elif change == "unreachable":
        data["objects"].append({"id": "unused", "kind": "Workload", "spec": {"template": template()}})
    elif change == "native":
        data["objects"][0]["kind"] = "Deployment"
    else:
        data["objects"][1]["spec"]["nodes"][0]["gate"] = "outside-request"
    with pytest.raises((ValueError, TypeError)):
        compile_composition(request_value(data), "test")


def test_flask_authentication_validation_and_idempotency_errors():
    """
    Expose JSON-only submissions, bounded bodies and stable conflict responses.
    """
    accepted = []

    def submit(value):
        if accepted:
            raise Conflict("different intent")
        accepted.append(value)
        return {"requestId": value.requestId, "uid": "stored"}

    app = create_app(submit, lambda key, audit: {"requestId": key, "resources": []} if audit else None, token="secret")
    client = app.test_client()
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/v1/compositions", json=document()).status_code == 401
    assert client.post("/v1/compositions", json=document(), headers=headers).status_code == 202
    assert client.post("/v1/compositions", json=document(), headers=headers).status_code == 409
    assert client.post("/v1/compositions", json={"bad": True}, headers=headers).status_code == 422
    assert client.post("/v1/compositions", data="{", content_type="application/json", headers=headers).status_code == 400
    assert (
        client.post("/v1/compositions", data="x" * (1024 * 1024 + 1), content_type="application/json", headers=headers).status_code == 413
    )
    assert client.get("/v1/compositions/request-one", headers=headers).status_code == 404
    assert client.get("/v1/compositions/request-one/resources", headers=headers).json["resources"] == []
    with pytest.raises(ValueError, match="token"):
        create_app(submit, lambda *_: None, token="")


def test_receipt_retries_recover_lost_acknowledgements_and_conflicts():
    """
    A retry on any replica resolves to one durable UID even after an uncertain POST.
    """

    async def scenario():
        api = FakeAPI()
        store = CompositionStore(api, "test")
        api.fail_create_after_commit = True
        with pytest.raises(ApiException):
            await store.submit(request_value())
        receipt = await store.submit(request_value())
        again = await CompositionStore(api, "test").submit(request_value())
        assert again["uid"] == receipt["uid"]
        assert len(api.objects) == 1
        different = document()
        different["objects"][0]["spec"]["template"]["spec"]["containers"][0]["image"] = "changed"
        with pytest.raises(Conflict):
            await store.submit(request_value(different))

    asyncio.run(scenario())


def test_queued_materialization_policy_and_audit_lineage():
    """
    Preflight before templates, observe before root, and trace nested Jobs into Pod metadata.
    """

    async def scenario():
        request = request_value()
        receipt = resource("Composition", request_name(request.requestId), receipt_spec(request))
        rule = resource("GraphPolicy", "budget", {"limits": {"expandedNodes": 3}})
        api = FakeAPI(receipt, rule)
        controller = Controller(api)
        key = ("Composition", "test", receipt["metadata"]["name"])
        with pytest.raises(PolicyViolation, match="expandedNodes=4"):
            await controller.reconcile(key)
        assert not any(call[0] == "POST" for call in api.calls)
        api.objects[("GraphPolicy", "test", "budget")]["spec"]["limits"]["expandedNodes"] = 4
        with pytest.raises(Pending, match="definitions"):
            await controller.reconcile(key)
        assert not any(k[0] == "PolyGraph" for k in api.objects)
        await controller.reconcile(key)
        for _ in range(8):
            for child_key in list(api.objects):
                if child_key[0] not in asts.BOUNDARY_KINDS:
                    continue
                try:
                    await controller.reconcile(child_key)
                except Pending:
                    pass
        jobs = [value for key, value in api.objects.items() if key[0] == "Job"]
        assert len(jobs) == 2
        paths = {job["metadata"]["annotations"][f"{asts.GROUP}/node-path"] for job in jobs}
        assert paths == {"root/left/execute", "root/right/execute"}
        for job in jobs:
            annotations = job["spec"]["template"]["metadata"]["annotations"]
            assert annotations[f"{asts.GROUP}/request-id"] == "request-one"
            assert annotations[f"{asts.GROUP}/composition-uid"] == receipt["metadata"]["uid"]
            assert annotations[f"{asts.GROUP}/definition-uid"]
            env = {item["name"]: item.get("value") for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}
            assert env["POLYAD_REQUEST_ID"] == "request-one"
            assert env["POLYAD_COMPOSITION_UID"] == receipt["metadata"]["uid"]
            assert env["POLYAD_NODE_PATH"] == annotations[f"{asts.GROUP}/node-path"]
        await controller.reconcile(key)
        audit = await CompositionStore(api, "test").lookup(request.requestId, True)
        assert len([item for item in audit["resources"] if item["kind"] == "Job"]) == 2
        coordinator = Coordinator(api, "test")
        assert await coordinator.shard_for(key) == await coordinator.shard_for(("Job", "test", jobs[0]["metadata"]["name"]))
        api.objects[key]["metadata"]["deletionTimestamp"] = "now"
        with pytest.raises(Pending):
            await controller.reconcile(key)
        deleting = [obj for obj in api.objects.values() if obj["metadata"].get("deletionTimestamp") and obj["kind"] != "Composition"]
        assert len(deleting) == 1 and deleting[0]["kind"] == "PolyGraph"

    asyncio.run(scenario())


def test_api_builder_branches_configuration_and_validates_before_build():
    """
    Configure isolated service instances without leaking credentials or builder mutations.
    """
    from polyad.api import APIBuilder

    base = APIBuilder().with_handlers(lambda value: {"requestId": value.requestId}, lambda *_: None)
    with pytest.raises(ValueError, match="token"):
        base.build()
    with pytest.raises(ValueError, match="handlers"):
        APIBuilder().with_bearer_token("secret").build()
    configured = base.with_bearer_token("secret")
    assert "secret" not in repr(configured)
    first = configured.build()
    second = base.with_bearer_token("other").build()
    assert first is not second and not base.token
    assert first.test_client().get("/v1/compositions/one", headers={"Authorization": "Bearer secret"}).status_code == 404
    assert second.test_client().get("/v1/compositions/one", headers={"Authorization": "Bearer secret"}).status_code == 401
