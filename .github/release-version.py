"""
Match release tags to Poetry's package version using Python prerelease normalization.

Poetry installs packaging, so this check runs before installing the project itself.
"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version


def release_version(package: str, tag: str | None) -> str:
    """
    Validate a release tag and return the canonical distribution version.

    Args:
        package (str): Version declared in pyproject.toml.
        tag (str | None): Pushed version tag, or None for a branch build.

    Returns:
        str: Normalized version used consistently for CI artifacts.

    Raises:
        ValueError: The tag is unsupported or differs from the declared version.
    """
    version = Version(package)
    if tag is not None:
        if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)(?:[.-]?\d+)?|(?:a|b|rc)\d+)?", tag):
            raise ValueError(f"Unsupported release tag: {tag}; use vX.Y.Z, vX.Y.Z-alpha.N, vX.Y.Z-beta.N or vX.Y.Z-rc.N")
        if Version(tag[1:]) != version:
            raise ValueError(f"Release tag {tag} does not match package version {version}")
    return str(version)


def main() -> None:
    """
    Validate checkout metadata and optionally emit a GitHub Actions version output.

    Returns:
        None: No return value.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    package = tomllib.loads(Path("pyproject.toml").read_text())["tool"]["poetry"]["version"]
    try:
        version = release_version(package, args.tag)
    except (InvalidVersion, ValueError) as error:
        parser.error(str(error))
    if args.output:
        with args.output.open("a") as output:
            output.write(f"version={version}\n")
    print(f"Validated polyad {version}")


if __name__ == "__main__":
    main()
