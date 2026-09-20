"""
Accept fresh, authorized application measurements without performing topology mutations at intake.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from polyad.api.http.errors import Conflict, Forbidden
from polyad.events.visibility import observation_ancestry, permitted_observation, public_observation
from polyad.operator.policies.soul.contracts import SAMPLE
from polyad_types.graphs.topology import topology
from polyad_types.serialization import to_dict

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad_types.api.auth import GraphAccess
    from polyad_types.api.throughput import ThroughputSample

__all__ = ("report_throughput",)


async def report_throughput(api: API, namespace: str, sample: ThroughputSample, grants: tuple[GraphAccess, ...] | None) -> dict[str, Any]:
    """
    Persist the latest aggregate report with an optimistic graph revision fence.

    Args:
        api (API): Ordered intake adapter.
        namespace (str): Listener namespace; callers cannot choose another destination.
        sample (ThroughputSample): Application's aggregate rates over one window.
        grants (tuple[GraphAccess, ...] | None): Named credential grants, or legacy namespace access.

    Returns:
        dict[str, Any]: Acknowledgement containing the accepted observation time.
    """
    obj = await api.get(sample.kind, namespace, sample.graph)
    reserved_name = os.environ.get("POLYAD_SELF_GRAPH", "")
    reserved = (os.environ.get("POLYAD_NAMESPACE", namespace), reserved_name) if reserved_name else None
    if obj is None or not await public_observation(api, obj, reserved_graph=reserved):
        raise Forbidden("throughput target is unavailable")
    identity = {"kind": obj["kind"], **obj["metadata"]}
    if grants is not None and not permitted_observation(identity, await observation_ancestry(api, obj), grants):
        raise Forbidden("credential does not authorize this graph tree")
    meta = obj["metadata"]
    if meta["uid"] != sample.graphUid or meta["generation"] != sample.generation or meta.get("deletionTimestamp"):
        raise Conflict("throughput target incarnation or generation changed")
    policy = topology(obj["spec"], obj["kind"]).throughput
    if policy is None or obj["spec"].get("templateOnly") or sample.unit != policy.unit:
        raise ValueError("throughput target requires an active policy with the same work unit")

    # Validate the policy's chosen signal and unit before persisting feedback;
    # offered throughput and an application-defined demand signal are not interchangeable.
    policy.demand_value(sample)
    observed = datetime.fromisoformat(sample.observedAt.replace("Z", "+00:00"))
    if not 0 <= (datetime.now(UTC) - observed).total_seconds() <= policy.sampleMaxAgeSeconds:
        raise ValueError("throughput sample is stale or from the future")
    value = to_dict(sample)
    if len(json.dumps(value, allow_nan=False).encode()) > 64 * 1024:
        raise ValueError("throughput reports must fit within 64 KiB")

    # Identical retries are harmless, but older or conflicting observations must
    # not replace newer demand evidence used by the adaptation controller.
    previous = json.loads(meta.get("annotations", {}).get(SAMPLE, "null"))
    if previous and datetime.fromisoformat(previous["observedAt"].replace("Z", "+00:00")) >= observed:
        if previous != value:
            raise Conflict("throughput observations must advance monotonically")
    else:
        await api.request(
            "PATCH",
            sample.kind,
            namespace,
            sample.graph,
            {"metadata": {"resourceVersion": meta["resourceVersion"], "annotations": {SAMPLE: json.dumps(value, allow_nan=False)}}},
        )
    return {"graphUid": sample.graphUid, "generation": sample.generation, "observedAt": sample.observedAt}
