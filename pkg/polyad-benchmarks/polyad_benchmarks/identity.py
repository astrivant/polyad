"""
Correlate one benchmark submission across receipts, logs and dashboard searches.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from polyad_benchmarks.config import request_prefix

if TYPE_CHECKING:
    from typing import Any


def new_run_id() -> str:
    """
    Generate a fresh run key before making any submission.

    Returns:
        str: UUID-backed identity usable in activation IDs and dashboard links.
    """
    return "load-" + uuid.uuid4().hex


def parent_run_id(request_id: str) -> str:
    """
    Recover the run key from a numbered arrival or a runner activation.

    Args:
        request_id (str): Run key with an optional five-digit arrival suffix.

    Returns:
        str: Validated parent identity.
    """
    return request_prefix(re.sub(r"-\d{5}$", "", request_id))


def plan_hash(plan: str) -> str:
    """
    Fingerprint the resolved projected plan separately from its unique run identity.

    Args:
        plan (str): Exact nonsecret projected plan JSON from its mounted ConfigMap.

    Returns:
        str: SHA-256 of the projected JSON bytes, matching the chart release notes.
    """
    return hashlib.sha256(plan.encode()).hexdigest()


def grafana_path(run_id: str, namespace: str, *, start: str | int = "now-30m", end: str | int = "now") -> str:
    """
    Open the study dashboard at this run's identity, namespace and observation window.

    Args:
        run_id (str): Run identity entered into the trace filter.
        namespace (str): Operator namespace for aggregate metrics.
        start (str | int): Grafana relative time or milliseconds since the epoch.
        end (str | int): Grafana relative time or milliseconds since the epoch.

    Returns:
        str: Relative Grafana URL suitable for a private or administrator-supplied host.
    """
    return "/d/polyad-study?" + urlencode({"var-run_id": request_prefix(run_id), "var-namespace": namespace, "from": start, "to": end})


def log_event(event: str, run_id: str, **fields: Any) -> None:
    """
    Write selected nonsecret benchmark fields as one structured stderr record.

    Args:
        event (str): Stable benchmark event name.
        run_id (str): Identity shared by the submission and every arrival.
        **fields (Any): Explicit receipt, timing or outcome metadata, never request payloads or credentials.

    Returns:
        None: Flush one JSON line without mixing it into CLI stdout receipts.
    """
    print(
        json.dumps({"timestamp": datetime.now(UTC).isoformat(), "event": event, "runId": request_prefix(run_id), **fields}, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )
