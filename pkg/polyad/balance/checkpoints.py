"""
Store explicit JSON checkpoints with input identity and corruption checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

from polyad.graph.workloads import Estimate, Statistics

if TYPE_CHECKING:
    from polyad.graph.workloads import Work


def save(directory: Path, work: Work, payload: dict[str, object], statistics: Statistics) -> None:
    """
    Commit a checkpoint atomically after serializing the complete recoverable state.

    Args:
        directory (Path): Scheduler-owned checkpoint directory.
        work (Work): Workload identity.
        payload (dict[str, object]): Explicit workload checkpoint, never a live process image.
        statistics (Statistics): Cumulative completed units and cost estimates.

    Returns:
        None: A flushed checkpoint replaces its prior version atomically.
    """
    body = json.dumps(
        {"version": 1, "name": work.name, "fingerprint": work.fingerprint, "payload": payload, "statistics": asdict(statistics)},
        sort_keys=True,
        allow_nan=False,
    )
    envelope = json.dumps({"body": body, "sha256": hashlib.sha256(body.encode()).hexdigest()})
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(dir=directory, prefix=f".{work.name}-")
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(envelope)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(directory / f"{work.name}.json")
    finally:
        temporary.unlink(missing_ok=True)


def load(directory: Path, work: Work) -> tuple[dict[str, object], Statistics] | None:
    """
    Restore only a checkpoint with matching identity and intact serialized content.

    Args:
        directory (Path): Scheduler checkpoint directory.
        work (Work): Current input and implementation identity.

    Returns:
        tuple[dict[str, object], Statistics] | None: Verified state or None when no checkpoint exists.

    Raises:
        ValueError: Checkpoint integrity or workload identity does not match.
    """
    path = directory / f"{work.name}.json"
    if not path.exists():
        return None
    envelope = json.loads(path.read_text())
    body = envelope["body"]
    if hashlib.sha256(body.encode()).hexdigest() != envelope["sha256"]:
        raise ValueError("checkpoint checksum mismatch")
    document = json.loads(body)
    if document["version"] != 1 or document["name"] != work.name or document["fingerprint"] != work.fingerprint:
        raise ValueError("checkpoint input or implementation changed")
    if not isinstance(document["payload"], dict):
        raise ValueError("checkpoint payload must be an object")
    progress = document["statistics"]
    return cast("dict[str, object]", document["payload"]), Statistics(
        progress["completed"], progress["total"], Estimate(**progress["estimate"])
    )
