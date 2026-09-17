"""
Stamp an isolated release checkout with versions derived from its Git tag.
"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


def prepare(tag: str) -> None:
    """
    Validate a tag and update package, chart, image and generated documentation versions.

    Args:
        tag (str): Release tag, optionally prefixed with refs/tags/.

    Returns:
        None: Files are updated in the current checkout.

    Raises:
        ValueError: The tag or expected metadata layout is unsupported.
    """
    match = re.fullmatch(
        r"v(\d+)\.(\d+)\.(\d+)(?:(?:-(alpha|beta|rc)[.-]?(\d*))|(?:(a|b|rc)(\d+)))?",
        tag.removeprefix("refs/tags/"),
    )
    if match is None:
        raise ValueError("Use vX.Y.Z, vX.Y.Z-alphaN, vX.Y.Z-betaN or vX.Y.Z-rcN")
    major, minor, patch, long_phase, long_number, short_phase, short_number = match.groups()
    base = ".".join(str(int(part)) for part in (major, minor, patch))
    phase = {"alpha": "a", "beta": "b", "rc": "rc"}.get(long_phase or "", short_phase)
    number = str(int(long_number or short_number or "0"))
    package = base + (f"{phase}{number}" if phase else "")
    phase_names = {"a": "alpha", "b": "beta", "rc": "rc"}
    chart = base + (f"-{phase_names[phase]}{number}" if phase else "")

    # Prepare every edit before writing, so a missing field cannot leave mixed versions.
    edits: dict[Path, str] = {}

    def replace(path: str, pattern: str, replacement: str) -> None:
        """
        Require exactly one metadata field and stage its replacement.
        """
        target = Path(path)
        source = edits.get(target, target.read_text())
        updated, count = re.subn(pattern, replacement, source, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"Expected one version field matching {pattern!r} in {path}; found {count}")
        edits[target] = updated

    project = Path("pyproject.toml")
    old_version = tomllib.loads(project.read_text())["project"]["version"]
    replace("pyproject.toml", rf'^(version\s*=\s*)"{re.escape(old_version)}"\s*$', rf'\g<1>"{package}"')
    replace("pkg/client/pyproject.toml", r'^(version\s*=\s*)"[^"\n]+"\s*$', rf'\g<1>"{package}"')
    replace("pkg/polyad-types/pyproject.toml", r'^(version\s*=\s*)"[^"\n]+"\s*$', rf'\g<1>"{package}"')
    replace("pkg/polyad-schemas/pyproject.toml", r'^(version\s*=\s*)"[^"\n]+"\s*$', rf'\g<1>"{package}"')
    replace("pyproject.toml", r'^schemas = \["polyad-schemas==[^"\n]+"\]$', f'schemas = ["polyad-schemas=={package}"]')
    for path in ("pyproject.toml", "pkg/client/pyproject.toml"):
        replace(path, r'^(\s*)"polyad-types==[^"\n]+",?$', rf'\g<1>"polyad-types=={package}",')
    # The local path dependency's version and the root dependency metadata change
    # together. Refresh this entry; the composite action refreshes the lock before builds.
    replace(
        "poetry.lock",
        r'(^name = "polyad-types"\nversion = )"[^"\n]+"',
        rf'\g<1>"{package}"',
    )
    replace("poetry.lock", r'(^name = "polyad-schemas"\nversion = )"[^"\n]+"', rf'\g<1>"{package}"')
    replace("charts/polyad/Chart.yaml", r"^version: .+$", f"version: {chart}")
    replace("charts/polyad/Chart.yaml", r"^appVersion: .+$", f"appVersion: {chart}")
    replace("charts/polyad/values.yaml", r"^    tag: .+$", f"    tag: '{chart}'")
    readme = Path("charts/polyad/README.md")
    source = readme.read_text()
    row = re.search(r"^(\| `operator\.image\.tag`[^\n]*\|)([^|]+)(\|)$", source, re.MULTILINE)
    if row is None:
        raise ValueError("Missing operator.image.tag in the generated chart README")
    cell = f" `{chart}`".ljust(len(row[2]) - 1) + " "
    edits[readme] = source[: row.start(2)] + cell + source[row.end(2) :]
    for path, content in edits.items():
        path.write_text(content)
    print(f"Prepared {tag}: package={package}, chart/image={chart}")


def main() -> None:
    """
    Prepare a release checkout from a validated command-line tag.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        prepare(args.tag)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
