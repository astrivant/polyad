"""
Verify process evidence and keep published summaries smaller than raw measurements.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from polyad_benchmarks.studies.plotting import figure_names

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

FIGURES = tuple(figure_names("soul"))
ARTIFACTS = (*FIGURES, "fixed.json", "adaptive.json")


def fingerprint(output: Path) -> dict[str, str]:
    """
    Hash the complete expected artifact inventory without accepting arbitrary paths.

    Args:
        output (Path): One study's output directory.

    Returns:
        dict[str, str]: Relative artifact names and SHA-256 hashes.
    """
    return {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in ARTIFACTS}


def verify(result: dict[str, Any], recipe: dict[str, Any], output: Path) -> None:
    """
    Require both measured trials, complete job accounting and intact plot evidence.

    Args:
        result (dict[str, Any]): Candidate result document.
        recipe (dict[str, Any]): Immutable prepared inputs, including run identity.
        output (Path): Directory containing the candidate's raw evidence.

    Returns:
        None: The result is suitable for publication or an exception reports missing evidence.
    """
    if result.get("recipe") != recipe or result.get("figures") != list(FIGURES):
        raise ValueError("process study recipe or figure inventory changed")
    if result.get("artifacts") != fingerprint(output):
        raise ValueError("process study artifacts changed")
    records = result.get("records", [])
    if [record.get("mode") for record in records] != ["fixed", "adaptive"]:
        raise ValueError("process study requires fixed and adaptive measurements")
    expected = sum(round(phase["seconds"] * phase["rate"]) for phase in recipe["phases"])
    for record in records:
        if (
            record.get("allJoined") is not True
            or record.get("offered") != expected
            or record.get("completed", -1) + record.get("rejected", -1) != expected
            or len(record.get("jobs", {})) != record.get("completed")
            or not record.get("samples")
            or not record.get("frames")
            or any("finished" not in job for job in record["jobs"].values())
        ):
            raise ValueError("incomplete process measurements cannot be published")


def compact(result: dict[str, Any]) -> dict[str, Any]:
    """
    Retain outcomes, lifecycle decisions and artifact hashes in a reviewable summary.

    Args:
        result (dict[str, Any]): Verified full measurement document.

    Returns:
        dict[str, Any]: Publication copy; raw samples, graph frames and job ledgers stay in run artifacts.
    """
    return {
        **result,
        "records": [{k: v for k, v in record.items() if k not in {"samples", "frames", "jobs"}} for record in result["records"]],
    }
