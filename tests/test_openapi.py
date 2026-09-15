"""Check the served OpenAPI document against routes, validators and actual JSON payloads."""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest
from openapi_spec_validator import validate

from polyad.api import APIBuilder
from polyad.compiler.composition import COMPOSITION_KINDS
from tests.test_composition_api import document


def test_openapi_endpoint_documents_real_routes_and_request_shapes():
    """Serve a valid authenticated document describing each live method and the ID request model."""
    app = (
        APIBuilder()
        .with_handlers(lambda value: {"requestId": value.requestId}, lambda *_: None)
        .with_bearer_token("secret")
        .with_metadata("Team Scheduler", "1.2.0")
        .build()
    )
    client = app.test_client()
    assert client.get("/openapi.json").status_code == 401
    response = client.get("/openapi.json", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200 and response.content_type == "application/json"
    spec = response.json
    validate(spec)
    assert spec["openapi"] == "3.1.0"
    assert spec["info"]["title"] == "Team Scheduler" and spec["info"]["version"] == "1.2.0"
    assert spec["security"] == [{"bearerAuth": []}]
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        path = re.sub(r"<([^>]+)>", r"{\1}", rule.rule)
        assert path in spec["paths"]
        assert {method.lower() for method in rule.methods - {"HEAD", "OPTIONS"}} == set(spec["paths"][path]) - {"parameters"}
    request_schema = {"$ref": "#/components/schemas/CompositionRequest", "components": spec["components"]}
    jsonschema.Draft202012Validator.check_schema(request_schema)
    validator = jsonschema.Draft202012Validator(request_schema)
    validator.validate(document())
    validator.validate(json.loads((Path(__file__).resolve().parents[1] / "examples" / "composition.json").read_text()))
    invalid = document()
    invalid["objects"][1]["spec"]["nodes"][0]["refId"] = "Not-a-valid-ID"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    assert set(spec["components"]["schemas"]["CompositionItem"]["properties"]["kind"]["enum"]) == COMPOSITION_KINDS
    assert {"202", "400", "401", "409", "413", "415", "422", "503"} <= spec["paths"]["/v1/compositions"]["post"]["responses"].keys()
    assert "secret" not in json.dumps(spec)


def test_openapi_schema_matches_persisted_receipt_and_audit_responses():
    """Validate real adapter outputs, including nullable Pod generations and pending observations."""
    import asyncio

    from polyad.api.store import CompositionStore
    from tests.test_composition_api import request_value
    from tests.test_operator import FakeAPI

    async def scenario():
        app = APIBuilder().with_handlers(lambda _: {}, lambda *_: None).with_bearer_token("token").build()
        components = app.extensions["polyad.openapi"]["components"]
        store = CompositionStore(FakeAPI(), "test")
        receipt = await store.submit(request_value())
        jsonschema.validate(receipt, {"$ref": "#/components/schemas/Receipt", "components": components})
        audit = await store.lookup("request-one", True)
        jsonschema.validate(audit, {"$ref": "#/components/schemas/Audit", "components": components})

    asyncio.run(scenario())


def test_builder_requires_nonempty_schema_metadata():
    """Fail during construction when the OpenAPI service identity would be invalid."""
    builder = APIBuilder().with_handlers(lambda _: {}, lambda *_: None).with_bearer_token("token")
    with pytest.raises(ValueError, match="OpenAPI"):
        builder.with_metadata("", "1").build()
