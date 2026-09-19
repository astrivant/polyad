"""
Provide an actual subprocess with filesystem readiness and drain acknowledgements.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> None:
    """
    Advertise readiness and preserve an explicit acknowledgement before exiting.

    Returns:
        None: A drain request completes the worker's finite ownership contract.
    """
    directory = Path(sys.argv[1])
    identity = str(os.getpid())
    # Publish readiness only after its payload is complete, even under parallel load.
    staging = directory / f"{identity}.starting"
    staging.write_text(os.environ.get("TRACEPARENT", ""))
    staging.replace(directory / f"{identity}.ready")
    while not (directory / f"{identity}.drain").exists():
        time.sleep(0.005)
    (directory / f"{identity}.drained").touch()


if __name__ == "__main__":
    main()
