"""
Regenerate or check the complete schema dependency graph in source order.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

GENERATORS = (
    "generate-status-schemas.py",
    "generate-network-schemas.py",
    "generate-resource-chart.py",
    "generate-reference-schema.py",
    "generate-event-schemas.py",
    "generate-json-schemas.py",
)


def main() -> int:
    """
    Run all schema generators; checks remain offline and report every failed stage.

    Returns:
        int: Nonzero when a source check or artifact generation fails.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--refresh-upstream", action="store_true")
    args = parser.parse_args()
    failed = False
    for name in GENERATORS:
        command = [sys.executable, str(Path(__file__).with_name(name))]
        if args.check:
            command.append("--check")
        if args.refresh_upstream and name == "generate-json-schemas.py":
            command.append("--refresh-upstream")
        result = subprocess.run(command, check=False)
        failed |= result.returncode != 0
        if failed and not args.check:
            break
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
