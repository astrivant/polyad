"""
Describe the composition HTTP contract with apispec and reusable request schemas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apispec import APISpec

from polyad.compiler.passes.schema import structural_schema
from polyad_types.activation import ActivationPolicy
from polyad_types.capacity import CapacityPlan
from polyad_types.replication import ReplicaConnectivity
from polyad_types.requests import COMPOSITION_KINDS
from polyad_types.throughput import ThroughputSample

if TYPE_CHECKING:
    from typing import Any


def reference(name: str) -> dict[str, str]:
    """
    Reference a reusable schema component.

    Args:
        name (str): Component name within this document.

    Returns:
        dict[str, str]: Local JSON Reference.
    """
    return {"$ref": f"#/components/schemas/{name}"}


def schemas() -> dict[str, dict[str, Any]]:
    """
    Describe ID-addressed envelopes, topology references and audited resource identities.

    Returns:
        dict[str, dict[str, Any]]: Named OpenAPI 3.1 schema components.
    """
    free_object = {"type": "object", "additionalProperties": True}
    identifier = {"type": "string", "minLength": 1, "maxLength": 63, "pattern": "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"}
    identity_fields = {
        "requestId": reference("ID"),
        "name": {"type": "string"},
        "namespace": {"type": "string"},
        "uid": {"type": "string"},
        "status": reference("Observation"),
    }
    return {
        "ID": identifier,
        "ThroughputSample": structural_schema(ThroughputSample),
        "ActivationPolicy": structural_schema(ActivationPolicy),
        "ReplicaConnectivity": structural_schema(ReplicaConnectivity),
        "ActivationRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["requestId", "graph", "graphUid", "node"],
            "properties": {
                "requestId": reference("ID"),
                "graph": reference("ID"),
                "node": reference("ID"),
                "graphUid": {"type": "string", "minLength": 1, "maxLength": 128},
                "kind": {"type": "string", "enum": ["Graph", "PolyGraph", "ReplicaGroup"], "default": "Graph"},
            },
        },
        "ActivationReceipt": {
            "type": "object",
            "required": ["requestId", "uid", "status"],
            "properties": {
                **identity_fields,
                "target": free_object,
                "stopRequested": {"type": "boolean"},
            },
        },
        "Dependency": {
            "type": "object",
            "required": ["nodeId"],
            "additionalProperties": False,
            "properties": {
                "nodeId": reference("ID"),
                "condition": {"type": "string", "enum": ["started", "ready", "completed"], "default": "completed"},
            },
        },
        "Connection": {
            "type": "object",
            "required": ["sourceId", "targetId"],
            "additionalProperties": False,
            "properties": {"sourceId": reference("ID"), "targetId": reference("ID")},
        },
        "Node": {
            "type": "object",
            "required": ["id", "refId"],
            "additionalProperties": False,
            "description": "A graph vertex referencing a request-local definition. Reusing refId creates independent instances.",
            "properties": {
                "id": reference("ID"),
                "refId": reference("ID"),
                "gateId": reference("ID"),
                "slots": {"type": "integer", "minimum": 1, "default": 1},
                "requires": {"type": "array", "items": reference("Dependency")},
            },
        },
        "GraphSpec": {
            "type": "object",
            "required": ["nodes"],
            "additionalProperties": False,
            "properties": {
                "activation": reference("ActivationPolicy"),
                "capacity": structural_schema(CapacityPlan),
                "nodes": {"type": "array", "items": reference("Node")},
                "connections": {"type": "array", "items": reference("Connection")},
                "mode": {"type": "string", "enum": ["finite", "persistent"], "default": "finite"},
                "slots": {"type": "integer", "minimum": 1, "default": 64},
                "suspend": {"type": "boolean", "default": False},
                "templateOnly": {"type": "boolean", "description": "Compiler-controlled: all definitions except the root are templates."},
                "shutdownPolicyId": reference("ID"),
                "placement": {
                    **free_object,
                    "description": (
                        "Graph placement with nodeSelector, tolerations, nodeAffinity and enforce; enforced constraints "
                        "reach descendant Pod manifests. "
                    ),
                },
                "rules": {
                    "type": "array",
                    "maxItems": 32,
                    "uniqueItems": True,
                    "items": {"type": "string", "maxLength": 253},
                    "description": "Additional namespace GraphRule names inherited by descendants; namespace-wide rules always apply.",
                },
            },
        },
        "ReplicaGroupSpec": {
            "type": "object",
            "required": ["template"],
            "properties": {
                "template": {
                    "type": "object",
                    "required": ["refId"],
                    "properties": {"refId": reference("ID")},
                    "additionalProperties": False,
                },
                "replicas": {"type": "integer", "minimum": 0, "maximum": 256, "default": 1},
                "minReplicas": {"type": "integer", "minimum": 0, "default": 0},
                "maxReplicas": {"type": "integer", "minimum": 1, "maximum": 256, "default": 32},
                "connectivity": reference("ReplicaConnectivity"),
                "placement": free_object,
                "network": free_object,
                "rules": {"type": "array", "items": reference("ID")},
            },
        },
        "CompositionItem": {
            "type": "object",
            "required": ["id", "kind", "spec"],
            "additionalProperties": False,
            "properties": {"id": reference("ID"), "kind": {"type": "string", "enum": sorted(COMPOSITION_KINDS)}, "spec": free_object},
            "oneOf": [
                {"properties": {"kind": {"enum": ["Graph", "PolyGraph"]}, "spec": reference("GraphSpec")}},
                {"properties": {"kind": {"const": "ReplicaGroup"}, "spec": reference("ReplicaGroupSpec")}},
                {
                    "properties": {
                        "kind": {"enum": sorted(COMPOSITION_KINDS - {"Graph", "PolyGraph", "ReplicaGroup"})},
                        "spec": {
                            **free_object,
                            "description": (
                                "Reusable definition specification for the selected Polyad CR kind, including native Pod "
                                "templates where applicable. The operator and Kubernetes validate kind-specific constraints "
                                "before scheduling. "
                            ),
                        },
                    }
                },
            ],
        },
        "CompositionRequest": {
            "type": "object",
            "required": ["requestId", "rootId", "objects"],
            "additionalProperties": False,
            "description": (
                "Immutable intent, at most 1 MiB. IDs are unique within objects; rootId identifies a graph "
                "boundary; all definitions must be reachable. Cross-reference and namespace policies are "
                "validated separately. "
            ),
            "properties": {
                "requestId": reference("ID"),
                "rootId": reference("ID"),
                "objects": {"type": "array", "minItems": 1, "maxItems": 128, "items": reference("CompositionItem")},
            },
        },
        "Observation": {
            **free_object,
            "description": (
                "Eventual operator observation. A durable receipt may still be Pending, Invalid or waiting on "
                "policy. The root graph's status carries recursive graph metrics. "
            ),
            "properties": {
                "phase": {"type": "string"},
                "ready": {"type": "boolean"},
                "completed": {"type": "boolean"},
                "failed": {"type": "boolean"},
                "message": {"type": "string"},
                "observedGeneration": {"type": "integer"},
                "objects": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "required": ["kind", "name", "uid"],
                        "properties": {"kind": {"type": "string"}, "name": {"type": "string"}, "uid": {"type": ["string", "null"]}},
                    },
                },
            },
        },
        "CompositionStatus": {"type": "object", "required": list(identity_fields), "properties": identity_fields},
        "Receipt": {
            "allOf": [
                reference("CompositionStatus"),
                {"type": "object", "required": ["kind"], "properties": {"kind": {"const": "Composition"}}},
            ]
        },
        "ResourceReference": {
            "type": "object",
            "required": ["kind", "name", "uid", "namespace", "generation", "owners", "trace"],
            "properties": {
                "kind": {"type": "string"},
                "name": {"type": "string"},
                "uid": {"type": "string"},
                "namespace": {"type": "string"},
                "generation": {"type": ["integer", "null"]},
                "owners": {"type": "array", "items": free_object},
                "trace": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Request, composition UID, definition UID/generation and node-path annotations from the actual manifest. "
                    ),
                },
            },
        },
        "Audit": {
            "allOf": [
                reference("CompositionStatus"),
                {
                    "type": "object",
                    "required": ["resources", "truncated"],
                    "properties": {
                        "resources": {"type": "array", "items": reference("ResourceReference")},
                        "truncated": {
                            "type": "boolean",
                            "description": "At least one kind has more than 200 matching resources; results are bounded, not historical.",
                        },
                    },
                },
            ]
        },
        "Error": {"type": "object", "required": ["error"], "properties": {"error": {"type": "string"}, "retry": {"type": "string"}}},
    }


def openapi_document(title: str, version: str) -> dict[str, Any]:
    """
    Generate the authenticated service contract, including the schema endpoint itself.

    Args:
        title (str): Service title configured by the builder.
        version (str): HTTP API contract version configured by the builder.

    Returns:
        dict[str, Any]: Serializable OpenAPI 3.1 document.
    """
    spec = APISpec(
        title=title,
        version=version,
        openapi_version="3.1.0",
        info={
            "description": (
                "Compose Kubernetes workload graphs by ID and audit generated resources. A 202 receipt "
                "acknowledges durable intent, not admission or execution. "
            )
        },
        security=[{"bearerAuth": []}],
    )
    spec.components.security_scheme(
        "bearerAuth", {"type": "http", "scheme": "bearer", "description": "Namespace-scoped token from the configured Kubernetes Secret."}
    )
    for name, component in schemas().items():
        spec.components.schema(name, component)

    def response(description: str, schema: str) -> dict[str, Any]:
        return {"description": description, "content": {"application/json": {"schema": reference(schema)}}}

    errors = {
        "401": response("Missing or invalid bearer token.", "Error"),
        "422": response("Invalid IDs, reference structure or request fields.", "Error"),
        "429": {
            **response("Shared shard request budget exhausted; retry with the same requestId after Retry-After.", "Error"),
            "headers": {"Retry-After": {"description": "Seconds until retry is allowed.", "schema": {"type": "integer", "minimum": 0}}},
        },
        "503": response("Acknowledgement or rate-limit storage unavailable; retry identical intent with the same requestId.", "Error"),
    }
    spec.path(
        path="/v1/compositions",
        operations={
            "post": {
                "operationId": "submitComposition",
                "summary": "Submit immutable composition intent",
                "tags": ["Compositions"],
                "requestBody": {"required": True, "content": {"application/json": {"schema": reference("CompositionRequest")}}},
                "responses": {
                    **errors,
                    "202": response("Durable receipt, including matching duplicate submissions.", "Receipt"),
                    "400": response("Malformed JSON.", "Error"),
                    "409": response("requestId already identifies different or deleting intent.", "Error"),
                    "413": response("Request exceeds 1 MiB.", "Error"),
                    "415": response("Content-Type must be application/json.", "Error"),
                },
            }
        },
    )
    parameter = {"name": "request_id", "in": "path", "required": True, "schema": reference("ID")}
    for suffix, operation, summary, schema in [
        ("", "getComposition", "Read current composition status", "CompositionStatus"),
        ("/resources", "getCompositionResources", "Trace generated manifest identities", "Audit"),
    ]:
        spec.path(
            path=f"/v1/compositions/{{request_id}}{suffix}",
            parameters=[parameter],
            operations={
                "get": {
                    "operationId": operation,
                    "summary": summary,
                    "tags": ["Compositions"],
                    "responses": {
                        **errors,
                        "200": response(summary, schema),
                        "404": response("No current receipt with this requestId.", "Error"),
                    },
                }
            },
        )
    spec.path(
        path="/v1/activations",
        operations={
            "post": {
                "operationId": "submitActivation",
                "summary": "Submit a bounded downstream pulse",
                "requestBody": {"required": True, "content": {"application/json": {"schema": reference("ActivationRequest")}}},
                "responses": {
                    **errors,
                    "202": response("Durable pulse receipt; policy admission is asynchronous.", "ActivationReceipt"),
                    "409": response("Conflicting request identity or unavailable graph incarnation.", "Error"),
                    "400": response("Malformed JSON.", "Error"),
                    "413": response("Request exceeds 1 MiB.", "Error"),
                    "415": response("Content-Type must be application/json.", "Error"),
                },
            }
        },
    )
    for suffix, method, operation, code in (("", "get", "getActivation", "200"), ("/stop", "post", "stopActivation", "202")):
        spec.path(
            path=f"/v1/activations/{{request_id}}{suffix}",
            parameters=[parameter],
            operations={
                method: {
                    "operationId": operation,
                    "responses": {
                        **errors,
                        code: response("Current pulse receipt.", "ActivationReceipt"),
                        "404": response("Activation receipt not found.", "Error"),
                    },
                }
            },
        )
    spec.path(
        path="/openapi.json",
        operations={
            "get": {
                "operationId": "getOpenAPI",
                "summary": "Read the OpenAPI 3.1 service schema",
                "tags": ["Schema"],
                "responses": {
                    "200": {
                        "description": "OpenAPI document.",
                        "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}},
                    },
                    "401": errors["401"],
                    "429": errors["429"],
                    "503": errors["503"],
                },
            }
        },
    )
    spec.path(
        path="/v1/throughput",
        operations={
            "post": {
                "operationId": "reportThroughput",
                "summary": "Report aggregate application demand and completed work for an assigned graph",
                "requestBody": {"required": True, "content": {"application/json": {"schema": reference("ThroughputSample")}}},
                "responses": {
                    **errors,
                    "202": {"description": "Fresh measurement accepted; feedback evaluation is asynchronous."},
                    "403": response("Graph tree is not assigned to this credential.", "Error"),
                    "409": response("Graph revision changed or the observation is out of order.", "Error"),
                },
            }
        },
    )
    return spec.to_dict()
