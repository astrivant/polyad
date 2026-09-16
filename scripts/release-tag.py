"""
Derive a safe Git version tag from the tested checkout's package metadata.
"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


def release_tag(project: Path) -> str:
    """
    Read the declared version and validate it before emitting workflow output.

    Args:
        project (Path): Path to the tested checkout's pyproject.toml.

    Returns:
        str: Git tag prefixed with v, using alpha, beta or rc prerelease spelling.
    """
    version = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:(?:a|b|rc)[0-9]+)?", version
    ):
        raise ValueError("release version must be X.Y.Z, optionally followed by aN, bN or rcN")
    release = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    assert release is not None
    base, phase, number = release.groups()
    phases = {"a": "alpha", "b": "beta", "rc": "rc"}
    suffix = f"-{phases[phase]}{number}" if phase else ""
    return f"v{base}{suffix}"


def main() -> None:
    """
    Print the version tag and optionally append it to GitHub's step output file.

    Returns:
        None: No return value.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    tag = release_tag(args.project)
    if args.output:
        with args.output.open("a", encoding="utf-8") as output:
            output.write(f"tag={tag}\n")
    print(tag)


if __name__ == "__main__":
    main()
